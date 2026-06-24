"""Central District (C.D. Cal.) opinions viewer generator (Justia 2026)."""

import json
import os
import re
import hashlib
import sqlite3
import html as html_lib
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
import logging

try:
    import cloudscraper
except Exception:
    cloudscraper = None

try:
    from PyPDF2 import PdfReader
except Exception:
    PdfReader = None

BASE_DIR = os.path.dirname(__file__)
HTML_FILE = os.path.join(BASE_DIR, "central_opinions_index.html")
JSON_FILE = os.path.join(BASE_DIR, "central_opinions_data.json")
CACHE_FILE = os.path.join(BASE_DIR, "central_case_cache.json")
DB_PATH = os.path.join(BASE_DIR, "atlas_law.db")
PDF_DIR = os.path.join(BASE_DIR, "central_pdfs")
LIST_SOURCE_URL = "https://law.justia.com/cases/federal/district-courts/california/cacdce/2026/"
LISTING_RAW_FALLBACK_FILE = os.path.join(BASE_DIR, "central_live_listing_raw.txt")
MAX_FETCH_PER_RUN = int(os.getenv("CENTRAL_FETCH_LIMIT", "300"))
MAX_PDF_DOWNLOAD_PER_RUN = int(os.getenv("CENTRAL_PDF_LIMIT", "300"))
SKIP_LOCAL_PDF_READ = os.getenv("CENTRAL_SKIP_PDF_READ", "0").strip() in {"1", "true", "TRUE", "yes", "YES"}

CASE_PATTERNS = [
    r'([A-Z][a-zA-Z0-9\s&,.\'\-]{3,50}?)\s+v\.\s+([A-Z][a-zA-Z0-9\s&,.\'\-]{3,50}?),\s+(\d+)\s+(U\.S\.|F\.\d?d|F\.\d?th|P\.\d?d|S\.Ct\.|Cal\.\s?App)',
    r'([A-Z][a-zA-Z0-9\s&,.\'\-]{3,60}?)\s+v\.\s+([A-Z][a-zA-Z0-9\s&,.\'\-]{3,60}?)',
    r'(In\s+re\s+[A-Z][A-Za-z0-9\s&,.\'\-]{3,80})',
]
STATUTE_PATTERNS = [
    r'(\d+)\s+U\.S\.C\.?\s*§?\s*(\d+[a-zA-Z0-9\-]*)',
]
REGULATION_PATTERNS = [
    r'(\d+)\s+C\.F\.R\.?\s*§?\s*(\d+(?:\.\d+)*)',
]
RULE_PATTERNS = [
    r'Fed\.\s+R\.[A-Za-z\.\s]*\s+\d+(?:\([a-zA-Z0-9]+\))*',
    r'Rule\s+\d+(?:\([a-zA-Z0-9]+\))*',
]

LOGGER = logging.getLogger(__name__)


def fetch_markdown(url: str, timeout: int = 12) -> str:
    if cloudscraper is not None and "justia.com" in (url or ""):
        try:
            scraper = cloudscraper.create_scraper()
            response = scraper.get(url, timeout=timeout)
            if response is not None and response.status_code == 200:
                return response.text or ""
        except Exception as exc:
            LOGGER.warning("cloudscraper fetch failed for %s: %s", url, exc)

    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        return urllib.request.urlopen(request, timeout=timeout).read().decode("utf-8", errors="ignore")
    except Exception:
        return ""


def is_cloudflare_challenge(text: str | None) -> bool:
    source = (text or "").lower()
    if not source:
        return False
    return "just a moment" in source and "challenges.cloudflare.com" in source


def load_listing_from_browser_dump(file_path: str = LISTING_RAW_FALLBACK_FILE) -> list[dict]:
    if not os.path.exists(file_path):
        return []

    try:
        raw = open(file_path, "r", encoding="utf-8", errors="ignore").read().strip()
    except Exception:
        return []

    if not raw:
        return []

    if raw.startswith("Result:"):
        raw = raw.split("Result:", 1)[1].strip()

    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]

    decoded = (
        raw.replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace('\\"', '"')
    )

    cases: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for idx, line in enumerate(decoded.splitlines(), start=1):
        parts = [normalize_ws(part) for part in line.split("\t")]
        if len(parts) < 4:
            continue
        title, url, date, docket = parts[:4]
        if not title or not url:
            continue

        key = (title.lower(), url)
        if key in seen:
            continue
        seen.add(key)

        cases.append(
            {
                "id": idx,
                "title": normalize_text(title),
                "date": date,
                "docket": docket,
                "url": normalize_central_url(url),
                "subjects": ["central-district"],
            }
        )

    return cases


def mirror_case_url(url: str) -> str:
    return (url or "").strip()


def normalize_text(value: str | None) -> str:
    text = str(value or "")
    return (
        text.replace("Â§", "§")
        .replace("Ã‚Â§", "§")
        .replace("â€”", "—")
        .replace("â€“", "–")
        .replace("â€™", "’")
        .replace("â€œ", "“")
        .replace("â€�", "”")
    )


