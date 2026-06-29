"""
Atlas Law PDF Viewer with Search — Access and search stored opinions
"""

import json
import os
import re
import sqlite3
import webbrowser
from urllib.parse import quote

DB_PATH = os.path.join(os.path.dirname(__file__), "atlas_law.db")
HTML_FILE = os.path.join(os.path.dirname(__file__), "opinions_index.html")
JSON_FILE = os.path.join(os.path.dirname(__file__), "opinions_data.json")


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table_name,))
    return cur.fetchone() is not None


def _parse_citations(value: str | None) -> dict:
    default = {"cases": [], "statutes": [], "rules": [], "regulations": []}
    if not value:
        return default
    try:
        data = json.loads(value)
        if not isinstance(data, dict):
            return default
        return {
            "cases": data.get("cases", []) or [],
            "statutes": data.get("statutes", []) or [],
            "rules": data.get("rules", []) or [],
            "regulations": data.get("regulations", []) or [],
        }
    except Exception:
        return default


def _load_authorities_map(conn: sqlite3.Connection) -> dict[int, dict]:
    mapped: dict[int, dict] = {}
    if not _table_exists(conn, "authorities"):
        return mapped

    cur = conn.cursor()
    cur.execute("""
        SELECT opinion_id, authority_type, citation
        FROM authorities
        WHERE citation IS NOT NULL AND citation != ''
        ORDER BY opinion_id, id
        """)

    for opinion_id, authority_type, citation in cur.fetchall():
        if opinion_id not in mapped:
            mapped[opinion_id] = {
                "case": [],
                "statute": [],
                "rule": [],
                "regulation": [],
                "constitutional": [],
                "other": [],
            }

        group = (authority_type or "other").strip().lower()
        if group not in mapped[opinion_id]:
            group = "other"

        if citation not in mapped[opinion_id][group]:
            mapped[opinion_id][group].append(citation)

    return mapped


def _looks_like_judicial_opinion(title: str | None, url: str | None, pdf_url: str | None) -> bool:
    title_l = (title or "").strip().lower()
    url_l = (url or "").strip().lower()
    pdf_l = (pdf_url or "").strip().lower()

    blocked_tokens = [
        "/misconduct/",
        "/information/coop",
        "psds.uscourts.gov",
        "seminar.fwx",
        "guidelines for judicial misconduct",
        "emergency operating status",
        "privately funded seminars disclosure",
    ]
    if any(token in title_l or token in url_l or token in pdf_l for token in blocked_tokens):
        return False

    source = f"{url_l} {pdf_l}"
    if ("/datastore/opinions/" in source or "/datastore/memoranda/" in source) and ".pdf" in source:
        return True

    if (pdf_l.endswith(".pdf") or ".pdf" in pdf_l) and (
        " v. " in title_l or title_l.startswith("in re") or title_l.startswith("in the matter of")
    ):
        return True

    return False


def _extract_named_case_citations(text: str, title: str | None) -> list[str]:
    found: list[str] = []

    patterns = [
        r"([A-Z][A-Za-z0-9&.,'\-\s]{2,80}\s+v\.\s+[A-Z][A-Za-z0-9&.,'\-\s]{2,80}(?:,?\s+\d+\s+[A-Z][A-Za-z0-9.\s]{1,20}\s+\d+)?)",
        r"([A-Z][A-Za-z0-9&.,'\-\s]{2,80}\s+v\.\s+[A-Z][A-Za-z0-9&.,'\-\s]{2,80})",
    ]

    sample = (text or "")[:30000]
    for pattern in patterns:
        for match in re.finditer(pattern, sample):
            citation = re.sub(r"\s+", " ", match.group(1)).strip(" ,.;:")

            if not re.search(r"\([^)]*\b(?:19|20)\d{2}\b[^)]*\)", citation):
                tail = sample[match.end() : match.end() + 60]
                year_paren = re.search(r"\(([^)]*\b(?:19|20)\d{2}\b[^)]*)\)", tail)
                if year_paren:
                    year_text = re.sub(r"\s+", " ", year_paren.group(1)).strip()
                    citation = f"{citation} ({year_text})"

            if citation and citation not in found:
                found.append(citation)
            if len(found) >= 30:
                break
        if len(found) >= 30:
            break

    title_value = (title or "").strip()
    if title_value and " v. " in title_value.lower() and title_value not in found:
        found.insert(0, title_value)

    return found[:30]


def _looks_like_good_case_citation(value: str, title: str | None = None) -> bool:
    text = _normalize_ws(_normalize_legal_text(value))
    if not text:
        return False

    if len(text) > 170:
        return False

    if title and text.lower() == _normalize_ws(_normalize_legal_text(title)).lower():
        return True

    has_v = bool(re.search(r"\bv\.\b", text, flags=re.IGNORECASE))
    has_in_re = bool(re.search(r"^\s*in\s+re\b", text, flags=re.IGNORECASE))
    has_reporter = bool(re.search(r"\b\d+\s+[A-Z][A-Za-z.\d ]{0,20}\s+\d+\b", text))
    has_year_paren = bool(re.search(r"\([^)]*\b(?:19|20)\d{2}\b[^)]*\)", text))
    has_court_paren = bool(
        re.search(r"\([^)]*\b(?:cir\.|ct\.|app\.|bap|u\.s\.)[^)]*\)", text, flags=re.IGNORECASE)
    )

    if has_reporter and has_v:
        return True

    if has_in_re and (has_reporter or has_year_paren or has_court_paren):
        return True

    if has_reporter and (has_year_paren or has_court_paren or has_v):
        return True

    if (
        has_reporter
        and len(text) <= 140
        and not any(
            t in text.lower() for t in ["petitioner", "respondent", "plaintiff", "defendant"]
        )
    ):
        return True

    blocked_tokens = [
        "united states court of appeals",
        "petitioner",
        "respondent",
        "plaintiff",
        "defendant",
        "attorney general",
        "agency no.",
        "no. ",
    ]
    lowered = text.lower()
    if any(token in lowered for token in blocked_tokens) and not has_reporter:
        return False

    if (has_v or has_in_re) and len(text) <= 130 and not text.endswith(","):
        return True

    return False


