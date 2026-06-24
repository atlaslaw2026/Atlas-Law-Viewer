"""Atlas Law Server — serves only Atlas Law search files (not your drive)."""

import http.server
import os
from pathlib import Path
import re
import socketserver
import html
import json
import threading
import subprocess
import sys
from datetime import datetime
import urllib.parse
import urllib.request
import logging
import shutil

try:
    import cloudscraper
except Exception:
    cloudscraper = None

PORT = int(os.getenv("PORT", "8080"))
HOST = os.getenv("HOST", "127.0.0.1")
BASE_DIR = Path(__file__).resolve().parent
START_PAGE = os.getenv("ATLAS_START_PAGE", "opinions_index.html")
REFRESH_SCRIPT = (BASE_DIR / "atlas_daily_refresh.ps1").resolve()
REFRESH_LOG_DIR = (BASE_DIR / "logs").resolve()
REFRESH_SUMMARY_FILE = (REFRESH_LOG_DIR / "atlas_refresh_last_summary.json").resolve()
LEGACY_BASE_DIR = BASE_DIR.parent.resolve()
LOCAL_PDF_DIRS = [
    (BASE_DIR / "ninth_pdfs").resolve(),
    (BASE_DIR / "central_pdfs").resolve(),
    (BASE_DIR / "case_extractor" / "documents").resolve(),
    (LEGACY_BASE_DIR / "ninth_pdfs").resolve(),
    (LEGACY_BASE_DIR / "central_pdfs").resolve(),
    (LEGACY_BASE_DIR / "case_extractor" / "documents").resolve(),
]

REFRESH_LOCK = threading.Lock()
REFRESH_PROCESS: subprocess.Popen | None = None
REFRESH_STARTED_AT = ""
REFRESH_LAST_TRIGGER = ""

ALLOWED_EXACT = {
    "opinions_index.html",
    "opinions_browser.html",
    "opinions_data.json",
    "central_opinions_index.html",
    "central_opinions_data.json",
    "supreme_opinions_index.html",
    "supreme_opinions_data.json",
    "case_extractor_index.html",
    "case_extractor_data.json",
}

ALLOWED_PATTERNS = [
    re.compile(r"opinion_viewer_\d+\.html"),
]

DENIED_EXACT = {
    "atlas_law.db",
    "atlas_law_server.py",
    "atlas_law_server_v2.py",
}

PDF_PROXY_RULES: dict[str, tuple[str, ...] | None] = {
    "cdn.ca9.uscourts.gov": None,
    "www.ca9.uscourts.gov": None,
    "www.supremecourt.gov": ("/opinions/",),
    "supremecourt.gov": ("/opinions/",),
    "law.justia.com": ("/cases/federal/district-courts/california/cacdce/",),
    "cases.justia.com": None,
}

LOGGER = logging.getLogger(__name__)


def is_allowed_name(name: str) -> bool:
    if name in DENIED_EXACT:
        return False
    if name in ALLOWED_EXACT:
        return True
    return any(pattern.fullmatch(name) for pattern in ALLOWED_PATTERNS)


