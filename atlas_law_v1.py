"""
Atlas Law — Ninth Circuit Opinions fetcher (single-file)

Usage (PowerShell):
python .\\atlas_law_v1.py

Configure optional environment variables in a `.env` file in the same folder:
DB_PATH, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, EMAIL_FROM, EMAIL_TO

This script fetches the Ninth Circuit opinions index, finds opinion links, extracts text,
pulls simple citations, classifies by keywords, stores results in local SQLite, and sends
an email summary when new opinions are found (if SMTP configured).
"""
from __future__ import annotations
import os
import re
import json
import hashlib
import sqlite3
import datetime
import io
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup
from email.message import EmailMessage
import smtplib
try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Configuration (can be set in .env)
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "atlas_law.db"))
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER)
EMAIL_TO = os.getenv("EMAIL_TO", "")
USER_AGENT = os.getenv("USER_AGENT", "AtlasLawBot/1.0 (+https://local)")
PDF_DIR = os.getenv("NINTH_PDF_DIR", os.path.join(os.path.dirname(__file__), "ninth_pdfs"))
MAX_NINTH_PDF_DOWNLOAD_PER_RUN = int(os.getenv("NINTH_PDF_LIMIT", "2500"))

BASE = "https://www.ca9.uscourts.gov"
INDEX = "https://www.ca9.uscourts.gov/opinions/"
MEMORANDA = "https://www.ca9.uscourts.gov/memoranda/"
HEADERS = {"User-Agent": USER_AGENT}