def _clean_case_authorities(citations: list[str], title: str | None = None) -> list[str]:
    cleaned: list[str] = []
    seen_keys: set[str] = set()

    party = r"[A-Z][A-Za-z0-9'&.\-]*(?:\s+[A-Z][A-Za-z0-9'&.\-]*){0,12}"
    case_core_pattern = re.compile(
        rf"(({party})\s+v\.\s+({party})(?:,\s*\d+\s+[A-Z][A-Za-z.\d\s]{{1,25}}\s+\d+(?:,\s*\d+)?)?(?:\s*\([^)]*\))?)"
    )

    for raw in citations:
        value = _normalize_ws(_normalize_legal_text(raw)).strip(" ,;:")

        signaled = re.split(r"\b(?:See|Cf\.|But\s+see|Compare)\b", value, flags=re.IGNORECASE)
        if len(signaled) > 1:
            value = _normalize_ws(signaled[-1]).strip(" ,;:")

        core = case_core_pattern.search(value)
        if core:
            value = _normalize_ws(core.group(1)).strip(" ,;:")

        value = re.sub(r"^(?:See|Cf\.|But see|Compare)\s+", "", value, flags=re.IGNORECASE)

        if not _looks_like_good_case_citation(value, title):
            continue

        key = re.sub(r"\s+", " ", value).lower()
        if key in seen_keys:
            continue

        seen_keys.add(key)
        cleaned.append(value)

    return cleaned[:30]


def _derive_issue_date(opinion_date: str | None, url: str | None, pdf_url: str | None) -> str:
    value = (opinion_date or "").strip()
    if value:
        return value

    source = f"{pdf_url or ''} {url or ''}"
    match = re.search(r"\b((?:19|20)\d{2})[/-](\d{2})[/-](\d{2})\b", source)
    if match:
        year, month, day = match.groups()
        return f"{year}-{month}-{day}"

    return ""


def _normalize_legal_text(value: str | None) -> str:
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


def _is_placeholder_title(title: str | None) -> bool:
    t = (title or "").strip().lower()
    return t in {"pdf document", "document", "pdf"}


def _derive_title(title: str | None, text: str | None, url: str | None) -> str:
    base = (title or "").strip()
    if base and not _is_placeholder_title(base):
        return _normalize_legal_text(base)

    body = (text or "")[:2500]
    match = re.search(r"([A-Z][A-Z0-9&.,'\-\s]{3,120}\s+V\.\s+[A-Z][A-Z0-9&.,'\-\s]{3,120})", body)
    if match:
        return _normalize_legal_text(re.sub(r"\s+", " ", match.group(1)).strip(" ,.;:"))

    url_value = (url or "").strip()
    if url_value:
        file_part = url_value.rstrip("/").split("/")[-1]
        if file_part.lower().endswith(".pdf"):
            return _normalize_legal_text(file_part)

    return "Untitled Opinion"