def normalize_ws(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_central_url(url: str | None) -> str:
    value = normalize_ws(url)
    if not value:
        return ""
    if value.startswith("//"):
        return "https:" + value
    if value.startswith("/"):
        return "https://law.justia.com" + value
    return value


def clean_case_citations(items: list[str], title: str | None = None) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()

    party = r"[A-Z][A-Za-z0-9'&.\-]*(?:\s+[A-Z][A-Za-z0-9'&.\-]*){0,12}"
    case_core_pattern = re.compile(
        rf"(({party})\s+v\.\s+({party})(?:,\s*\d+\s+[A-Z][A-Za-z.\d\s]{{1,25}}\s+\d+(?:,\s*\d+)?)?(?:\s*\([^)]*\))?)"
    )

    blocked_pattern = re.compile(
        r"Court Description|Petitioner|Respondent|DKT\.?\s*NO\.?|InSign\s*Up|Image\s*\d+|My Account|Find A Lawyer|Ask A Lawyer|^cv-\d",
        flags=re.IGNORECASE,
    )

    for raw in items:
        value = normalize_ws(normalize_text(raw)).strip(" ,;:")
        if not value:
            continue

        value = re.sub(r"^[^A-Z]*(?=[A-Z])", "", value)

        value = re.sub(r"^(?:See|Cf\.|But\s+see|Compare|quoting)\s+", "", value, flags=re.IGNORECASE)
        signaled = re.split(r"\b(?:See|Cf\.|But\s+see|Compare|quoting)\b", value, flags=re.IGNORECASE)
        if len(signaled) > 1:
            value = normalize_ws(signaled[-1]).strip(" ,;:")

        core = case_core_pattern.search(value)
        if core:
            value = normalize_ws(core.group(1)).strip(" ,;:")

        is_title = title and value.lower() == normalize_ws(normalize_text(title)).lower()
        has_v = bool(re.search(r"\bv\.\s", value, flags=re.IGNORECASE))
        has_in_re = bool(re.search(r"^\s*in\s+re\b", value, flags=re.IGNORECASE))
        has_reporter = bool(re.search(r"\b\d+\s+[A-Z][A-Za-z.\d ]{0,20}\s+\d+\b", value))

        if blocked_pattern.search(value):
            continue
        if len(value) > 155:
            continue
        if re.search(r"\b\d{2,}\s+\d{2,}\s+\d{2,}\b", value):
            continue
        if not (is_title or has_v or has_in_re):
            continue

        lowered = value.lower()
        if any(token in lowered for token in [
            "download pdf",
            "final judgment",
            "dockets.justia.com",
            "united states district court",
            "order granting",
            "doc.",
        ]):
            continue

        if has_v:
            parts = re.split(r"\bv\.\s", value, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) == 2:
                left_part = normalize_ws(parts[0])
                right_part = normalize_ws(parts[1])
                if len(left_part) < 4 or len(right_part) < 4:
                    continue
                left_words = re.findall(r"[A-Za-z]+", left_part)
                right_words = re.findall(r"[A-Za-z]+", right_part)
                if len(left_words) < 2 or len(right_words) < 1:
                    continue
                if re.match(r"^(inc|co|corp|llc|ltd)\.?$", left_part.strip(), flags=re.IGNORECASE):
                    continue

        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(value)

    return cleaned[:30]


def dedupe_normalized(items: list[str], limit: int = 30) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in items:
        value = normalize_ws(normalize_text(raw)).strip(" ,;:")
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
        if len(out) >= limit:
            break
    return out


def collapse_duplicate_opinions(items: list[dict]) -> list[dict]:
    def score(op: dict) -> tuple[int, int, int, int]:
        text_len = len(normalize_ws(op.get("text") or ""))
        has_pdf = 1 if (op.get("pdf_url") or op.get("local_pdf_path")) else 0
        auth_count = len(((op.get("authorities") or {}).get("cases") or []))
        date_quality = 1 if (op.get("issue_date") or "").endswith("-00-00") is False and (op.get("issue_date") or "") else 0
        return (date_quality, has_pdf, text_len, auth_count)

    grouped: dict[tuple[str, str, str], dict] = {}
    for op in items:
        title = normalize_ws(op.get("title") or "").lower()
        docket = normalize_ws(op.get("docket") or "").lower()
        date_key = normalize_ws(op.get("issue_date") or op.get("date") or "")
        key = (title, docket, date_key)

        existing = grouped.get(key)
        if existing is None:
            grouped[key] = op
            continue

        if score(op) > score(existing):
            grouped[key] = op

    collapsed = list(grouped.values())
    for idx, op in enumerate(collapsed, start=1):
        op["id"] = idx
    return collapsed


def build_preview(full_text: str, date_value: str, docket_value: str) -> str:
    source = normalize_ws(full_text or "")
    if not source:
        return f"Date: {date_value} | Docket: {docket_value}"

    source = re.sub(r"\b(?:ORDER|JUDGMENT|MEMORANDUM OPINION)\b\s*[:\-]?", "", source, flags=re.IGNORECASE)
    source = re.sub(r"\bDKT\.?\s*NO\.?\s*\d+\b", "", source, flags=re.IGNORECASE)

    parts = re.split(r"(?<=[.!?])\s+", source)
    preview = " ".join(parts[:2]).strip()
    if len(preview) > 240:
        preview = preview[:237].rstrip() + "..."
    return preview


def is_noisy_page_text(text: str | None) -> bool:
    source = normalize_ws(text or "")
    if not source:
        return True

    low = source.lower()
    noise_markers = [
        "research the law",
        "justia connect",
        "lawyer directory",
        "marketing solutions",
        "platinum placements",
        "gold placements",
        "find a lawyer",
        "ask a lawyer",
        "free summaries",
        "log in sign up",
    ]
    hits = sum(1 for marker in noise_markers if marker in low)
    return hits >= 2


def _extract_caption_candidate(text: str | None) -> str:
    source = normalize_ws(normalize_text(text or ""))
    if not source:
        return ""

    source = re.sub(r"United States District Court Central District of California", " ", source, flags=re.IGNORECASE)
    source = re.sub(r"\b\d{1,2}\b", " ", source)
    source = re.sub(r"\s{2,}", " ", source).strip()

    match = re.search(r"([A-Z][A-Za-z0-9&.,'\-\s]{2,90}?)\s+v\.\s+([A-Z][A-Za-z0-9&.,'\-\s]{2,90})", source)
    if not match:
        return ""

    left = normalize_ws(match.group(1)).strip(" ,.;:")
    right = normalize_ws(match.group(2)).strip(" ,.;:")
    left = re.split(r"\b(?:Petitioner|Plaintiff|Defendant|Respondent)\b", left, maxsplit=1, flags=re.IGNORECASE)[0]
    left = normalize_ws(left).strip(" ,.;:")
    right = re.split(r"\b(?:Case|No\.|Doc\.|Petitioner|Plaintiff|Defendant|Respondent|et\s+al\.)\b", right, maxsplit=1, flags=re.IGNORECASE)[0]
    right = normalize_ws(right).strip(" ,.;:")

    caption = f"{left} v. {right}".strip()
    if len(caption) > 170 or not left or not right:
        return ""
    return caption


def clean_display_title(raw_title: str | None, full_text: str | None, docket_value: str | None) -> str:
    title = normalize_ws(normalize_text(raw_title))

    if title:
        title = re.sub(r"^Download\s+PDF\s*", "", title, flags=re.IGNORECASE)
        title = re.sub(r"\s+Doc\.\s*\d+.*$", "", title, flags=re.IGNORECASE)
        title = re.sub(r"\s+\d\s+\d\s+\d\s+\d.*$", "", title)
        title = re.sub(r",\s*No\.\s*\d+.*$", "", title, flags=re.IGNORECASE)
        title = normalize_ws(title).strip(" ,.;:")

    from_title = _extract_caption_candidate(title)
    if from_title:
        return from_title

    if title and " v. " in title.lower() and "quoting " not in title.lower() and len(title) <= 170:
        return title

    from_text = _extract_caption_candidate(full_text)
    if from_text:
        return from_text

    body = normalize_ws(normalize_text(full_text or ""))
    match = re.search(r"([A-Z][A-Za-z0-9&.,'\-\s]{3,120}\s+v\.\s+[A-Z][A-Za-z0-9&.,'\-\s]{3,120})", body)
    if match:
        candidate = normalize_ws(match.group(1)).strip(" ,.;:")
        if candidate and len(candidate) <= 170:
            return candidate

    docket = normalize_ws(docket_value)
    if docket:
        return f"Central District Case {docket}"

    return "Central District Opinion"


def clean_main_text(text: str) -> str:
    cleaned = normalize_text(text)
    cleaned = cleaned.replace("**", "")

    low = cleaned.lower()
    if "<html" in low or "<!doctype" in low:
        cleaned = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", cleaned)
        cleaned = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", cleaned)
        cleaned = re.sub(r"(?is)<noscript[^>]*>.*?</noscript>", " ", cleaned)
        cleaned = re.sub(r"(?is)<head[^>]*>.*?</head>", " ", cleaned)
        cleaned = re.sub(r"(?is)<[^>]+>", " ", cleaned)
        cleaned = html_lib.unescape(cleaned)

    patterns = [
        r"\[Image\s*\d+:[^\]]*\]",
        r"\*\s*Image\s*\d+:[^\n]*",
        r"InSign\s*Up\s*\|\s*Image\s*\d+:[^\n]*",
        r"My Account\s+Log\s+In",
        r"Find\s+A\s+Lawyer",
        r"Ask\s+A\s+Lawyer",
        r"Justia\s*::\s*.*$",
        r"Download\s+PDF\s+[^\n]{0,120}",
        r"Justia Case Law",
        r"Additional Links",
        r"Toggle button Toggle",
        r"Get free summaries of new .*? opinions delivered to your inbox!",
        r"©\s*\d{4}\s*Justia",
    ]
    for pattern in patterns:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)

    cleaned = re.sub(r"Court Description:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()


def parse_cases(markdown_text: str) -> list[dict]:
    entry_pattern = re.compile(
        r"\[(?:\*\*)?(?P<title>.+?)(?:\*\*)?\]\((?P<url>https?://[^\)]+)\)\s*"
        r"(?:\*\*\s*)?Date:\s*(?:\*\*\s*)?(?P<date>[^\n\r]+?)\s*"
        r"(?:\*\*\s*)?Docket Number:\s*(?:\*\*\s*)?(?P<docket>[^\n\r\[]+)",
        flags=re.IGNORECASE,
    )

    cases: list[dict] = []
    seen = set()

    for idx, match in enumerate(entry_pattern.finditer(markdown_text), start=1):
        title = normalize_ws(match.group("title"))
        url = match.group("url").strip()
        date = normalize_ws(match.group("date"))
        docket = normalize_ws(match.group("docket"))

        key = (title.lower(), url)
        if key in seen:
            continue
        seen.add(key)

        cases.append(
            {
                "id": idx,
                "title": normalize_text(title),
                "date": date,
                "docket": docket,
                "url": url,
                "subjects": ["central-district"],
            }
        )

    if not cases:
        fallback_pattern = re.compile(
            r"\[(?P<title>[^\]]+?)\]\((?P<url>https?://law\.justia\.com/cases/federal/district-courts/california/cacdce/[^\)]+)\)"
            r"\s*Date:\s*(?P<date>[A-Za-z]+\s+\d{1,2},\s+\d{4})\s*Docket Number:\s*(?P<docket>[^\n\r\[]+)",
            flags=re.IGNORECASE,
        )

        for idx, match in enumerate(fallback_pattern.finditer(markdown_text), start=1):
            title = normalize_ws(match.group("title"))
            url = match.group("url").strip()
            date = normalize_ws(match.group("date"))
            docket = normalize_ws(match.group("docket"))

            key = (title.lower(), url)
            if key in seen:
                continue
            seen.add(key)

            cases.append(
                {
                    "id": idx,
                    "title": normalize_text(title),
                    "date": date,
                    "docket": docket,
                    "url": url,
                    "subjects": ["central-district"],
                }
            )

    if not cases:
        html_pattern = re.compile(
            r"<a\s+href=\"(?P<url>/cases/federal/district-courts/california/cacdce/[^\"]+)\"[^>]*class=\"case-name\"[^>]*>"
            r".*?<span>(?P<title>[^<]+)</span>.*?"
            r"Date:\s*</strong>\s*(?P<date>[A-Za-z]+\s+\d{1,2},\s+\d{4}).*?"
            r"Docket Number:\s*</strong>\s*(?P<docket>[^<\n\r]+)",
            flags=re.IGNORECASE | re.DOTALL,
        )

        for idx, match in enumerate(html_pattern.finditer(markdown_text), start=1):
            title = normalize_ws(match.group("title"))
            url = "https://law.justia.com" + match.group("url").strip()
            date = normalize_ws(match.group("date"))
            docket = normalize_ws(match.group("docket"))

            key = (title.lower(), url)
            if key in seen:
                continue
            seen.add(key)

            cases.append(
                {
                    "id": idx,
                    "title": normalize_text(title),
                    "date": date,
                    "docket": docket,
                    "url": url,
                    "subjects": ["central-district"],
                }
            )

    if not cases:
        html_loose_pattern = re.compile(
            r"<a[^>]+href=\"(?P<url>/cases/federal/district-courts/california/cacdce/[^\"]+)\"[^>]*>(?P<title>.*?)</a>"
            r"\s*Date:\s*(?P<date>[A-Za-z]+\s+\d{1,2},\s+\d{4})\s*Docket Number:\s*(?P<docket>[^<\n\r]+)",
            flags=re.IGNORECASE | re.DOTALL,
        )

        for idx, match in enumerate(html_loose_pattern.finditer(markdown_text), start=1):
            title_html = match.group("title")
            title = normalize_ws(html_lib.unescape(re.sub(r"<[^>]+>", " ", title_html)))
            title = normalize_ws(re.sub(r"\s+", " ", title))
            url = "https://law.justia.com" + match.group("url").strip()
            date = normalize_ws(match.group("date"))
            docket = normalize_ws(match.group("docket"))

            key = (title.lower(), url)
            if key in seen:
                continue
            seen.add(key)

            cases.append(
                {
                    "id": idx,
                    "title": normalize_text(title),
                    "date": date,
                    "docket": docket,
                    "url": url,
                    "subjects": ["central-district"],
                }
            )

    return cases


def _derive_title_from_cache(url: str, detail: dict) -> str:
    explicit_title = normalize_ws(normalize_text((detail or {}).get("title") or ""))
    if explicit_title and "quoting " not in explicit_title.lower() and len(explicit_title) <= 180:
        return explicit_title

    body = normalize_ws(normalize_text((detail or {}).get("text") or ""))
    title_match = re.search(r"([A-Z][A-Za-z0-9&.,'\-\s]{3,120}\s+v\.\s+[A-Z][A-Za-z0-9&.,'\-\s]{3,120})", body)
    if title_match:
        candidate = normalize_ws(title_match.group(1)).strip(" ,.;:")
        if candidate and "quoting " not in candidate.lower():
            return candidate

    citations = (detail or {}).get("citations") or {}
    case_items = citations.get("cases") or []
    for value in case_items:
        text = normalize_ws(normalize_text(value))
        if " v. " in text.lower() and len(text) <= 140:
            return text

    docket = ""
    dm = re.search(r"/cacdce/([^/]+)/", url)
    if dm:
        docket = dm.group(1)
    return f"Central District Opinion {docket}".strip()


def _derive_issue_date_from_docket(docket_value: str) -> str:
    text = normalize_ws(docket_value)
    if not text:
        return ""

    match_full = re.search(r"(?:^|\D)((?:19|20)\d{2})\s*cv\s*(\d+)", text, flags=re.IGNORECASE)
    if match_full:
        year = int(match_full.group(1))
        return f"{year:04d}-01-01"

    match = re.search(r"(?:^|\D)(\d{2})-(?:cv|cr|mc|bk|po)\b", text, flags=re.IGNORECASE)
    if not match:
        return ""

    yy = int(match.group(1))
    year = 2000 + yy if yy <= 50 else 1900 + yy
    return f"{year:04d}-01-01"


def _derive_year_seq_from_docket(docket_value: str) -> tuple[int, int]:
    text = normalize_ws(docket_value)
    if not text:
        return (0, 0)

    year = 0
    seq = 0

    year_match = re.search(r"(?:^|\D)((?:19|20)\d{2})\s*(?:cv|cr|mc|bk|po)", text, flags=re.IGNORECASE)
    if year_match:
        year = int(year_match.group(1))
    else:
        yy_match = re.search(r"(?:^|\D)(\d{2})-(?:cv|cr|mc|bk|po)\b", text, flags=re.IGNORECASE)
        if yy_match:
            yy = int(yy_match.group(1))
            year = 2000 + yy if yy <= 50 else 1900 + yy

    seq_match = re.search(r"(?:cv|cr|mc|bk|po)\s*(\d+)", text, flags=re.IGNORECASE)
    if seq_match:
        try:
            seq = int(seq_match.group(1))
        except Exception:
            seq = 0

    return (year, seq)


def _derive_sort_components(op: dict) -> tuple[str, int]:
    exact_date = parse_issue_date(op.get("date") or "") or parse_issue_date(op.get("issue_date") or "")
    year, seq = _derive_year_seq_from_docket(op.get("docket") or "")

    if exact_date:
        return exact_date, seq
    if year:
        return f"{year:04d}-00-00", seq
    return "", seq


def build_cases_from_cache(cache: dict) -> list[dict]:
    cases: list[dict] = []
    urls = sorted(cache.keys())
    for idx, url in enumerate(urls, start=1):
        detail = cache.get(url) if isinstance(cache.get(url), dict) else {}
        docket_match = re.search(r"/cacdce/([^/]+)/", url)
        docket = docket_match.group(1) if docket_match else ""
        title = _derive_title_from_cache(url, detail)
        date_value = normalize_ws((detail or {}).get("date") or "")
        issue_date = parse_issue_date((detail or {}).get("issue_date") or "") or parse_issue_date(date_value)

        cases.append(
            {
                "id": idx,
                "title": title,
                "date": date_value,
                "issue_date": issue_date,
                "docket": docket,
                "url": url,
                "subjects": ["central-district"],
            }
        )
    return cases


def extract_citations(text: str) -> dict:
    found = {"cases": [], "statutes": [], "rules": [], "regulations": []}
    if not text or len(text) < 100:
        return found

    for pattern in CASE_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            value = normalize_ws(match.group(0)).strip(" ,.;:")
            if value and value not in found["cases"]:
                found["cases"].append(value)

    for pattern in STATUTE_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            value = normalize_ws(match.group(0)).strip(" ,.;:")
            if value and value not in found["statutes"]:
                found["statutes"].append(value)

    for pattern in REGULATION_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            value = normalize_ws(match.group(0)).strip(" ,.;:")
            if value and value not in found["regulations"]:
                found["regulations"].append(value)

    for pattern in RULE_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            value = normalize_ws(match.group(0)).strip(" ,.;:")
            if value and value not in found["rules"]:
                found["rules"].append(value)

    return found


def extract_case_citations_from_text(text: str, limit: int = 40) -> list[str]:
    source = normalize_text(text or "")
    if not source:
        return []

    patterns = [
        r"([A-Z][A-Za-z0-9&.,'\-\s]{2,90}\s+v\.\s+[A-Z][A-Za-z0-9&.,'\-\s]{2,90},\s*\d+\s+(?:U\.S\.|F\.\s?Supp\.\s?\d*d?|F\.\s?\d+d|F\.\s?\d+th|S\.\s?Ct\.)\s+\d+(?:,\s*\d+)*)",
        r"(In\s+re\s+[A-Z][A-Za-z0-9&.,'\-\s]{2,100},\s*\d+\s+(?:U\.S\.|F\.\s?Supp\.\s?\d*d?|F\.\s?\d+d|F\.\s?\d+th|S\.\s?Ct\.)\s+\d+(?:,\s*\d+)*)",
        r"([A-Z][A-Za-z0-9&.,'\-\s]{2,90}\s+v\.\s+[A-Z][A-Za-z0-9&.,'\-\s]{2,90}\s*\([^\)]*(?:19|20)\d{2}[^\)]*\))",
    ]

    out: list[str] = []
    seen: set[str] = set()

    for pattern in patterns:
        for m in re.finditer(pattern, source):
            value = normalize_ws(m.group(1)).strip(" ,.;:")
            value = re.sub(r"^(?:See|Cf\.|But\s+see|Compare)\s+", "", value, flags=re.IGNORECASE)
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(value)
            if len(out) >= limit:
                return out

    return out


def extract_pdf_text(pdf_path: str, max_pages: int = 25) -> str:
    if not pdf_path or not os.path.exists(pdf_path) or PdfReader is None:
        return ""

    try:
        reader = PdfReader(pdf_path)
    except Exception:
        return ""

    chunks: list[str] = []
    pages = reader.pages[:max_pages]
    for page in pages:
        try:
            page_text = page.extract_text() or ""
        except Exception:
            page_text = ""
        if page_text:
            chunks.append(page_text)

    return normalize_ws(" ".join(chunks))[:120000]


def extract_pdf_metadata_date(pdf_path: str) -> str:
    if not pdf_path or not os.path.exists(pdf_path) or PdfReader is None:
        return ""
    try:
        reader = PdfReader(pdf_path)
        metadata = reader.metadata or {}
    except Exception:
        return ""

    raw_value = str(metadata.get("/CreationDate") or metadata.get("/ModDate") or "")
    match = re.search(r"D:(\d{4})(\d{2})(\d{2})", raw_value)
    if not match:
        return ""
    year, month, day = match.groups()
    return f"{year}-{month}-{day}"


def ensure_central_db(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS central_opinions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE NOT NULL,
            title TEXT,
            date TEXT,
            issue_date TEXT,
            docket TEXT,
            pdf_url TEXT,
            local_pdf_path TEXT,
            text TEXT,
            citations TEXT,
            subjects TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_central_issue_date ON central_opinions(issue_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_central_docket ON central_opinions(docket)")
    conn.commit()


def _safe_filename_from_case(item: dict) -> str:
    docket = normalize_ws(item.get("docket") or "")
    if docket:
        name = re.sub(r"[^A-Za-z0-9._-]", "_", docket)
    else:
        name = re.sub(r"[^A-Za-z0-9._-]", "_", normalize_ws(item.get("title") or "central_opinion"))
    source = normalize_ws(item.get("url") or item.get("pdf_url") or name)
    digest = hashlib.sha1(source.encode("utf-8", errors="ignore")).hexdigest()[:10]
    return f"{(name[:110] or 'central_opinion')}_{digest}.pdf"


def local_pdf_path_for_case(item: dict) -> str:
    return os.path.join(PDF_DIR, _safe_filename_from_case(item))


def sync_local_pdf_paths(cases: list[dict]) -> None:
    for item in cases:
        expected_local_pdf = local_pdf_path_for_case(item)
        item["local_pdf_path"] = expected_local_pdf if os.path.exists(expected_local_pdf) else ""


def download_central_pdf(pdf_url: str, file_path: str, timeout: int = 30) -> bool:
    if not pdf_url:
        return False
    try:
        req = urllib.request.Request(pdf_url, headers={"User-Agent": "Mozilla/5.0"})
        data = urllib.request.urlopen(req, timeout=timeout).read()
        if not data:
            return False
        with open(file_path, "wb") as f:
            f.write(data)
        return True
    except Exception:
        return False


def persist_to_database(cases: list[dict]) -> tuple[int, int]:
    os.makedirs(PDF_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    ensure_central_db(conn)
    cur = conn.cursor()

    upserts = 0
    downloaded = 0
    download_attempts = 0

    now = datetime.utcnow().isoformat(timespec="seconds")

    for item in cases:
        pdf_url = normalize_ws(item.get("pdf_url") or "")
        local_pdf_path = ""

        if pdf_url and download_attempts < MAX_PDF_DOWNLOAD_PER_RUN:
            filename = _safe_filename_from_case(item)
            local_path_abs = os.path.join(PDF_DIR, filename)
            local_pdf_path = local_path_abs

            if not os.path.exists(local_path_abs):
                download_attempts += 1
                if download_central_pdf(pdf_url, local_path_abs):
                    downloaded += 1

            if not os.path.exists(local_path_abs):
                local_pdf_path = ""

        citations_json = json.dumps(item.get("citations") or {}, ensure_ascii=False)
        subjects_text = ";".join(item.get("subjects") or ["central-district"])

        cur.execute(
            """
            INSERT INTO central_opinions (
                url, title, date, issue_date, docket, pdf_url, local_pdf_path, text, citations, subjects, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                title = excluded.title,
                date = excluded.date,
                issue_date = excluded.issue_date,
                docket = excluded.docket,
                pdf_url = excluded.pdf_url,
                local_pdf_path = excluded.local_pdf_path,
                text = excluded.text,
                citations = excluded.citations,
                subjects = excluded.subjects,
                updated_at = excluded.updated_at
            """,
            (
                item.get("url") or "",
                item.get("title") or "",
                item.get("date") or "",
                item.get("issue_date") or "",
                item.get("docket") or "",
                pdf_url,
                local_pdf_path,
                item.get("text") or "",
                citations_json,
                subjects_text,
                now,
                now,
            ),
        )
        upserts += 1

    conn.commit()
    conn.close()
    return upserts, downloaded


def load_cache() -> dict:
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_cache(cache: dict) -> None:
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def extract_main_text(markdown_text: str) -> str:
    body = markdown_text
    if "Markdown Content:" in body:
        body = body.split("Markdown Content:", 1)[1]
    body = re.sub(r"\[([^\]]+)\]\((https?://[^\)]+)\)", r"\1", body)
    body = body.split("Additional Links", 1)[0]
    body = clean_main_text(body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    body = normalize_ws(body)
    return body


def fetch_case_details(case_url: str) -> dict:
    mirrored = mirror_case_url(case_url)
    text = fetch_markdown(mirrored, timeout=12)
    if not text:
        return {"text": "", "pdf_url": "", "citations": {"cases": [], "statutes": [], "rules": [], "regulations": []}}
    body = extract_main_text(text)
    cites = extract_citations(body)

    pdf_url = ""
    pdf_patterns = [
        r'href="(?P<url>//cases\.justia\.com/[^"\s>]+\.pdf(?:\?[^"\s>]*)?)"',
        r'href="(?P<url>https?://[^"\s>]+\.pdf(?:\?[^"\s>]*)?)"',
        r'href="(?P<url>/[^"\s>]+\.pdf(?:\?[^"\s>]*)?)"',
    ]
    for pattern in pdf_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            pdf_url = normalize_central_url(match.group("url"))
            break

    return {
        "text": body[:80000],
        "pdf_url": pdf_url,
        "citations": cites,
    }


def parse_issue_date(value: str) -> str:
    text = normalize_ws(value)
    if not text:
        return ""

    candidates = [text, text.replace(".", "")]
    formats = [
        "%B %d, %Y",
        "%b %d, %Y",
        "%m/%d/%Y",
        "%Y-%m-%d",
    ]

    for candidate in candidates:
        for fmt in formats:
            try:
                return datetime.strptime(candidate, fmt).strftime("%Y-%m-%d")
            except Exception:
                continue
    return ""


def derive_issue_date_from_text(text: str | None) -> str:
    source = normalize_text(text or "")
    if not source:
        return ""

    month_name = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"

    patterns = [
        rf"\b(?:Dated|Date|Filed|Signed)\s*[:\-]?\s*({month_name}\s+\d{{1,2}},\s*\d{{4}})",
        rf"\b(?:on|dated|filed|signed)\s+({month_name}\s+\d{{1,2}},\s*\d{{4}})",
        r"\b(?:on|dated|filed|signed)\s+(\d{1,2}/\d{1,2}/\d{4})",
        r"\b(?:Dated|Date|Filed|Signed)\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{4})",
    ]

    for pattern in patterns:
        match = re.search(pattern, source, flags=re.IGNORECASE)
        if not match:
            continue
        parsed = parse_issue_date(match.group(1))
        if parsed:
            return parsed

    return ""


def export_data() -> int:
    source_used = "none"
    markdown_text = ""
    for _ in range(3):
        markdown_text = fetch_markdown(LIST_SOURCE_URL)
        if markdown_text:
            break
    if markdown_text and not is_cloudflare_challenge(markdown_text):
        cases = parse_cases(markdown_text)
        if cases:
            source_used = "justia-http"
    else:
        cases = []

    if not cases:
        dump_cases = load_listing_from_browser_dump()
        if dump_cases:
            cases = dump_cases
            source_used = "browser-dump"

    cache = load_cache()
    if not cases and cache:
        cases = build_cases_from_cache(cache)
        if cases:
            source_used = "cache"

    if not cases and os.path.exists(JSON_FILE):
        with open(JSON_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        loaded_cases = loaded if isinstance(loaded, list) else []
        if loaded_cases:
            cases = loaded_cases
            source_used = "json"

    if not cases:
        raise RuntimeError("Could not build Central District dataset from source, local JSON, or cache")

    for idx, item in enumerate(cases, start=1):
        item.setdefault("id", idx)
        item.setdefault("title", "Untitled")
        item.setdefault("date", "")
        item.setdefault("docket", "")
        item.setdefault("url", "")
        item.setdefault("subjects", ["central-district"])
    updated = 0
    attempted = 0

    for item in cases:
        key = item["url"]
        detail = cache.get(key)
        previous_detail = detail if isinstance(detail, dict) else {}
        needs_refresh = (
            not isinstance(detail, dict)
            or not detail.get("text")
            or not detail.get("pdf_url")
        )
        if needs_refresh:
            if attempted >= MAX_FETCH_PER_RUN:
                detail = {"text": "", "pdf_url": "", "citations": {"cases": [], "statutes": [], "rules": [], "regulations": []}}
            else:
                attempted += 1
            try:
                if attempted <= MAX_FETCH_PER_RUN:
                    detail = fetch_case_details(key)
                    if (
                        (not (detail or {}).get("text"))
                        and (not (detail or {}).get("pdf_url"))
                        and (previous_detail.get("text") or previous_detail.get("pdf_url"))
                    ):
                        detail = previous_detail
                    cache[key] = detail
                    if detail.get("text"):
                        updated += 1
            except Exception:
                if previous_detail.get("text") or previous_detail.get("pdf_url"):
                    detail = previous_detail
                else:
                    detail = {"text": "", "pdf_url": "", "citations": {"cases": [], "statutes": [], "rules": [], "regulations": []}}

        detail["title"] = item.get("title") or detail.get("title") or ""
        detail["docket"] = item.get("docket") or detail.get("docket") or ""
        detail["date"] = item.get("date") or detail.get("date") or ""
        detail_issue = parse_issue_date(item.get("issue_date") or "") or parse_issue_date(item.get("date") or "")
        if detail_issue:
            detail["issue_date"] = detail_issue
        cache[key] = detail

        full_text = clean_main_text(detail.get("text") or "")
        if is_noisy_page_text(full_text):
            full_text = ""
        item["title"] = clean_display_title(item.get("title"), full_text, item.get("docket"))
        raw_citations = detail.get("citations") or {"cases": [], "statutes": [], "rules": [], "regulations": []}
        extracted = extract_citations(full_text) if full_text else {"cases": [], "statutes": [], "rules": [], "regulations": []}
        reporter_cases = extract_case_citations_from_text(full_text, limit=50)

        pdf_text = ""
        pdf_meta_date = ""
        expected_local_pdf = local_pdf_path_for_case(item)
        if (not SKIP_LOCAL_PDF_READ) and os.path.exists(expected_local_pdf):
            pdf_text = extract_pdf_text(expected_local_pdf, max_pages=25)
            pdf_meta_date = extract_pdf_metadata_date(expected_local_pdf)

        pdf_reporter_cases = extract_case_citations_from_text(pdf_text, limit=80) if pdf_text else []

        if full_text or pdf_text:
            case_candidates = pdf_reporter_cases + reporter_cases + (extracted.get("cases") or []) + (raw_citations.get("cases") or [])
            statute_candidates = (extracted.get("statutes") or []) + (raw_citations.get("statutes") or [])
            rule_candidates = (extracted.get("rules") or []) + (raw_citations.get("rules") or [])
            regulation_candidates = (extracted.get("regulations") or []) + (raw_citations.get("regulations") or [])
        else:
            case_candidates = []
            statute_candidates = []
            rule_candidates = []
            regulation_candidates = []

        cleaned_cases = clean_case_citations(case_candidates, item["title"])
        authorities_cases = dedupe_normalized([item["title"], *cleaned_cases], limit=30)

        preview = build_preview(full_text, item["date"], item["docket"])

        merged_citations = {
            "cases": dedupe_normalized(cleaned_cases, limit=30),
            "statutes": dedupe_normalized(statute_candidates, limit=30),
            "rules": dedupe_normalized(rule_candidates, limit=30),
            "regulations": dedupe_normalized(regulation_candidates, limit=30),
        }

        exact_issue_date = parse_issue_date(item.get("date") or "")
        if not exact_issue_date and pdf_meta_date:
            exact_issue_date = parse_issue_date(pdf_meta_date)
        if not exact_issue_date:
            exact_issue_date = derive_issue_date_from_text(full_text)
        if not exact_issue_date and pdf_text:
            exact_issue_date = derive_issue_date_from_text(pdf_text)
        if not exact_issue_date:
            year_guess, _ = _derive_year_seq_from_docket(item.get("docket") or "")
            if year_guess:
                exact_issue_date = f"{year_guess:04d}-00-00"

        item["issue_date"] = exact_issue_date
        if not item.get("date") and exact_issue_date and not exact_issue_date.endswith("-00-00"):
            item["date"] = exact_issue_date
        existing_pdf_url = normalize_ws(item.get("pdf_url") or "")
        fetched_pdf_url = normalize_ws(detail.get("pdf_url") or "")
        item["pdf_url"] = fetched_pdf_url or existing_pdf_url

        existing_local_pdf = normalize_ws(item.get("local_pdf_path") or "")
        if os.path.exists(expected_local_pdf):
            item["local_pdf_path"] = expected_local_pdf
        elif existing_local_pdf and os.path.exists(existing_local_pdf):
            item["local_pdf_path"] = existing_local_pdf
        else:
            item["local_pdf_path"] = ""
        item["preview"] = preview
        item["text"] = full_text
        item["citations"] = merged_citations
        item["authorities"] = {
            "cases": authorities_cases[:30],
            "statutes": (merged_citations.get("statutes") or [])[:30],
            "rules": (merged_citations.get("rules") or [])[:30],
            "regulations": (merged_citations.get("regulations") or [])[:30],
            "constitutional": [],
        }

    cases = collapse_duplicate_opinions(cases)

    cases.sort(
        key=lambda op: (
            _derive_sort_components(op)[0],
            _derive_sort_components(op)[1],
            op.get("id") or 0,
        ),
        reverse=True,
    )

    if updated:
        save_cache(cache)

    upserts, downloaded = persist_to_database(cases)

    sync_local_pdf_paths(cases)

    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(cases, f, ensure_ascii=False, indent=2)

    print(f"[OK] Exported {len(cases)} Central District opinions ({updated} fetched/updated this run, attempted {attempted}, cap {MAX_FETCH_PER_RUN})")
    print(f"[OK] Listing source used: {source_used}")
    print(f"[OK] Database upserts: {upserts} into central_opinions | PDFs downloaded this run: {downloaded}")
    return len(cases)


def create_html(count: int) -> None:
    with open(JSON_FILE, "r", encoding="utf-8") as f:
        embedded_json = f.read().replace("</script", "<\\/script")

    html = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Central District (C.D. Cal.) Opinions — 2026</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: "Times New Roman", Times, serif;
            background: #f5f7fa;
            min-height: 100vh;
            padding: 20px;
            color: #333;
        }
        .container {
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            border: 2px solid #003da5;
            padding: 30px;
            box-shadow: 0 2px 8px rgba(0,61,165,0.1);
        }
        h1 {
            color: #003da5;
            margin-bottom: 5px;
            text-align: center;
            font-size: 28px;
        }
        .stats {
            text-align: center;
            color: #0066cc;
            margin-bottom: 25px;
            font-size: 16px;
        }

        .update-controls {
            text-align: center;
            margin-bottom: 14px;
        }

        .update-btn {
            background: #0a6d3a;
            color: #fff;
            border: 1px solid #0a6d3a;
            padding: 8px 16px;
            font-size: 13px;
            font-weight: bold;
            cursor: pointer;
        }

        .update-btn:hover {
            background: #085b31;
        }

        .update-status {
            margin-left: 10px;
            font-size: 12px;
            color: #4a628b;
        }

        .theme-btn {
            margin-left: 8px;
            background: #1e293b;
            color: #fff;
            border: 1px solid #1e293b;
            padding: 8px 12px;
            font-size: 12px;
            font-weight: bold;
            cursor: pointer;
        }

        .theme-btn:hover {
            background: #0f172a;
        }

        .view-switch { display: flex; justify-content: center; gap: 8px; margin-bottom: 14px; }
        .view-link { border: 1px solid #0066cc; color: #0066cc; padding: 6px 10px; font-size: 13px; font-weight: bold; text-decoration: none; background: #fff; }
        .view-link.active { background: #0066cc; color: #fff; }

        .search-box { display: flex; gap: 10px; margin-bottom: 20px; flex-wrap: wrap; }
        input[type="text"], select {
            padding: 10px;
            border: 1px solid #0066cc;
            font-size: 14px;
            font-family: "Times New Roman", Times, serif;
            flex: 1;
            min-width: 200px;
        }
        input[type="text"]:focus, select:focus { outline: none; border: 2px solid #003da5; background-color: #f0f4ff; }
        button { padding: 10px 20px; background: #0066cc; color: white; border: none; cursor: pointer; font-weight: bold; font-family: "Times New Roman", Times, serif; font-size: 14px; }
        button:hover { background: #003da5; }

        .results-count { color: #0066cc; margin-bottom: 15px; font-size: 14px; font-weight: bold; }
        .layout { display: grid; grid-template-columns: 34% 66%; gap: 16px; min-height: 70vh; }
        .left-pane { border: 1px solid #c7d9f7; background: #fbfdff; overflow-y: auto; max-height: 74vh; }
        .opinion-item { border-bottom: 1px solid #e7eefc; padding: 12px; cursor: pointer; }
        .opinion-item:hover { background: #eef4ff; }
        .opinion-item.active { background: #e3edff; border-left: 4px solid #0066cc; }
        .inline-detail-mobile { display: none; }
        .opinion-item-title { font-size: 15px; color: #003da5; font-weight: bold; margin-bottom: 8px; }
        .opinion-item-date { font-size: 12px; color: #4a628b; margin-bottom: 6px; font-weight: bold; }
        .opinion-item-preview { font-size: 12px; color: #555; line-height: 1.35; }
        .subject-badge {
            display: inline-block;
            background: #e6f0ff;
            color: #003da5;
            padding: 2px 6px;
            margin-right: 5px;
            font-size: 12px;
        }

        .right-pane { border: 1px solid #c7d9f7; padding: 14px; overflow-y: auto; max-height: 74vh; background: white; }
        .right-title { color: #003da5; font-size: 20px; margin-bottom: 10px; }
        .authority-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
            margin-bottom: 14px;
        }
        .authority-box {
            border: 1px solid #d8e5fb;
            background: #f8fbff;
            padding: 10px;
            min-height: 90px;
        }
        .authority-head {
            font-size: 13px;
            color: #003da5;
            font-weight: bold;
            margin-bottom: 6px;
        }
        .authority-list {
            font-size: 12px;
            color: #333;
            line-height: 1.35;
            list-style: none;
        }
        .authority-list li { margin-bottom: 4px; }

        .source-panel {
            border: 1px solid #c7d9f7;
            background: #f5f7fa;
            padding: 16px;
        }
        .source-frame {
            width: 100%;
            height: 460px;
            border: 1px solid #c7d9f7;
            background: #f5f7fa;
            margin-top: 10px;
        }
        .empty-state { color: #666; text-align: center; padding: 30px; }

        .highlight { background-color: #ffff99; font-weight: bold; }
        .no-results { text-align: center; color: #666; padding: 40px; font-size: 16px; }
        .preview-text { color: #666; font-size: 13px; margin-bottom: 10px; line-height: 1.5; }

        a { color: #0066cc; text-decoration: none; font-size: 13px; }
        a:hover { text-decoration: underline; }

        body.night {
            background: #0b1220;
            color: #e5e7eb;
        }
        body.night .container {
            background: #111827;
            border-color: #1d4ed8;
            box-shadow: 0 2px 8px rgba(0,0,0,0.35);
        }
        body.night h1,
        body.night .right-title,
        body.night .authority-head,
        body.night .opinion-item-title,
        body.night .results-count {
            color: #93c5fd;
        }
        body.night .stats,
        body.night .opinion-item-date,
        body.night .update-status,
        body.night .preview-text,
        body.night .authority-list,
        body.night .empty-state,
        body.night .no-results {
            color: #cbd5e1;
        }
        body.night .left-pane,
        body.night .right-pane,
        body.night .source-panel,
        body.night .authority-box {
            background: #0f172a;
            border-color: #334155;
        }
        body.night .opinion-item { border-bottom-color: #334155; }
        body.night .opinion-item:hover { background: #1e293b; }
        body.night .opinion-item.active { background: #1f2a44; border-left-color: #60a5fa; }
        body.night .subject-badge {
            background: #1e3a8a;
            color: #dbeafe;
        }
        body.night input[type="text"],
        body.night select {
            background: #0f172a;
            color: #e5e7eb;
            border-color: #3b82f6;
        }
        body.night input[type="text"]:focus,
        body.night select:focus {
            background: #111827;
            border-color: #60a5fa;
        }
        body.night a { color: #93c5fd; }

        @media (max-width: 1080px) {
            .layout { grid-template-columns: 1fr; }
            .left-pane, .right-pane { max-height: none; }
            .right-pane { display: none; }
            .inline-detail-mobile {
                display: block;
                margin-top: 10px;
                border-top: 1px solid #c7d9f7;
                padding-top: 10px;
            }
            .inline-detail-mobile .source-frame { height: 360px; }
            .authority-grid { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Atlas Law Opinions</h1>
        <div class="view-switch">
            <a class="view-link" href="/supreme_opinions_index.html">U.S. Supreme Court</a>
            <a class="view-link" href="/opinions_index.html">Ninth Circuit</a>
            <a class="view-link active" href="/central_opinions_index.html">Central District (C.D. Cal.)</a>
        </div>
        <div class="stats"><span id="total-count">""" + str(count) + """</span> opinions | Searchable</div>
        <div class="update-controls">
            <button id="update-all-btn" class="update-btn" onclick="runAtlasRefresh()">Update All Courts</button>
            <span id="update-status" class="update-status">Idle</span>
            <button id="theme-toggle-btn" class="theme-btn" onclick="toggleNightVision()">Night Vision: Off</button>
        </div>

        <div class="search-box">
            <input type="text" id="search-input" placeholder="Search by case name, docket, date..." autofocus>
            <select id="subject-filter">
                <option value="">All Subjects</option>
            </select>
            <button onclick="search()">Search</button>
            <button onclick="clearSearch()">Clear</button>
        </div>

        <div class="results-count" id="results-count"></div>

        <div class="layout">
            <div id="results" class="left-pane"></div>
            <div id="detail" class="right-pane"><div class="empty-state">Select an opinion from the left list.</div></div>
        </div>
    </div>

    <script>
        const EMBEDDED_OPINIONS = __EMBEDDED_OPINIONS__;
        let allOpinions = [];
        let filteredOpinions = [];
        let selectedOpinionId = null;

        function applyTheme(theme) {
            const isNight = theme === 'night';
            document.body.classList.toggle('night', isNight);
            const btn = document.getElementById('theme-toggle-btn');
            if (btn) btn.textContent = isNight ? 'Night Vision: On' : 'Night Vision: Off';
        }

        function toggleNightVision() {
            const current = localStorage.getItem('atlas-central-theme') || 'day';
            const next = current === 'night' ? 'day' : 'night';
            localStorage.setItem('atlas-central-theme', next);
            applyTheme(next);
        }

        async function refreshUpdateStatus() {
            const el = document.getElementById('update-status');
            const btn = document.getElementById('update-all-btn');
            if (!el || !btn) return;
            try {
                const resp = await fetch('/api/refresh_status?t=' + Date.now(), { cache: 'no-store' });
                const data = await resp.json();
                if (data.running) {
                    el.textContent = 'Updating…';
                    btn.disabled = true;
                    btn.textContent = 'Updating…';
                } else {
                    const added = Number(data?.refresh_summary?.total_added || 0);
                    if (data.exit_code === 0) {
                        el.textContent = `Last update: +${added} cases`;
                    } else if (data.exit_code !== null && data.exit_code !== undefined) {
                        el.textContent = 'Last update failed';
                    } else {
                        el.textContent = 'Idle';
                    }
                    btn.disabled = false;
                    btn.textContent = 'Update All Courts';
                }
            } catch (_) {
                el.textContent = 'Status unavailable';
            }
        }

        async function runAtlasRefresh() {
            const el = document.getElementById('update-status');
            const btn = document.getElementById('update-all-btn');
            if (btn) {
                btn.disabled = true;
                btn.textContent = 'Starting…';
            }
            try {
                const resp = await fetch('/api/run_refresh', { method: 'POST' });
                const data = await resp.json();
                if (!resp.ok || !data.ok) {
                    if (el) el.textContent = data.message || 'Unable to start update';
                } else if (el) {
                    el.textContent = 'Updating…';
                }
            } catch (_) {
                if (el) el.textContent = 'Unable to start update';
            }
            refreshUpdateStatus();
        }

        function normalizeOpinions(items) {
            return (Array.isArray(items) ? items : []).map(op => ({
                ...op,
                id: op.id,
                title: op.title || 'Untitled Opinion',
                date: op.date || '',
                issue_date: op.issue_date || '',
                docket: op.docket || '',
                text: op.text || '',
                preview: op.preview || (op.text || '').slice(0, 300),
                pdf_url: op.pdf_url || '',
                local_pdf_path: op.local_pdf_path || '',
                subjects: Array.isArray(op.subjects) ? op.subjects.filter(Boolean) : [],
                authorities: op.authorities || {},
                citations: op.citations || {}
            }));
        }

        allOpinions = normalizeOpinions(EMBEDDED_OPINIONS);
        applyTheme(localStorage.getItem('atlas-central-theme') || 'day');
        populateSubjectFilter();
        displayAll();
        refreshUpdateStatus();
        setInterval(refreshUpdateStatus, 15000);

        function populateSubjectFilter() {
            const subjects = new Set();
            allOpinions.forEach(op => {
                (op.subjects || []).forEach(s => {
                    const text = String(s || '').trim();
                    if (text) subjects.add(text);
                });
            });

            const select = document.getElementById('subject-filter');
            while (select.options.length > 1) {
                select.remove(1);
            }
            Array.from(subjects).sort().forEach(subject => {
                const opt = document.createElement('option');
                opt.value = subject;
                opt.textContent = subject;
                select.appendChild(opt);
            });
        }

        function search() {
            const q = document.getElementById('search-input').value.toLowerCase().trim();
            const subject = document.getElementById('subject-filter').value;

            if (!q && !subject) {
                displayAll();
                return;
            }

            filteredOpinions = allOpinions.filter(op => {
                const title = String(op.title || '').toLowerCase();
                const docket = String(op.docket || '').toLowerCase();
                const date = String(op.date || '').toLowerCase();
                const issueDate = String(op.issue_date || '').toLowerCase();
                const matchesQuery = !q || title.includes(q) || docket.includes(q) || date.includes(q) || issueDate.includes(q);
                const matchesSubject = !subject || (op.subjects || []).includes(subject);
                return matchesQuery && matchesSubject;
            });

            displayResults();
        }

        function displayAll() {
            filteredOpinions = allOpinions;
            displayResults();
        }

        function clearSearch() {
            document.getElementById('search-input').value = '';
            document.getElementById('subject-filter').value = '';
            selectedOpinionId = null;
            displayAll();
        }

        function displayResults() {
            const results = document.getElementById('results');
            document.getElementById('results-count').textContent = 'Results: ' + filteredOpinions.length;
            const query = document.getElementById('search-input').value.toLowerCase().trim();
            const mobileView = isMobileView();

            if (!filteredOpinions.length) {
                results.innerHTML = '<div class="no-results">No opinions found.</div>';
                document.getElementById('detail').innerHTML = '<div class="empty-state">No opinion selected.</div>';
                return;
            }

            results.innerHTML = filteredOpinions.map(op => {
                const inlineDetail = mobileView && selectedOpinionId === op.id
                    ? `<div class="inline-detail-mobile">${buildDetailHtml(op)}</div>`
                    : '';
                const shownDate = escapeHtml(op.issue_date || op.date || 'Unknown');
                return `
                <div class="opinion-item ${selectedOpinionId === op.id ? 'active' : ''}" onclick="selectOpinion(${op.id})">
                    <div class="opinion-item-title">${query ? highlightText(escapeHtml(op.title || 'Untitled'), query) : escapeHtml(op.title || 'Untitled')}</div>
                    <div class="opinion-item-date">Issued: ${shownDate} | Docket: ${escapeHtml(op.docket || 'N/A')}</div>
                    <div class="opinion-item-preview">${query ? highlightText(escapeHtml(op.preview || ''), query) : escapeHtml(op.preview || '')}</div>
                    <div style="margin-top:6px;">
                        ${(op.subjects || []).slice(0, 4).map(s => '<span class="subject-badge">' + escapeHtml(s) + '</span>').join('')}
                    </div>
                    ${inlineDetail}
                </div>
            `;
            }).join('');

            const selected = filteredOpinions.find(op => op.id === selectedOpinionId);
            if (selected) {
                if (!mobileView) {
                    renderDetail(selected);
                }
            } else {
                selectOpinion(filteredOpinions[0].id);
            }
        }

        function isMobileView() {
            return window.matchMedia('(max-width: 1080px)').matches;
        }

        function selectOpinion(id) {
            selectedOpinionId = id;
            const op = filteredOpinions.find(x => x.id === id) || allOpinions.find(x => x.id === id);
            if (!op) return;
            displayResults();
            if (!isMobileView()) {
                renderDetail(op);
            }
        }

        function findOpinionById(opinionId) {
            if (opinionId === null || opinionId === undefined) return null;
            const target = String(opinionId);
            return filteredOpinions.find(op => String(op.id) === target) || allOpinions.find(op => String(op.id) === target) || null;
        }

        function proxiedPdfUrl(url) {
            const value = String(url || '').trim();
            if (!value) return '';
            return `/api/pdf?url=${encodeURIComponent(value)}`;
        }

        function deriveJustiaPdfFromOpinionUrl(url) {
            const value = String(url || '').trim();
            if (!value) return '';

            try {
                const parsed = new URL(value, window.location.origin);
                const host = (parsed.hostname || '').toLowerCase();
                let path = parsed.pathname || '';
                if (!host.includes('law.justia.com')) return '';
                if (!path.includes('/cases/federal/district-courts/california/cacdce/')) return '';

                path = path.replace(/\/download\/?$/i, '/');
                const normalizedPath = path.endsWith('/') ? path : `${path}/`;
                return `https://cases.justia.com${normalizedPath}0.pdf`;
            } catch {
                return '';
            }
        }

        function buildPdfEmbed(url, title = 'Central District PDF') {
            const rawUrl = String(url || '').trim();
            if (!rawUrl) return '';
            const escapedRawUrl = escapeHtml(rawUrl);
            const escapedTitle = escapeHtml(title);
            return `<object class="source-frame" data="${escapedRawUrl}" type="application/pdf"><iframe class="source-frame" src="${escapedRawUrl}" title="${escapedTitle}"></iframe></object>
                    <div style="margin-top:8px;"><a href="${escapedRawUrl}" target="_blank">Open original PDF in new tab</a></div>`;
        }

        function buildDetailHtml(op) {
            const safeUrl = String(op.url || '#');
            const authorities = op.authorities || {};
            const citations = op.citations || {};
            const caseItems = (authorities.cases && authorities.cases.length) ? authorities.cases : (citations.cases || []);
            const statuteItems = (authorities.statutes && authorities.statutes.length) ? authorities.statutes : (citations.statutes || []);
            const ruleItems = (authorities.rules && authorities.rules.length) ? authorities.rules : (citations.rules || []);
            const regulationItems = (authorities.regulations && authorities.regulations.length) ? authorities.regulations : (citations.regulations || []);
            const preferredPdf = op.local_pdf_path
                ? `/api/local_pdf?path=${encodeURIComponent(op.local_pdf_path)}`
                : '';
            const remotePdf = proxiedPdfUrl(op.pdf_url || '');
            const derivedPdf = proxiedPdfUrl(deriveJustiaPdfFromOpinionUrl(safeUrl));
            const alternatePdf = remotePdf || derivedPdf;
            const pdfToEmbed = preferredPdf || alternatePdf;
            const embeddedHtml = pdfToEmbed
                ? buildPdfEmbed(pdfToEmbed, 'Central District PDF')
                : '<div class="preview-text" style="margin-top:12px;">No PDF or source page URL available for this opinion yet.</div>';

            return `
                <div class="right-title">${escapeHtml(op.title || 'Untitled')}</div>
                <div class="preview-text">Issued: ${escapeHtml(op.issue_date || op.date || 'Unknown')} | Docket: ${escapeHtml(op.docket || 'N/A')}</div>
                <div style="margin-bottom:10px;"><a href="${safeUrl}" target="_blank">View source opinion page</a></div>
                <div class="authority-grid">
                    <div class="authority-box">
                        <div class="authority-head">Cases</div>
                        ${listItems(caseItems, 'cases')}
                    </div>
                    <div class="authority-box">
                        <div class="authority-head">Statutes</div>
                        ${listItems(statuteItems, 'statutes')}
                    </div>
                    <div class="authority-box">
                        <div class="authority-head">Rules</div>
                        ${listItems(ruleItems, 'rules')}
                    </div>
                    <div class="authority-box">
                        <div class="authority-head">Regulations / Constitutional</div>
                        ${listItems(regulationItems, 'regulations')}
                    </div>
                </div>
                <div class="authority-head" style="margin-bottom:8px;">Original Opinion Page</div>
                <div class="source-panel">
                    <div class="preview-text">PDF mode: this panel uses local PDF when available, otherwise remote PDF, otherwise a derived Justia direct PDF URL.</div>
                    <a href="${safeUrl}" target="_blank">Open source opinion page in new tab</a>
                    ${preferredPdf ? `<div style="margin-top:8px;"><a href="${escapeHtml(preferredPdf)}" target="_blank">Open local PDF in new tab</a></div>` : ''}
                    ${(!preferredPdf && alternatePdf) ? `<div style="margin-top:8px;"><a href="${escapeHtml(alternatePdf)}" target="_blank">Open PDF in new tab</a></div>` : ''}
                    ${embeddedHtml}
                </div>
            `;
        }

        function ruleLinkFor(value) {
            const text = String(value || '').replace(/\s+/g, ' ').trim();
            const frap = text.match(/Fed\.\s*R\.\s*App\.\s*P\.\s*(\d+)/i);
            if (frap) return `https://www.law.cornell.edu/rules/frap/rule_${frap[1]}`;
            const frcp = text.match(/Fed\.\s*R\.\s*Civ\.\s*P\.\s*(\d+)/i);
            if (frcp) return `https://www.law.cornell.edu/rules/frcp/rule_${frcp[1]}`;
            const frcrp = text.match(/Fed\.\s*R\.\s*Crim\.\s*P\.\s*(\d+)/i);
            if (frcrp) return `https://www.law.cornell.edu/rules/frcrmp/rule_${frcrp[1]}`;
            const frbp = text.match(/Fed\.\s*R\.\s*Bankr\.\s*P\.\s*(\d+)/i);
            if (frbp) return `https://www.law.cornell.edu/rules/frbp/rule_${frbp[1]}`;
            const fre = text.match(/Fed\.\s*R\.\s*Evid\.\s*(\d+)/i);
            if (fre) return `https://www.law.cornell.edu/rules/fre/rule_${fre[1]}`;
            const bare = text.match(/^Rule\s*(\d+)/i);
            if (bare) return `https://www.law.cornell.edu/rules/frcp/rule_${bare[1]}`;
            return null;
        }

        function statuteLinkFor(value) {
            const text = String(value || '').replace(/(?:Ã‚)?Â§/g, '§').replace(/\s+/g, ' ').trim();
            const usc = text.match(/(\d+)\s*U\.?\s*S\.?\s*C\.?\s*§+\s*([0-9A-Za-z._-]+)/i);
            if (usc) return `https://www.law.cornell.edu/uscode/text/${usc[1]}/${usc[2]}`;
            return null;
        }

        function regulationLinkFor(value) {
            const text = String(value || '').replace(/(?:Ã‚)?Â§/g, '§').replace(/\s+/g, ' ').trim();
            const cfr = text.match(/(\d+)\s*C\.?\s*F\.?\s*R\.?\s*§+\s*([0-9]+(?:\.[0-9A-Za-z-]+)*)/i);
            if (cfr) return `https://www.ecfr.gov/current/title-${cfr[1]}/section-${cfr[2]}`;
            return null;
        }

        function caseLinkFor(value) {
            const citation = String(value || '').trim();
            const params = new URLSearchParams({ citation });
            return `/api/resolve_case?${params.toString()}`;
        }

        function listItems(items, category = '') {
            if (!items || items.length === 0) {
                return '<div class="preview-text">None</div>';
            }

            const rendered = items.slice(0, 50).map(i => {
                const text = String(i || '');
                const escaped = escapeHtml(text);
                let link = null;

                if (category === 'cases') link = caseLinkFor(text);
                if (category === 'statutes') link = statuteLinkFor(text);
                if (category === 'rules') link = ruleLinkFor(text);
                if (category === 'regulations') link = regulationLinkFor(text);

                if (link) return `<li><a href="${link}" target="_blank">${escaped}</a></li>`;
                return `<li>${escaped}</li>`;
            });

            return `<ul class="authority-list">${rendered.join('')}</ul>`;
        }

        function renderDetail(op) {
            document.getElementById('detail').innerHTML = buildDetailHtml(op);
        }

        function highlightText(text, query) {
            if (!query) return text;
            const regex = new RegExp(`(${query})`, 'gi');
            return text.replace(regex, '<span class="highlight">$1</span>');
        }

        function escapeHtml(value) {
            return String(value || '')
                .replaceAll('&', '&amp;')
                .replaceAll('<', '&lt;')
                .replaceAll('>', '&gt;');
        }

        document.getElementById('search-input').addEventListener('keypress', e => {
            if (e.key === 'Enter') search();
        });

        document.getElementById('subject-filter').addEventListener('change', search);

    </script>
</body>
</html>
"""

    html = html.replace("__EMBEDDED_OPINIONS__", embedded_json)

    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✓ Created: {HTML_FILE}")


if __name__ == "__main__":
    print("Building Central District (C.D. Cal.) opinions index...\n")
    total = export_data()
    create_html(total)
    if os.getenv("ATLAS_NO_BROWSER", "0") == "1":
        print("\n✓ Complete! Browser launch skipped (ATLAS_NO_BROWSER=1)")
    else:
        print("\n✓ Complete! Opening http://127.0.0.1:8080/central_opinions_index.html")
        webbrowser.open("http://127.0.0.1:8080/central_opinions_index.html")