def init_db(path: str = DB_PATH):
    conn = sqlite3.connect(path, timeout=120)
    cur = conn.cursor()
    cur.execute("PRAGMA busy_timeout = 120000")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS opinions (
        id INTEGER PRIMARY KEY,
        url TEXT UNIQUE NOT NULL,
        title TEXT,
        date TEXT,
        docket TEXT,
        published INTEGER DEFAULT 0,
        text TEXT,
        pdf_url TEXT,
        local_pdf_path TEXT,
        citations TEXT,
        subjects TEXT,
        created_at TEXT
    )
    """)
    cur.execute("PRAGMA table_info(opinions)")
    columns = {row[1] for row in cur.fetchall()}
    if "local_pdf_path" not in columns:
        cur.execute("ALTER TABLE opinions ADD COLUMN local_pdf_path TEXT")
    conn.commit()
    return conn


def _safe_pdf_filename(url: str, title: str = "") -> str:
    path = (urlparse(url).path or "").strip()
    base = os.path.basename(path)
    if not base:
        base = re.sub(r"[^A-Za-z0-9._-]", "_", (title or "ninth_opinion"))
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    stem = base[:-4] if base.lower().endswith(".pdf") else base
    digest = hashlib.sha1((url or stem).encode("utf-8", errors="ignore")).hexdigest()[:10]
    return f"{stem[:140]}_{digest}.pdf" if stem else f"ninth_opinion_{digest}.pdf"


def cache_ninth_pdf(pdf_url: str, title: str = "", pdf_bytes: bytes | None = None) -> str:
    if not pdf_url:
        return ""
    try:
        os.makedirs(PDF_DIR, exist_ok=True)
        file_path = os.path.join(PDF_DIR, _safe_pdf_filename(pdf_url, title))
        if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            return file_path

        if pdf_bytes is None:
            response = requests.get(pdf_url, headers=HEADERS, timeout=45)
            response.raise_for_status()
            pdf_bytes = response.content

        if not pdf_bytes:
            return ""

        with open(file_path, "wb") as f:
            f.write(pdf_bytes)
        return file_path if os.path.exists(file_path) else ""
    except Exception:
        return ""


def list_recent_opinion_links() -> list[tuple[str, str]]:
    """Fetch both published opinions and unpublished memoranda"""
    links: list[tuple[str, str]] = []
    
    # Fetch published opinions
    try:
        r = requests.get(INDEX, headers=HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.select("a"):
            href = a.get("href") or ""
            if not href:
                continue
            text = a.get_text(" ", strip=True)
            if href.lower().endswith(".pdf") or "opinion" in href.lower() or "opinions" in text.lower():
                full = urljoin(BASE, href)
                links.append((text or a.get("title") or "", full))
    except Exception as e:
        print(f"Error fetching published opinions: {e}")
    
    # Fetch unpublished memoranda
    try:
        r = requests.get(MEMORANDA, headers=HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.select("a"):
            href = a.get("href") or ""
            if not href:
                continue
            text = a.get_text(" ", strip=True)
            if href.lower().endswith(".pdf") or "memoranda" in href.lower() or text.strip():
                full = urljoin(BASE, href)
                links.append((text or a.get("title") or "", full))
    except Exception as e:
        print(f"Error fetching memoranda: {e}")
    
    # dedupe preserving order
    seen = set()
    dedup = []
    for t, u in links:
        if u not in seen:
            seen.add(u)
            dedup.append((t, u))
    return dedup


def fetch_opinion_page(url: str) -> tuple[bytes | str, str]:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    content_type = r.headers.get("Content-Type", "")
    
    # Return raw bytes for PDFs, text for HTML
    if "pdf" in content_type.lower() or url.lower().endswith(".pdf"):
        return r.content, content_type
    return r.text, content_type


def extract_from_pdf(pdf_bytes: bytes) -> str:
    """Extract text from PDF using PyMuPDF"""
    if not fitz:
        return ""
    
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text_parts = []
        for page_num in range(len(doc)):
            page = doc[page_num]
            text = page.get_text()
            if text.strip():
                text_parts.append(text)
        doc.close()
        return "\n\n".join(text_parts)
    except Exception as e:
        print(f"Error extracting PDF: {e}")
        return ""


def extract_from_html(html: str, base_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.string.strip() if soup.title and soup.title.string else None
    body_text = []
    # Collect all text elements (p, div, span, etc.)
    for el in soup.find_all(['p', 'div', 'span', 'li', 'td']):
        txt = el.get_text(" ", strip=True)
        if txt and len(txt) > 10:  # ignore very short fragments
            body_text.append(txt)
    text = "\n\n".join(body_text).strip()
    
    # If we got no text, try to get from body
    if not text:
        body = soup.find('body')
        if body:
            text = body.get_text("\n", strip=True)
    
    pdf = None
    for a in soup.select("a"):
        href = a.get("href") or ""
        if href.lower().endswith(".pdf"):
            pdf = urljoin(base_url, href)
            break
    return {"title": title, "text": text or "", "pdf_url": pdf}


# Improved citation extraction patterns for real opinion PDFs
CASE_PATTERNS = [
    # Full case citation: Name v. Name, XX F.3d YYY or XX U.S. YYY
    r'([A-Z][a-zA-Z0-9\s&,.\'\-]{3,40}?)\s+v\.\s+([A-Z][a-zA-Z0-9\s&,.\'\-]{3,40}?),\s+(\d+)\s+(U\.S\.|F\.\d?d|P\.\d?d|S\.Ct\.|Cal\.\s?App)',
    # Shorter case name: Name v. Name,
    r'([A-Z][a-zA-Z\s\&,.\'\-]{5,40}?)\s+v\.\s+([A-Z][a-zA-Z\s\&,.\'\-]{5,40}?)[,.]',
]
STATUTE_PATTERNS = [
    # Full USC citation: 28 U.S.C. § 1291
    r'(\d+)\s+U\.S\.C\.?\s*§?\s*(\d+[a-zA-Z0-9\-]*)',
]

REGULATION_PATTERNS = [
    # CFR citation: 8 C.F.R. § 1003.31
    r'(\d+)\s+C\.F\.R\.?\s*§?\s*(\d+(?:\.\d+)*)',
]

RULE_PATTERNS = [
    # Fed. R. App. P. 34 ; Fed. R. Civ. P. 12(b)(6)
    r'Fed\.\s+R\.[A-Za-z\.\s]*\s+\d+(?:\([a-zA-Z0-9]+\))*',
    # Generic rule reference
    r'Rule\s+\d+(?:\([a-zA-Z0-9]+\))*',
]


def extract_citations(text: str) -> dict:
    found = {"cases": [], "statutes": [], "rules": [], "regulations": []}
    if not text or len(text) < 100:
        return found
    
    # Extract case citations
    for pattern in CASE_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            citation_text = match.group(0).strip()
            # Clean up whitespace
            citation_text = re.sub(r'\s+', ' ', citation_text)
            if citation_text and citation_text not in found["cases"]:
                found["cases"].append(citation_text)
    
    # Extract statute citations
    for pattern in STATUTE_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            citation_text = match.group(0).strip()
            if citation_text and citation_text not in found["statutes"]:
                found["statutes"].append(citation_text)

    # Extract regulation citations
    for pattern in REGULATION_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            citation_text = match.group(0).strip()
            if citation_text and citation_text not in found["regulations"]:
                found["regulations"].append(citation_text)

    # Extract rules citations
    for pattern in RULE_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            citation_text = re.sub(r'\s+', ' ', match.group(0)).strip()
            if citation_text and citation_text not in found["rules"]:
                found["rules"].append(citation_text)
    
    return found


DEFAULT_KEYWORDS = {
    "immigration": ["immigration", "asylum", "deport", "removal", "naturalization"],
    "criminal": ["convict", "sentence", "guilty", "drug trafficking", "criminal"],
    "patent": ["patent", "infring", "claim", "prior art"],
    "antitrust": ["antitrust", "monopol", "competition", "sherman"],
    "contract": ["contract", "breach", "agreement"],
    "civil rights": ["civil rights", "equal protection", "first amendment", "fourteenth"],
}


def classify_by_keywords(text: str) -> list[str]:
    if not text:
        return []
    text_l = text.lower()
    scores = {}
    for k, kws in DEFAULT_KEYWORDS.items():
        for kw in kws:
            if kw and kw.lower() in text_l:
                scores[k] = scores.get(k, 0) + 1
    subjects = sorted(scores.keys(), key=lambda s: -scores[s])
    return subjects


def opinion_exists(conn: sqlite3.Connection, url: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM opinions WHERE url = ?", (url,))
    return cur.fetchone() is not None


def opinion_needs_refresh(conn: sqlite3.Connection, url: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT title, text FROM opinions WHERE url = ?", (url,))
    row = cur.fetchone()
    if not row:
        return True

    title = (row[0] or "").strip().lower()
    text = (row[1] or "").strip()
    placeholder_title = title in {"pdf document", "document", "pdf", ""}
    missing_text = not text
    return placeholder_title or missing_text


def save_opinion(conn: sqlite3.Connection, data: dict) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR IGNORE INTO opinions (url, title, date, docket, published, text, pdf_url, local_pdf_path, citations, subjects, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data.get("url"),
            data.get("title"),
            data.get("date"),
            data.get("docket"),
            1 if data.get("published") else 0,
            data.get("text"),
            data.get("pdf_url"),
            data.get("local_pdf_path") or "",
            json.dumps(data.get("citations", {}), ensure_ascii=False),
            ";".join(data.get("subjects", [])),
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    return cur.lastrowid


def update_opinion(conn: sqlite3.Connection, data: dict) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE opinions
        SET title = ?,
            date = ?,
            docket = ?,
            published = ?,
            text = ?,
            pdf_url = ?,
            local_pdf_path = COALESCE(NULLIF(?, ''), local_pdf_path),
            citations = ?,
            subjects = ?,
            created_at = ?
        WHERE url = ?
        """,
        (
            data.get("title"),
            data.get("date"),
            data.get("docket"),
            1 if data.get("published") else 0,
            data.get("text"),
            data.get("pdf_url"),
            data.get("local_pdf_path") or "",
            json.dumps(data.get("citations", {}), ensure_ascii=False),
            ";".join(data.get("subjects", [])),
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
            data.get("url"),
        ),
    )
    conn.commit()