def is_allowed_pdf_target(target: urllib.parse.ParseResult) -> bool:
    host = (target.netloc or "").lower()
    path = target.path or ""
    allowed_prefixes = PDF_PROXY_RULES.get(host)
    if target.scheme not in {"http", "https"} or allowed_prefixes is None and host not in PDF_PROXY_RULES:
        return False

    lower_path = path.lower()
    if host in {"www.supremecourt.gov", "supremecourt.gov"} and not lower_path.endswith(".pdf"):
        return False

    # Justia CACD docket pages expose downloadable PDFs via a "/download" path
    # that may not include a .pdf suffix.
    if host == "law.justia.com" and not (lower_path.endswith(".pdf") or "/download" in lower_path):
        return False

    if allowed_prefixes is None:
        return True

    return any(path.startswith(prefix) for prefix in allowed_prefixes)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def list_directory(self, path):
        self.send_error(403, "Directory listing is disabled")
        return None

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        return super().end_headers()

    def _send_json(self, status_code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._write_response_body(body)

    def _write_response_body(self, data: bytes) -> bool:
        try:
            self.wfile.write(data)
            return True
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            # Client disconnected before receiving the full response.
            return False

    def _refresh_status_payload(self) -> dict:
        global REFRESH_PROCESS, REFRESH_STARTED_AT, REFRESH_LAST_TRIGGER
        with REFRESH_LOCK:
            running = REFRESH_PROCESS is not None and REFRESH_PROCESS.poll() is None
            exit_code = None
            if REFRESH_PROCESS is not None and not running:
                exit_code = REFRESH_PROCESS.poll()

            refresh_summary = None
            try:
                if REFRESH_SUMMARY_FILE.exists():
                    refresh_summary = json.loads(REFRESH_SUMMARY_FILE.read_text(encoding="utf-8-sig"))
            except Exception:
                refresh_summary = None

            return {
                "running": running,
                "started_at": REFRESH_STARTED_AT,
                "last_trigger": REFRESH_LAST_TRIGGER,
                "exit_code": exit_code,
                "refresh_summary": refresh_summary,
            }

    def _handle_refresh_status(self):
        return self._send_json(200, self._refresh_status_payload())

    def _handle_run_refresh(self):
        global REFRESH_PROCESS, REFRESH_STARTED_AT, REFRESH_LAST_TRIGGER

        if not REFRESH_SCRIPT.exists():
            return self._send_json(500, {"ok": False, "message": "Refresh script not found"})

        with REFRESH_LOCK:
            if REFRESH_PROCESS is not None and REFRESH_PROCESS.poll() is None:
                payload = {
                    "ok": False,
                    "message": "Refresh already running",
                    "running": True,
                    "started_at": REFRESH_STARTED_AT,
                    "last_trigger": REFRESH_LAST_TRIGGER,
                    "exit_code": None,
                }
                return self._send_json(409, payload)

            try:
                REFRESH_LOG_DIR.mkdir(parents=True, exist_ok=True)
                startup_info = None
                if os.name == "nt":
                    startup_info = subprocess.STARTUPINFO()
                    startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW

                creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                REFRESH_PROCESS = subprocess.Popen(
                    [
                        "powershell.exe",
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(REFRESH_SCRIPT),
                    ],
                    cwd=str(BASE_DIR),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    startupinfo=startup_info,
                    creationflags=creation_flags,
                )
            except Exception as exc:
                return self._send_json(500, {"ok": False, "message": f"Failed to start refresh: {exc}"})

            now = datetime.now().isoformat(timespec="seconds")
            REFRESH_STARTED_AT = now
            REFRESH_LAST_TRIGGER = now
            return self._send_json(
                202,
                {
                    "ok": True,
                    "message": "Refresh started",
                    "pid": REFRESH_PROCESS.pid,
                    "started_at": REFRESH_STARTED_AT,
                },
            )

    def _extract_reporter_citation_url(self, citation: str) -> str | None:
        match = re.search(r"\b(\d+)\s+([A-Za-z][A-Za-z.\d ]{0,25})\s+(\d+)\b", citation)
        if not match:
            return None
        volume, reporter, page = match.groups()
        reporter_slug = re.sub(r"\s+", "-", reporter.strip())
        reporter_slug = re.sub(r"-+", "-", reporter_slug)
        reporter_slug = urllib.parse.quote(reporter_slug, safe=".-")
        return f"https://www.courtlistener.com/citation/{volume}/{reporter_slug}/{page}/"

    def _is_courtlistener_url(self, url: str) -> bool:
        host = (urllib.parse.urlparse(url).netloc or "").lower()
        return "courtlistener.com" in host

    def _extract_result_links(self, html_text: str) -> list[str]:
        links: list[str] = []
        for href in re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"', html_text):
            value = html.unescape(href)
            if value.startswith("/l/?"):
                parsed = urllib.parse.urlparse(value)
                query = urllib.parse.parse_qs(parsed.query)
                target = (query.get("uddg") or [""])[0]
                if target:
                    value = urllib.parse.unquote(target)
            if value.startswith("//"):
                value = "https:" + value
            if value.startswith("http://") or value.startswith("https://"):
                links.append(value)
        return links

    def _best_case_url(self, citation: str, hint: str = "") -> str:
        citation = (citation or "").strip()
        hint = (hint or "").strip()
        preferred_domains = (
            "law.justia.com",
            "openjurist.org",
            "casetext.com",
            "scholar.google.com",
        )
        if (hint.startswith("http://") or hint.startswith("https://")) and not self._is_courtlistener_url(hint):
            return hint

        web_query = f'"{citation}" (site:law.justia.com OR site:openjurist.org OR site:casetext.com OR site:scholar.google.com OR site:courtlistener.com)'
        search_url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(web_query)
        req = urllib.request.Request(search_url, headers={"User-Agent": "Mozilla/5.0"})

        try:
            body = urllib.request.urlopen(req, timeout=8).read().decode("utf-8", errors="ignore")
            links = self._extract_result_links(body)

            for link in links:
                host = (urllib.parse.urlparse(link).netloc or "").lower()
                if any(domain in host for domain in preferred_domains):
                    return link

            for link in links:
                host = (urllib.parse.urlparse(link).netloc or "").lower()
                if "courtlistener.com" not in host:
                    return link

            if links:
                return links[0]
        except Exception as exc:
            LOGGER.warning("Citation resolver search failed for '%s': %s", citation, exc)

        if (hint.startswith("http://") or hint.startswith("https://")) and not self._is_courtlistener_url(hint):
            return hint

        fallback_query = citation if citation else "federal case"
        return "https://www.google.com/search?q=" + urllib.parse.quote(fallback_query + " case")

    def _handle_case_resolver(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        citation = (params.get("citation") or [""])[0]
        hint = (params.get("hint") or [""])[0]

        if not citation.strip():
            self.send_error(400, "Missing citation")
            return

        target = self._best_case_url(citation, hint)
        self.send_response(302)
        self.send_header("Location", target)
        self.end_headers()
        return

    def _handle_pdf_proxy(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        raw_url = (params.get("url") or [""])[0].strip()

        if not raw_url:
            self.send_error(400, "Missing url")
            return

        try:
            target = urllib.parse.urlparse(raw_url)
        except Exception:
            self.send_error(400, "Invalid url")
            return

        if not is_allowed_pdf_target(target):
            # Keep the in-program viewer as the default path by redirecting
            # to the source URL instead of returning an HTML notice page.
            self.send_response(302)
            self.send_header("Location", raw_url)
            self.end_headers()
            return

        host = (target.netloc or "").lower()

        def send_justia_notice() -> None:
            notice_html = f"""<!doctype html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\">
    <title>PDF unavailable in preview</title>
    <style>
        body {{
            font-family: Georgia, 'Times New Roman', serif;
            margin: 0;
            padding: 16px;
            color: #1f2937;
            background: #f8fafc;
        }}
        .box {{
            border: 1px solid #cbd5e1;
            background: #ffffff;
            padding: 14px;
        }}
        h2 {{ margin: 0 0 10px 0; color: #0b4fa8; font-size: 20px; }}
        p {{ margin: 8px 0; line-height: 1.45; }}
        a {{ color: #0b4fa8; font-weight: 700; }}
    </style>
</head>
<body>
    <div class=\"box\">
        <h2>Preview blocked by source site</h2>
        <p>Justia is currently blocking automated PDF retrieval from this endpoint.</p>
        <p><a href=\"{html.escape(raw_url, quote=True)}\" target=\"_blank\">Open original PDF link in a new tab</a></p>
    </div>
</body>
</html>"""
            body = notice_html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self._write_response_body(body)

        data = b""
        content_type = ""
        try:
            if cloudscraper is not None and host in {"law.justia.com", "cases.justia.com"}:
                scraper = cloudscraper.create_scraper()
                resp = scraper.get(raw_url, timeout=20)
                if resp is not None and resp.status_code == 200:
                    data = resp.content or b""
                    content_type = (resp.headers.get("Content-Type") or "").lower()

            if not data:
                req = urllib.request.Request(raw_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = resp.read()
                    content_type = (resp.headers.get("Content-Type") or "").lower()
        except Exception:
            if host in {"law.justia.com", "cases.justia.com"}:
                # If server-side fetch is blocked, fall back to direct source.
                self.send_response(302)
                self.send_header("Location", raw_url)
                self.end_headers()
                return
            self.send_error(502, "Unable to fetch PDF")
            return

        if not data:
            if host in {"law.justia.com", "cases.justia.com"}:
                self.send_response(302)
                self.send_header("Location", raw_url)
                self.end_headers()
                return
            self.send_error(502, "Empty PDF response")
            return

        # Reject HTML/challenge pages so the client can fall back gracefully.
        if ("pdf" not in content_type) and not data.startswith(b"%PDF"):
            if host in {"law.justia.com", "cases.justia.com"}:
                self.send_response(302)
                self.send_header("Location", raw_url)
                self.end_headers()
                return
            self.send_error(502, "Upstream did not return a PDF")
            return

        filename = Path(target.path).name or "opinion.pdf"
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", f'inline; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self._write_response_body(data)
        return

    def _handle_local_pdf(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        raw_path = (params.get("path") or [""])[0].strip()

        if not raw_path:
            self.send_error(400, "Missing path")
            return

        try:
            target = Path(raw_path).resolve()
        except Exception:
            self.send_error(400, "Invalid path")
            return

        def resolve_by_filename(path_text: str) -> Path | None:
            try:
                name = Path(path_text).name
            except Exception:
                return None
            if not name or name in {".", ".."} or not name.lower().endswith(".pdf"):
                return None
            for base in LOCAL_PDF_DIRS:
                candidate = (base / name).resolve()
                if candidate.exists() and candidate.is_file():
                    return candidate
            return None

        allowed = False
        target_text = str(target).lower()
        for base in LOCAL_PDF_DIRS:
            base_text = str(base).lower()
            if target_text.startswith(base_text + os.sep.lower()) or target_text == base_text:
                allowed = True
                break

        if not allowed:
            portable_target = resolve_by_filename(raw_path)
            if portable_target is not None:
                target = portable_target
                allowed = True

        if not allowed:
            # Cross-machine installs may carry absolute paths from another device.
            # Treat a valid PDF basename as cache-miss (404) instead of forbidden (403).
            fallback_name = Path(raw_path).name
            if fallback_name and fallback_name not in {".", ".."} and fallback_name.lower().endswith(".pdf"):
                target = (LOCAL_PDF_DIRS[0] / fallback_name).resolve()
                allowed = True

        if not allowed:
            self.send_error(403, "Forbidden")
            return

        if not target.exists() or not target.is_file():
            self.send_error(404, "PDF not found")
            return

        try:
            data = target.read_bytes()
        except Exception:
            self.send_error(500, "Unable to read PDF")
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", f'inline; filename="{target.name}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self._write_response_body(data)
        return

    def _handle_central_proxy(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        raw_url = (params.get("url") or [""])[0].strip()

        if not raw_url:
            self.send_error(400, "Missing url")
            return

        try:
            target = urllib.parse.urlparse(raw_url)
        except Exception:
            self.send_error(400, "Invalid url")
            return

        allowed_host = "law.justia.com"
        allowed_prefix = "/cases/federal/district-courts/california/cacdce/"
        if (
            target.scheme not in {"http", "https"}
            or (target.netloc or "").lower() != allowed_host
            or not (target.path or "").startswith(allowed_prefix)
        ):
            self.send_error(403, "Forbidden")
            return

        html_data = ""
        try:
            if cloudscraper is not None:
                scraper = cloudscraper.create_scraper()
                resp = scraper.get(raw_url, timeout=20)
                if resp is not None and resp.status_code == 200:
                    html_data = resp.text or ""

            if not html_data:
                req = urllib.request.Request(raw_url, headers={"User-Agent": "Mozilla/5.0"})
                html_data = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", errors="ignore")
        except Exception:
            self.send_error(502, "Unable to fetch source page")
            return

        if "<head" in html_data.lower() and "<base " not in html_data.lower():
            html_data = re.sub(
                r"(?i)<head([^>]*)>",
                r'<head\1><base href="https://law.justia.com/">',
                html_data,
                count=1,
            )

        body = html_data.encode("utf-8", errors="ignore")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._write_response_body(body)
        return

    def _find_deploy_zip(self) -> Path | None:
        # Prefer full package, then lite, then older bundled variants.
        for name in ("Atlas_GitHub_Portfolio_full_v6.zip", "Atlas_GitHub_Portfolio_full_v5.zip", "Atlas_GitHub_Portfolio_full_v4.zip", "Atlas_GitHub_Portfolio_full_v3.zip", "Atlas_GitHub_Portfolio_full_v2.zip", "Atlas_GitHub_Portfolio_full.zip", "Atlas_GitHub_Portfolio_ship_lite.zip"):
            candidate = BASE_DIR.parent / name
            if candidate.exists():
                return candidate

        exact_candidates = sorted(
            BASE_DIR.parent.glob("Atlas_GitHub_Portfolio_folder_exact*.zip"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for candidate in exact_candidates:
            if candidate.exists():
                return candidate

        bundled_candidates = sorted(
            BASE_DIR.parent.glob("Atlas_GitHub_Portfolio_deploy_bundled_runtime*.zip"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for candidate in bundled_candidates:
            if candidate.exists():
                return candidate

        for name in ("Atlas_GitHub_Portfolio_deploy.zip",):
            candidate = BASE_DIR.parent / name
            if candidate.exists():
                return candidate
        return None

    def _handle_install_page(self):
        zip_path = self._find_deploy_zip()
        zip_name = zip_path.name if zip_path else None
        zip_size = f"{zip_path.stat().st_size / (1024*1024):.1f} MB" if zip_path else "unavailable"
        zip_token = str(int(zip_path.stat().st_mtime)) if zip_path else "0"

        if zip_path:
            download_block = f"""
            <p style="margin:16px 0 8px">
                <a href="/download/atlas" style="display:inline-block;background:#003da5;color:#fff;padding:12px 28px;font-size:16px;font-weight:bold;text-decoration:none;border-radius:3px;">
                    ⬇ Download Atlas Law Viewer ({zip_size})
                </a>
            </p>
            <p style="font-size:12px;color:#666;">File: {html.escape(zip_name)}</p>"""
            download_block = download_block.replace('/download/atlas', f'/download/atlas?v={zip_token}')
        else:
            download_block = '<p style="color:#b00;margin-top:16px;">Deploy zip not found on server. Run the deploy zip build first.</p>'

        body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<title>Install Atlas Law Viewer</title>
<style>body{{font-family:"Times New Roman",serif;max-width:640px;margin:60px auto;padding:20px;color:#222}}
h1{{color:#003da5}}ol li{{margin-bottom:10px}}code{{background:#f0f4ff;padding:2px 6px;border-radius:2px}}</style>
</head><body>
<h1>Atlas Law Viewer — Install</h1>
<p>Download and run the app on this computer in three steps.</p>
{download_block}
<h2 style="margin-top:32px">After downloading:</h2>
<ol>
  <li>Unzip the downloaded file anywhere (e.g. Desktop or Documents).</li>
  <li>Open the unzipped folder and double-click <code>launch_atlas_standard.cmd</code>.</li>
  <li>A browser window will open automatically to <code>http://127.0.0.1:8080/</code>.</li>
</ol>
<p style="margin-top:24px;font-size:13px;color:#555">No Python installation required. Everything is bundled.</p>
</body></html>""".encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._write_response_body(body)

    def _handle_download_zip(self):
        zip_path = self._find_deploy_zip()
        if not zip_path:
            return self._send_json(404, {"error": "Deploy zip not found."})

        try:
            size = zip_path.stat().st_size
        except Exception as exc:
            return self._send_json(500, {"error": str(exc)})

        try:
            stream = zip_path.open("rb")
        except Exception as exc:
            return self._send_json(500, {"error": f"Unable to open deploy zip: {exc}"})

        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{zip_path.name}"')
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        try:
            with stream:
                shutil.copyfileobj(stream, self.wfile, length=1024 * 1024)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as exc:
            LOGGER.warning("Download stream failed for %s: %s", zip_path, exc)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        raw_path = parsed.path

        if raw_path == "/ninth/":
            self.send_response(302)
            self.send_header("Location", "/ninth")
            self.end_headers()
            return

        if raw_path == "/supreme/":
            self.send_response(302)
            self.send_header("Location", "/supreme")
            self.end_headers()
            return

        if raw_path == "/central/":
            self.send_response(302)
            self.send_header("Location", "/central")
            self.end_headers()
            return

        # Explicit page routes avoid false 403s from generic path validation.
        if raw_path in ("/opinions_index.html", "/ninth"):
            return self._serve_file_direct(BASE_DIR / "opinions_index.html")

        if raw_path in ("/supreme_opinions_index.html", "/supreme"):
            return self._serve_file_direct(BASE_DIR / "supreme_opinions_index.html")

        if raw_path in ("/central_opinions_index.html", "/central"):
            return self._serve_file_direct(BASE_DIR / "central_opinions_index.html")

        if raw_path == "/api/resolve_case":
            return self._handle_case_resolver()

        if raw_path == "/api/pdf":
            return self._handle_pdf_proxy()

        if raw_path == "/api/local_pdf":
            return self._handle_local_pdf()

        if raw_path == "/api/central_proxy":
            return self._handle_central_proxy()

        if raw_path == "/api/refresh_status":
            return self._handle_refresh_status()

        if raw_path in ("/install", "/install/"):
            return self._handle_install_page()

        if raw_path == "/download/atlas":
            return self._handle_download_zip()

        if raw_path in ("", "/"):
            self.send_response(302)
            self.send_header("Location", "/ninth")
            self.end_headers()
            return

        rel = Path(raw_path.lstrip("/"))

        if rel.is_absolute() or ".." in rel.parts or len(rel.parts) != 1:
            self.send_error(403, "Forbidden")
            return

        if not is_allowed_name(rel.name):
            self.send_error(404, "Not Found")
            return

        file_path = BASE_DIR / rel.name
        if file_path.exists():
            return self._serve_file_direct(file_path)

        self.path = f"/{rel.name}"
        return super().do_GET()

    def _serve_file_direct(self, file_path: Path):
        import mimetypes
        mime_type, _ = mimetypes.guess_type(str(file_path))
        if not mime_type:
            mime_type = "application/octet-stream"
        try:
            size = file_path.stat().st_size
        except Exception:
            self.send_error(500, "Internal Server Error")
            return
        self.send_response(200)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        try:
            with file_path.open("rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        raw_path = parsed.path

        if raw_path == "/api/run_refresh":
            return self._handle_run_refresh()

        self.send_error(404, "Not Found")
        return

    def log_message(self, format, *args):
        return


class ReuseAddrTCPServer(socketserver.TCPServer):
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        _, exc, _ = sys.exc_info()
        if isinstance(exc, ConnectionResetError):
            return
        return super().handle_error(request, client_address)


def start_server():
    with ReuseAddrTCPServer((HOST, PORT), Handler) as httpd:
        print(f"\n✅ Atlas Law Server running at http://{HOST}:{PORT}")
        print(f"📍 Base path: {BASE_DIR}")
        print(f"🔒 Serving Atlas-only allowlist (no directory browsing)")
        print(f"🌐 Press Ctrl+C to stop\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n✋ Server stopped.")


if __name__ == "__main__":
    start_server()