def _normalize_ws(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _case_cache_key(value: str | None) -> str:
    return _normalize_ws(_normalize_legal_text(value)).lower()


def _case_query_from_citation(citation: str) -> str:
    text = _normalize_ws(_normalize_legal_text(citation))

    reporter = re.search(r"\b\d+\s+[A-Za-z][A-Za-z.\d ]{0,20}\s+\d+\b", text)
    if reporter:
        return reporter.group(0)

    versus = re.search(r"([A-Z][^,;]{2,140}?\sv\.\s[^,;]{2,140})", text, flags=re.IGNORECASE)
    if versus:
        return _normalize_ws(versus.group(1))

    return text[:180]


def _case_link_from_citation(citation: str) -> str:
    text = _normalize_ws(_normalize_legal_text(citation))

    reporter = re.search(r"\b(\d+)\s+([A-Za-z][A-Za-z.\d ]{0,25})\s+(\d+)\b", text)
    if reporter:
        volume, reporter_name, page = reporter.groups()
        reporter_segment = re.sub(r"-{2,}", "-", _normalize_ws(reporter_name).replace(" ", "-"))
        return f"https://www.courtlistener.com/citation/{volume}/{quote(reporter_segment, safe='.-')}/{page}/"

    query = _case_query_from_citation(text)
    if query:
        return f"https://www.google.com/search?q={quote(query + ' case')}"

    return ""


def _attach_case_links(opinions: list[dict]) -> None:
    for op in opinions:
        authorities = op.get("authorities") or {}
        case_items = authorities.get("cases") or []
        case_links: dict[str, str] = {}
        for citation in case_items:
            normalized = _normalize_ws(_normalize_legal_text(citation))
            link = _case_link_from_citation(normalized)
            if link:
                case_links[normalized] = link
        authorities["case_links"] = case_links
        op["authorities"] = authorities


def export_opinions_to_json():
    """Export opinions, citations, and extracted authorities to JSON."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    authorities_map = _load_authorities_map(conn)

    cur.execute("""
        SELECT id, title, date, published, url, pdf_url, local_pdf_path, subjects, text, citations
        FROM opinions
        ORDER BY rowid DESC
        """)

    opinions = []
    total_rows = 0
    skipped_rows = 0

    for (
        opinion_id,
        title,
        opinion_date,
        published,
        url,
        pdf_url,
        local_pdf_path,
        subjects,
        text,
        citations,
    ) in cur.fetchall():
        total_rows += 1
        # Allow backfilled CourtListener records even if they lack URL/PDF data
        is_backfilled = subjects and "courtlistener-backfill" in (subjects or "")
        if not is_backfilled and not _looks_like_judicial_opinion(title, url, pdf_url):
            skipped_rows += 1
            continue

        parsed_citations = _parse_citations(citations)
        parsed_citations = {
            "cases": [_normalize_legal_text(x) for x in parsed_citations.get("cases", [])],
            "statutes": [_normalize_legal_text(x) for x in parsed_citations.get("statutes", [])],
            "rules": [_normalize_legal_text(x) for x in parsed_citations.get("rules", [])],
            "regulations": [
                _normalize_legal_text(x) for x in parsed_citations.get("regulations", [])
            ],
        }
        extracted = authorities_map.get(opinion_id, {})
        named_cases = _extract_named_case_citations(text or "", title)

        extracted_cases = [_normalize_legal_text(x) for x in extracted.get("case", [])]
        extracted_has_names = any(" v. " in value.lower() for value in extracted_cases)
        if extracted_cases and extracted_has_names:
            case_authorities = extracted_cases
        elif extracted_cases and not extracted_has_names:
            case_authorities = named_cases or extracted_cases
        else:
            case_authorities = named_cases

        issue_date = _derive_issue_date(opinion_date, url, pdf_url)

        normalized_title = _derive_title(title, text, pdf_url or url)
        if _is_placeholder_title(normalized_title) and not (text or "").strip():
            skipped_rows += 1
            continue

        case_authorities = _clean_case_authorities(case_authorities, normalized_title)

        opinions.append(
            {
                "id": opinion_id,
                "title": normalized_title,
                "date": opinion_date,
                "issue_date": issue_date,
                "published": bool(published),
                "url": url,
                "pdf_url": pdf_url,
                "local_pdf_path": local_pdf_path or "",
                "subjects": subjects.split(";") if subjects else [],
                "text": text or "",
                "citations": parsed_citations,
                "authorities": {
                    "cases": case_authorities,
                    "statutes": [_normalize_legal_text(x) for x in extracted.get("statute", [])],
                    "rules": [_normalize_legal_text(x) for x in extracted.get("rule", [])],
                    "regulations": [
                        _normalize_legal_text(x) for x in extracted.get("regulation", [])
                    ],
                    "constitutional": [
                        _normalize_legal_text(x) for x in extracted.get("constitutional", [])
                    ],
                    "other": [_normalize_legal_text(x) for x in extracted.get("other", [])],
                },
            }
        )

    opinions.sort(
        key=lambda op: (
            (op.get("issue_date") or ""),
            1 if op.get("published") else 0,
            op.get("id") or 0,
        ),
        reverse=True,
    )
    _attach_case_links(opinions)

    conn.close()

    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(opinions, f, ensure_ascii=False, indent=2)

    print(
        f"OK Exported {len(opinions)} judicial opinions to JSON (skipped {skipped_rows} of {total_rows})"
    )
    return len(opinions)


def create_searchable_html(count):
    """Create two-pane UI: opinions list left, details/PDF right."""
    with open(JSON_FILE, "r", encoding="utf-8") as jf:
        embedded_json = jf.read().replace("</script", "<\\/script")

    html = (
        """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Atlas Law — Ninth Circuit Opinions Search</title>
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

        .view-switch {
            display: flex;
            justify-content: center;
            gap: 8px;
            margin-bottom: 14px;
        }

        .view-link {
            border: 1px solid #0066cc;
            color: #0066cc;
            padding: 6px 10px;
            font-size: 13px;
            font-weight: bold;
            text-decoration: none;
            background: #fff;
        }

        .view-link.active {
            background: #0066cc;
            color: #fff;
        }

        .search-box {
            display: flex;
            gap: 10px;
            margin-bottom: 20px;
            flex-wrap: wrap;
        }
        input[type="text"], select {
            padding: 10px;
            border: 1px solid #0066cc;
            font-size: 14px;
            font-family: "Times New Roman", Times, serif;
            flex: 1;
            min-width: 200px;
        }
        input[type="text"]:focus, select:focus {
            outline: none;
            border: 2px solid #003da5;
            background-color: #f0f4ff;
        }

        button {
            padding: 10px 20px;
            background: #0066cc;
            color: white;
            border: none;
            cursor: pointer;
            font-weight: bold;
            font-family: "Times New Roman", Times, serif;
            font-size: 14px;
            transition: background 0.3s;
        }
        button:hover {
            background: #003da5;
        }

        .pub-toggle {
            display: flex;
            gap: 8px;
        }

        .pub-btn {
            background: #fff;
            color: #0066cc;
            border: 1px solid #0066cc;
        }

        .pub-btn.active {
            background: #0066cc;
            color: #fff;
        }

        .layout {
            display: grid;
            grid-template-columns: 34% 66%;
            gap: 16px;
            min-height: 70vh;
        }

        .left-pane {
            border: 1px solid #c7d9f7;
            background: #fbfdff;
            overflow-y: auto;
            max-height: 74vh;
        }

        .opinion-item {
            border-bottom: 1px solid #e7eefc;
            padding: 12px;
            cursor: pointer;
        }

        .opinion-item:hover {
            background: #eef4ff;
        }

        .opinion-item.active {
            background: #e3edff;
            border-left: 4px solid #0066cc;
        }

        .inline-detail-mobile {
            display: none;
        }

        .opinion-item-title {
            font-size: 15px;
            color: #003da5;
            font-weight: bold;
            margin-bottom: 8px;
        }

        .opinion-item-preview {
            font-size: 12px;
            color: #555;
            line-height: 1.35;
        }

        .opinion-item-date {
            font-size: 12px;
            color: #4a628b;
            margin-bottom: 6px;
            font-weight: bold;
        }

        .right-pane {
            border: 1px solid #c7d9f7;
            padding: 14px;
            overflow-y: auto;
            max-height: 74vh;
            background: white;
        }

        .right-title {
            color: #003da5;
            font-size: 20px;
            margin-bottom: 10px;
        }

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

        .authority-list li {
            margin-bottom: 4px;
        }

        .pdf-frame {
            width: 100%;
            height: 430px;
            border: 1px solid #c7d9f7;
            background: #f5f7fa;
        }

        .empty-state {
            color: #666;
            text-align: center;
            padding: 30px;
        }

        .results-count {
            color: #0066cc;
            margin-bottom: 15px;
            font-size: 14px;
            font-weight: bold;
        }

        .subject-badge {
            display: inline-block;
            background: #e6f0ff;
            color: #003da5;
            padding: 2px 6px;
            margin-right: 5px;
            font-size: 12px;
        }

        a {
            color: #0066cc;
            text-decoration: none;
            font-size: 13px;
        }
        a:hover {
            text-decoration: underline;
        }
        .highlight {
            background-color: #ffff99;
            font-weight: bold;
        }
        .no-results {
            text-align: center;
            color: #666;
            padding: 40px;
            font-size: 16px;
        }
        .preview-text {
            color: #666;
            font-size: 13px;
            margin-bottom: 10px;
            line-height: 1.5;
        }

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

        body.night .opinion-item {
            border-bottom-color: #334155;
        }

        body.night .opinion-item:hover {
            background: #1e293b;
        }

        body.night .opinion-item.active {
            background: #1f2a44;
            border-left-color: #60a5fa;
        }

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

        body.night .pub-btn {
            background: #0f172a;
            color: #93c5fd;
            border-color: #3b82f6;
        }

        body.night .pub-btn.active {
            background: #1d4ed8;
            color: #fff;
            border-color: #1d4ed8;
        }

        body.night a {
            color: #93c5fd;
        }

        @media (max-width: 1080px) {
            .layout {
                grid-template-columns: 1fr;
            }

            .left-pane, .right-pane {
                max-height: none;
            }

            .right-pane {
                display: none;
            }

            .inline-detail-mobile {
                display: block;
                margin-top: 10px;
                border-top: 1px solid #c7d9f7;
                padding-top: 10px;
            }

            .inline-detail-mobile .pdf-frame {
                height: 360px;
            }

            .authority-grid {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Atlas Law Opinions</h1>
        <div class="view-switch">
            <a class="view-link" href="/supreme_opinions_index.html">U.S. Supreme Court</a>
            <a class="view-link active" href="/opinions_index.html">Ninth Circuit</a>
            <a class="view-link" href="/central_opinions_index.html">Central District (C.D. Cal.)</a>
        </div>
        <div class="stats">
            <span id="total-count">"""
        + str(count)
        + """</span> opinions | Searchable
        </div>
        <div class="update-controls">
            <button id="update-all-btn" class="update-btn" onclick="runAtlasRefresh()">Update All Courts</button>
            <span id="update-status" class="update-status">Idle</span>
            <button id="theme-toggle-btn" class="theme-btn" onclick="toggleNightVision()">Night Vision: Off</button>
        </div>

        <div class="search-box">
            <input type="text" id="search-input" placeholder="Search by keyword, case name, judge..." autofocus>
            <select id="subject-filter">
                <option value="">All Subjects</option>
            </select>
            <div class="pub-toggle">
                <button id="published-btn" class="pub-btn" onclick="setPublicationFilter('published')">Published</button>
                <button id="unpublished-btn" class="pub-btn" onclick="setPublicationFilter('unpublished')">Unpublished</button>
            </div>
            <button onclick="search()">Search</button>
            <button onclick="clearSearch()">Clear</button>
        </div>


        <div class="results-count" id="results-count"></div>

        <div class="layout">
            <div id="results" class="left-pane"></div>
            <div id="detail" class="right-pane">
                <div class="empty-state">Select an opinion from the left list.</div>
            </div>
        </div>
    </div>

    <script>
        const EMBEDDED_OPINIONS = __EMBEDDED_OPINIONS__;
        let allOpinions = [];
        let filteredOpinions = [];
        let selectedOpinionId = null;
        let publicationMode = '';

        function applyTheme(theme) {
            const isNight = theme === 'night';
            document.body.classList.toggle('night', isNight);
            const btn = document.getElementById('theme-toggle-btn');
            if (btn) btn.textContent = isNight ? 'Night Vision: On' : 'Night Vision: Off';
        }

        function toggleNightVision() {
            const current = localStorage.getItem('atlas-ninth-theme') || 'day';
            const next = current === 'night' ? 'day' : 'night';
            localStorage.setItem('atlas-ninth-theme', next);
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
                    const warnings = Array.isArray(data?.refresh_summary?.warnings)
                        ? data.refresh_summary.warnings
                        : [];
                    const warningText = warnings.length ? ' (CACD unavailable)' : '';
                    if (data.exit_code === 0) {
                        el.textContent = `Last update: +${added} cases${warningText}`;
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
                published: Boolean(op.published),
                text: op.text || '',
                local_pdf_path: op.local_pdf_path || '',
                subjects: Array.isArray(op.subjects) ? op.subjects.filter(Boolean) : [],
                authorities: op.authorities || {},
                citations: op.citations || {},
            }));
        }

        allOpinions = normalizeOpinions(EMBEDDED_OPINIONS);
        applyTheme(localStorage.getItem('atlas-ninth-theme') || 'day');
        updatePublicationButtons();
        populateSubjectFilter();
        displayAll();
        refreshUpdateStatus();
        setInterval(refreshUpdateStatus, 15000);

        function setPublicationFilter(mode) {
            publicationMode = publicationMode === mode ? '' : mode;
            selectedOpinionId = null;
            updatePublicationButtons();
            search();
        }

        function updatePublicationButtons() {
            const publishedBtn = document.getElementById('published-btn');
            const unpublishedBtn = document.getElementById('unpublished-btn');
            if (!publishedBtn || !unpublishedBtn) return;
            publishedBtn.classList.toggle('active', publicationMode === 'published');
            unpublishedBtn.classList.toggle('active', publicationMode === 'unpublished');
        }

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

        const STANDARDS = {
            "rule_16b4": {
                "title": "Rule 16(b)(4) — Ninth Circuit standard",
                "body": "Rule 16(b)(4) requires 'good cause' to modify a scheduling order; under Ninth Circuit law (Johnson v. Mammoth Recreations, Inc., 975 F.2d 604 (9th Cir. 1992)) 'good cause' focuses on the moving party's diligence in attempting to meet the schedule. If good cause exists, courts then consider the Rule 15 factors (bad faith, prejudice, futility, undue delay) when deciding whether to permit the amendment."
            }
        };

        function populateStandards() {
            const select = document.getElementById('standards-select');
            if (!select) return;
            // remove any existing except the first
            while (select.options.length > 1) select.remove(1);
            Object.keys(STANDARDS).forEach(key => {
                const opt = document.createElement('option');
                opt.value = key;
                opt.textContent = STANDARDS[key].title;
                select.appendChild(opt);
            });

            select.addEventListener('change', () => {
                const key = select.value;
                const display = document.getElementById('standard-display');
                const titleEl = document.getElementById('standard-title');
                const bodyEl = document.getElementById('standard-body');
                if (!key) {
                    if (display) display.style.display = 'none';
                    return;
                }
                const item = STANDARDS[key];
                if (titleEl) titleEl.textContent = item.title || '';
                if (bodyEl) bodyEl.textContent = item.body || '';
                if (display) display.style.display = 'block';
            });
        }

        function insertSelectedStandard() {
            const select = document.getElementById('standards-select');
            if (!select) return;
            const key = select.value;
            if (!key) return;
            const item = STANDARDS[key];
            const input = document.getElementById('search-input');
            if (input) {
                input.value = (input.value ? input.value + ' ' : '') + (item.body || item.title || '');
                input.focus();
            }
        }

        function search() {
            const query = document.getElementById('search-input').value.toLowerCase().trim();
            const subject = document.getElementById('subject-filter').value;
            const publication = publicationMode;

            if (!query && !subject && !publication) {
                displayAll();
                return;
            }

            filteredOpinions = allOpinions.filter(op => {
                const safeTitle = (op.title || '').toLowerCase();
                const safeText = (op.text || '').toLowerCase();
                const safeSubjects = Array.isArray(op.subjects) ? op.subjects : [];
                const matchesQuery = !query ||
                    safeTitle.includes(query) ||
                    safeText.includes(query);

                const matchesSubject = !subject || safeSubjects.includes(subject);
                const matchesPublication = !publication ||
                    (publication === 'published' ? op.published : !op.published);

                return matchesQuery && matchesSubject && matchesPublication;
            });

            displayResults();
        }

        function displayAll() {
            filteredOpinions = allOpinions;
            displayResults();
        }

        function displayResults() {
            const resultsDiv = document.getElementById('results');
            const query = document.getElementById('search-input').value.toLowerCase().trim();

            if (filteredOpinions.length === 0) {
                resultsDiv.innerHTML = '<div class="no-results">No opinions found.</div>';
                document.getElementById('results-count').textContent = 'Results: 0';
                document.getElementById('detail').innerHTML = '<div class="empty-state">No opinion selected.</div>';
                return;
            }

            document.getElementById('results-count').textContent = 'Results: ' + filteredOpinions.length;

            const mobileView = isMobileView();

            const html = filteredOpinions.map(op => {
                let title = op.title || 'Untitled Opinion';
                let text_preview = cleanPreview(op.text).substring(0, 160);

                if (query) {
                    title = highlightText(title, query);
                    text_preview = highlightText(text_preview, query);
                }

                const activeClass = selectedOpinionId === op.id ? 'active' : '';
                const issuedDate = op.issue_date ? String(op.issue_date) : (op.date ? String(op.date) : 'Unknown date');
                const publicationLabel = op.published ? 'Published' : 'Memorandum';
                const inlineDetail = mobileView && selectedOpinionId === op.id
                    ? `<div class="inline-detail-mobile">${buildDetailHtml(op)}</div>`
                    : '';

                return `
                    <div class="opinion-item ${activeClass}" onclick="selectOpinion(${op.id})">
                        <div class="opinion-item-title">${title}</div>
                        <div class="opinion-item-date">Issued: ${issuedDate} | ${publicationLabel}</div>
                        ${text_preview ? '<div class="opinion-item-preview">' + text_preview + '...</div>' : ''}
                        <div style="margin-top:6px;">
                            ${op.subjects.length > 0 ? op.subjects.slice(0, 4).map(s => '<span class="subject-badge">' + s + '</span>').join('') : ''}
                        </div>
                        ${inlineDetail}
                    </div>
                `;
            }).join('');

            resultsDiv.innerHTML = html;

            const selected = filteredOpinions.find(op => op.id === selectedOpinionId);
            if (selected) {
                if (!mobileView) {
                    renderDetail(selected);
                }
            } else if (filteredOpinions.length > 0) {
                selectOpinion(filteredOpinions[0].id);
            }
        }

        function isMobileView() {
            return window.matchMedia('(max-width: 1080px)').matches;
        }

        function cleanPreview(text) {
            if (!text) return '';

            let cleaned = String(text).replace(/\\s+/g, ' ').trim();

            const junkPatterns = [
                /Home About the Court[^.]{0,300}Calendar Oral Argument/gi,
                /Viewing a Document\\s*-\\s*PACER/gi,
                /Ninth Circuit Court of Appeals/gi,
                /Published Unpublished/gi,
                /Motions Opinions/gi,
                /Employment E-Filing/gi,
                /Attorneys Mediation News Media/gi,
                /Judges Reporting Attendance/gi,
            ];

            junkPatterns.forEach(pattern => {
                cleaned = cleaned.replace(pattern, ' ');
            });

            cleaned = cleaned.replace(/\\s{2,}/g, ' ').trim();
            return cleaned;
        }

        function detectRuleFamily(value) {
            const text = String(value || '').replace(/\s+/g, ' ').trim();
            if (/Fed\.\s*R\.\s*App\.\s*P\.|\\bFRAP\\b/i.test(text)) return 'frap';
            if (/Fed\.\s*R\.\s*Civ\.\s*P\.|\\bFRCP\\b/i.test(text)) return 'frcp';
            if (/Fed\.\s*R\.\s*Crim\.\s*P\.|\\bFRCrP\\b/i.test(text)) return 'frcrmp';
            if (/Fed\.\s*R\.\s*Evid\.|\\bFRE\\b/i.test(text)) return 'fre';
            if (/Fed\.\s*R\.\s*Bankr\.\s*P\.|\\bFRBP\\b/i.test(text)) return 'frbp';
            return null;
        }

        function inferRuleFamily(items) {
            if (!Array.isArray(items)) return null;
            const counts = { frap: 0, frcp: 0, frcrmp: 0, fre: 0, frbp: 0 };
            for (const item of items) {
                const family = detectRuleFamily(item);
                if (family && counts[family] !== undefined) {
                    counts[family] += 1;
                }
            }
            let winner = null;
            let maxCount = 0;
            for (const key of Object.keys(counts)) {
                if (counts[key] > maxCount) {
                    maxCount = counts[key];
                    winner = key;
                }
            }
            return maxCount > 0 ? winner : null;
        }

        function ruleLinkFor(value, fallbackFamily = null) {
            const text = String(value || '');
            const normalized = text.replace(/\s+/g, ' ').trim();

            const frap = normalized.match(/Fed\.\s*R\.\s*App\.\s*P\.\s*(\d+)/i);
            if (frap) {
                return `https://www.law.cornell.edu/rules/frap/rule_${frap[1]}`;
            }

            const frapShort = normalized.match(/\\bFRAP\s*(\d+)/i);
            if (frapShort) {
                return `https://www.law.cornell.edu/rules/frap/rule_${frapShort[1]}`;
            }

            const frcp = normalized.match(/Fed\.\s*R\.\s*Civ\.\s*P\.\s*(\d+)/i);
            if (frcp) {
                return `https://www.law.cornell.edu/rules/frcp/rule_${frcp[1]}`;
            }

            const frcpShort = normalized.match(/\\bFRCP\s*(\d+)/i);
            if (frcpShort) {
                return `https://www.law.cornell.edu/rules/frcp/rule_${frcpShort[1]}`;
            }

            const frcrp = normalized.match(/Fed\.\s*R\.\s*Crim\.\s*P\.\s*(\d+)/i);
            if (frcrp) {
                return `https://www.law.cornell.edu/rules/frcrmp/rule_${frcrp[1]}`;
            }

            const frcrpShort = normalized.match(/\\bFRCrP\s*(\d+)/i);
            if (frcrpShort) {
                return `https://www.law.cornell.edu/rules/frcrmp/rule_${frcrpShort[1]}`;
            }

            const frbp = normalized.match(/Fed\.\s*R\.\s*Bankr\.\s*P\.\s*(\d+)/i);
            if (frbp) {
                return `https://www.law.cornell.edu/rules/frbp/rule_${frbp[1]}`;
            }

            const frbpShort = normalized.match(/\\bFRBP\s*(\d+)/i);
            if (frbpShort) {
                return `https://www.law.cornell.edu/rules/frbp/rule_${frbpShort[1]}`;
            }

            const fre = normalized.match(/Fed\.\s*R\.\s*Evid\.\s*(\d+)/i);
            if (fre) {
                return `https://www.law.cornell.edu/rules/fre/rule_${fre[1]}`;
            }

            const freShort = normalized.match(/\\bFRE\s*(\d+)/i);
            if (freShort) {
                return `https://www.law.cornell.edu/rules/fre/rule_${freShort[1]}`;
            }

            const bareRule = normalized.match(/^Rule\s*(\d+)/i);
            if (bareRule && fallbackFamily) {
                return `https://www.law.cornell.edu/rules/${fallbackFamily}/rule_${bareRule[1]}`;
            }

            return null;
        }

        function statuteLinkFor(value) {
            const normalized = String(value || '')
                .replace(/(?:Ã‚)?Â§/g, '§')
                .replace(/\s+/g, ' ')
                .trim();

            const usc = normalized.match(/(\d+)\s*U\.?\s*S\.?\s*C\.?\s*§+\s*([0-9A-Za-z._-]+)/i);
            if (usc) {
                const title = usc[1];
                const section = usc[2];
                return `https://www.law.cornell.edu/uscode/text/${title}/${section}`;
            }

            return null;
        }

        function regulationLinkFor(value) {
            const normalized = String(value || '')
                .replace(/(?:Ã‚)?Â§/g, '§')
                .replace(/\s+/g, ' ')
                .trim();

            const cfr = normalized.match(/(\d+)\s*C\.?\s*F\.?\s*R\.?\s*§+\s*([0-9]+(?:\.[0-9A-Za-z-]+)*)/i);
            if (cfr) {
                const title = cfr[1];
                const section = cfr[2];
                return `https://www.ecfr.gov/current/title-${title}/section-${section}`;
            }

            return null;
        }

        function caseLinkFor(value) {
            const normalized = String(value || '')
                .replace(/\s+/g, ' ')
                .trim();

            if (!normalized) return null;

            const reporter = normalized.match(/(\d+\s+[A-Za-z][A-Za-z.\d ]{0,20}\s+\d+)/);
            if (reporter) {
                const query = `${reporter[1]} case`;
                return `https://www.courtlistener.com/?q=${encodeURIComponent(query)}`;
            }

            const versus = normalized.match(/([A-Z][^,;]{2,120}?\sv\.\s[^,;]{2,120})/i);
            if (versus) {
                const query = `${versus[1]} case`;
                return `https://www.courtlistener.com/?q=${encodeURIComponent(query)}`;
            }

            return `https://www.courtlistener.com/?q=${encodeURIComponent(normalized)}`;
        }

        function proxiedPdfUrl(url) {
            const value = String(url || '').trim();
            if (!value) return '';
            return `/api/pdf?url=${encodeURIComponent(value)}`;
        }

        function buildPdfEmbed(url, title = 'Opinion PDF', originalUrl = '') {
            const pdfUrl = String(url || '').trim();
            const originalPdfUrl = String(originalUrl || '').trim() || pdfUrl;
            if (!pdfUrl) {
                return '<div class="empty-state">No PDF URL available for this opinion.</div>';
            }

            const escapedPdfUrl = escapeHtml(pdfUrl);
            const escapedOriginalUrl = escapeHtml(originalPdfUrl);
            const escapedTitle = escapeHtml(title);
            return `<object class="pdf-frame" data="${escapedPdfUrl}" type="application/pdf">
                        <iframe class="pdf-frame" src="${escapedPdfUrl}" title="${escapedTitle}"></iframe>
                   </object>
                   <div style="margin-top:8px;"><a href="${escapedOriginalUrl}" target="_blank">Open original PDF in new tab</a></div>`;
        }

        function listItems(items, category = '', linkMap = null) {
            if (!items || items.length === 0) {
                return '<div class="preview-text">None</div>';
            }

            const normalizeDisplayText = (value) => String(value)
                .replaceAll('Ã‚Â§', '§')
                .replaceAll('Â§', '§')
                .replaceAll('â€”', '—')
                .replaceAll('â€“', '–')
                .replaceAll('â€™', '’')
                .replaceAll('â€œ', '“')
                .replaceAll('â€�', '”');

            const inferredRuleFamily = category === 'rules' ? inferRuleFamily(items) : null;

            const rendered = items.slice(0, 50).map(i => {
                const normalized = normalizeDisplayText(i);
                const escaped = normalized
                    .replaceAll('&', '&amp;')
                    .replaceAll('<', '&lt;')
                    .replaceAll('>', '&gt;');

                if (category === 'rules') {
                    const link = ruleLinkFor(normalized, inferredRuleFamily);
                    if (link) {
                        return `<li><a href="${link}" target="_blank">${escaped}</a></li>`;
                    }
                }

                if (category === 'cases') {
                    const mappedLink = linkMap && typeof linkMap === 'object' ? (linkMap[normalized] || null) : null;
                    const params = new URLSearchParams({
                        citation: normalized,
                        hint: mappedLink || ''
                    });
                    const link = `/api/resolve_case?${params.toString()}`;
                    if (link) {
                        return `<li><a href="${link}" target="_blank">${escaped}</a></li>`;
                    }
                }

                if (category === 'statutes') {
                    const link = statuteLinkFor(normalized);
                    if (link) {
                        return `<li><a href="${link}" target="_blank">${escaped}</a></li>`;
                    }
                }

                if (category === 'regulations') {
                    const link = regulationLinkFor(normalized);
                    if (link) {
                        return `<li><a href="${link}" target="_blank">${escaped}</a></li>`;
                    }
                }

                return `<li>${escaped}</li>`;
            });

            return `<ul class="authority-list">${rendered.join('')}</ul>`;
        }

        function escapeHtml(value) {
            return String(value || '')
                .replaceAll('&', '&amp;')
                .replaceAll('<', '&lt;')
                .replaceAll('>', '&gt;')
                .replaceAll('"', '&quot;')
                .replaceAll("'", '&#39;');
        }

        function findOpinionById(opinionId) {
            if (opinionId === null || opinionId === undefined) return null;
            const target = String(opinionId);
            return filteredOpinions.find(op => String(op.id) === target) || allOpinions.find(op => String(op.id) === target) || null;
        }

        function selectOpinion(id) {
            selectedOpinionId = id;
            const opinion = filteredOpinions.find(op => op.id === id) || allOpinions.find(op => op.id === id);
            if (!opinion) {
                return;
            }
            displayResults();
            if (!isMobileView()) {
                renderDetail(opinion);
            }
        }

        function buildDetailHtml(op) {
            const authorities = op.authorities || {};
            const citations = op.citations || {};
            const caseLinkMap = authorities.case_links || {};
            const caseItems = (authorities.cases && authorities.cases.length) ? authorities.cases : (citations.cases || []);
            const statuteItems = (authorities.statutes && authorities.statutes.length) ? authorities.statutes : (citations.statutes || []);
            const ruleItems = (authorities.rules && authorities.rules.length) ? authorities.rules : (citations.rules || []);
            const regulationItems = (authorities.regulations && authorities.regulations.length) ? authorities.regulations : (citations.regulations || []);
            const constitutionalItems = authorities.constitutional || [];
            const localPdfProxy = op.local_pdf_path ? `/api/local_pdf?path=${encodeURIComponent(op.local_pdf_path)}` : '';
            const pdfProxy = (op.pdf_url ? proxiedPdfUrl(op.pdf_url) : '') || localPdfProxy;
            const sourceUrl = op.url || '#';
            const originalPdfUrl = op.pdf_url || op.url || '';

            const pdfHtml = buildPdfEmbed(pdfProxy, 'Original PDF', originalPdfUrl);

            return `
                <div class="right-title">${op.title || 'Untitled Opinion'}</div>
                <div style="margin-bottom:10px;">
                    <a href="${sourceUrl}" target="_blank">View source opinion page</a>
                </div>
                <div class="authority-grid">
                    <div class="authority-box">
                        <div class="authority-head">Cases</div>
                        ${listItems(caseItems, 'cases', caseLinkMap)}
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
                        ${listItems([...(regulationItems || []), ...(constitutionalItems || [])], 'regulations')}
                    </div>
                </div>
                <div class="authority-head" style="margin-bottom:8px;">Original PDF</div>
                ${pdfHtml}
            `;
        }

        function renderDetail(op) {
            document.getElementById('detail').innerHTML = buildDetailHtml(op);
        }

        function highlightText(text, query) {
            if (!query) return text;
            const regex = new RegExp(`(${query})`, 'gi');
            return text.replace(regex, '<span class="highlight">$1</span>');
        }

        function clearSearch() {
            document.getElementById('search-input').value = '';
            document.getElementById('subject-filter').value = '';
            publicationMode = '';
            updatePublicationButtons();
            selectedOpinionId = null;
            displayAll();
        }

        document.getElementById('search-input').addEventListener('keypress', e => {
            if (e.key === 'Enter') search();
        });

        document.getElementById('subject-filter').addEventListener('change', search);

    </script>
</body>
</html>
"""
    )

    html = html.replace("__EMBEDDED_OPINIONS__", embedded_json)

    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"OK Created: {HTML_FILE}")


if __name__ == "__main__":
    print("Building searchable opinions index...\n")
    count = export_opinions_to_json()
    create_searchable_html(count)
    if os.getenv("ATLAS_NO_BROWSER", "0") == "1":
        print("\nOK Complete! Browser launch skipped (ATLAS_NO_BROWSER=1)")
    else:
        print("\nOK Complete! Opening http://127.0.0.1:8080/")
        webbrowser.open("http://127.0.0.1:8080/")