def send_summary(subject: str, body: str):
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
        print("SMTP not configured; skipping email.")
        return
    msg = EmailMessage()
    msg["From"] = EMAIL_FROM or SMTP_USER
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


def recategorize_existing_citations(conn: sqlite3.Connection) -> int:
    cur = conn.cursor()
    rows = cur.execute("SELECT url, text, subjects FROM opinions").fetchall()
    updated = 0
    for url, text, subjects in rows:
        body = text or ""
        if not body.strip():
            continue
        new_cites = extract_citations(body)
        new_subjects = classify_by_keywords(body)
        cur.execute(
            """
            UPDATE opinions
            SET citations = ?,
                subjects = ?
            WHERE url = ?
            """,
            (
                json.dumps(new_cites, ensure_ascii=False),
                ";".join(new_subjects),
                url,
            ),
        )
        updated += 1
    conn.commit()
    return updated


def backfill_ninth_local_pdfs(conn: sqlite3.Connection, max_download: int = MAX_NINTH_PDF_DOWNLOAD_PER_RUN) -> int:
    cur = conn.cursor()
    cur.execute("SELECT url, title, pdf_url, local_pdf_path FROM opinions ORDER BY id DESC")
    rows = cur.fetchall()
    downloaded = 0
    updates = 0

    for url, title, pdf_url, local_pdf_path in rows:
        if downloaded >= max_download:
            break

        candidate_url = (pdf_url or "").strip() or (url or "").strip()
        if not candidate_url or ".pdf" not in candidate_url.lower():
            continue

        desired_path = os.path.join(PDF_DIR, _safe_pdf_filename(candidate_url, title or ""))

        if local_pdf_path and os.path.exists(local_pdf_path) and os.path.abspath(local_pdf_path) == os.path.abspath(desired_path):
            continue

        if os.path.exists(desired_path) and os.path.getsize(desired_path) > 0:
            cached_path = desired_path
        else:
            cached_path = cache_ninth_pdf(candidate_url, title or "")

        if not cached_path:
            continue

        cur.execute("UPDATE opinions SET local_pdf_path = ? WHERE url = ?", (cached_path, url))
        updates += 1
        downloaded += 1

    if updates:
        conn.commit()
    return downloaded


def run_once():
    conn = init_db()
    links = list_recent_opinion_links()
    new_rows = []
    refreshed_rows = []
    for title_hint, url in links:
        try:
            exists = opinion_exists(conn, url)
            if exists and not opinion_needs_refresh(conn, url):
                continue
            content, content_type = fetch_opinion_page(url)
            
            # Handle PDF extraction
            if "pdf" in content_type.lower() or url.lower().endswith(".pdf"):
                if fitz:
                    text = extract_from_pdf(content)
                    local_pdf_path = cache_ninth_pdf(url, title_hint, content)
                    parsed = {"title": title_hint, "text": text, "pdf_url": url, "local_pdf_path": local_pdf_path}
                else:
                    print(f"PyMuPDF not available; skipping PDF: {url}")
                    continue
            else:
                # HTML content
                parsed = extract_from_html(content, url)
                local_pdf_path = ""
                if parsed.get("pdf_url"):
                    local_pdf_path = cache_ninth_pdf(parsed.get("pdf_url") or "", parsed.get("title") or title_hint)
                parsed["local_pdf_path"] = local_pdf_path
            
            text = parsed.get("text") or ""
            pdf_url = parsed.get("pdf_url") or url if "pdf" in content_type.lower() else parsed.get("pdf_url")
            cites = extract_citations(text)
            subjects = classify_by_keywords(text)
            row = {
                "url": url,
                "title": parsed.get("title") or title_hint,
                "date": None,
                "docket": None,
                "published": "memoranda" not in url.lower(),
                "text": text,
                "pdf_url": pdf_url,
                "local_pdf_path": parsed.get("local_pdf_path") or "",
                "citations": cites,
                "subjects": subjects,
            }
            if exists:
                update_opinion(conn, row)
                refreshed_rows.append(row)
            else:
                save_opinion(conn, row)
                new_rows.append(row)
        except Exception as e:
            print("Error processing", url, e)
    if new_rows or refreshed_rows:
        lines = []
        for r in new_rows:
            lines.append(f"- {r.get('title')} — {r.get('url')}\n  Subjects: {','.join(r.get('subjects', []))}\n  Citations: {json.dumps(r.get('citations', {}), ensure_ascii=False)}\n")
        for r in refreshed_rows:
            lines.append(f"- [refreshed] {r.get('title')} — {r.get('url')}\n  Subjects: {','.join(r.get('subjects', []))}\n  Citations: {json.dumps(r.get('citations', {}), ensure_ascii=False)}\n")
        subject = f"Ninth Circuit updates — new {len(new_rows)}, refreshed {len(refreshed_rows)}"
        send_summary(subject, "\n".join(lines))
        print(subject)
    else:
        print("No new opinions found.")

    try:
        recat_count = recategorize_existing_citations(conn)
        print(f"Re-categorized citations for {recat_count} opinions.")
    except sqlite3.OperationalError as e:
        print(f"Warning: skipping recategorization due to database lock: {e}")

    try:
        backfilled = backfill_ninth_local_pdfs(conn)
        print(f"Backfilled local Ninth PDFs: {backfilled}")
    except sqlite3.OperationalError as e:
        print(f"Warning: skipping PDF backfill due to database lock: {e}")

    conn.close()


def build_ninth_viewer() -> None:
    """Rebuild Ninth JSON/HTML outputs from the refreshed SQLite data."""
    try:
        import atlas_law_viewer as ninth_viewer
    except Exception as exc:
        print(f"Warning: unable to import atlas_law_viewer.py: {exc}")
        return

    try:
        count = ninth_viewer.export_opinions_to_json()
        ninth_viewer.create_searchable_html(count)
        print(f"Rebuilt Ninth viewer outputs ({count} opinions).")
    except Exception as exc:
        print(f"Warning: unable to rebuild Ninth viewer outputs: {exc}")


if __name__ == "__main__":
    run_once()
    build_ninth_viewer()
