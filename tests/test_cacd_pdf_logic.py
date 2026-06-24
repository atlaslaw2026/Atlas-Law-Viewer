from urllib.parse import urlparse

import atlas_law_server
import central_district_viewer


def test_is_allowed_pdf_target_allows_cases_justia_direct_pdf():
    target = urlparse(
        "https://cases.justia.com/cases/federal/district-courts/california/cacdce/2:2026cv01234/123456/0.pdf"
    )
    assert atlas_law_server.is_allowed_pdf_target(target) is True


def test_is_allowed_pdf_target_rejects_non_cacd_law_justia_path():
    target = urlparse("https://law.justia.com/cases/federal/district-courts/new-york/nysdce/1:2026cv00001/")
    assert atlas_law_server.is_allowed_pdf_target(target) is False


def test_create_html_includes_justia_pdf_derivation_logic(tmp_path, monkeypatch):
    output_file = tmp_path / "central_opinions_index.html"
    monkeypatch.setattr(central_district_viewer, "HTML_FILE", str(output_file))

    central_district_viewer.create_html(0)

    html = output_file.read_text(encoding="utf-8")
    assert "function deriveJustiaPdfFromOpinionUrl(url)" in html
    assert "https://cases.justia.com${normalizedPath}0.pdf" in html


def test_cloudflare_challenge_detector():
    payload = "Just a moment... Please enable JavaScript to continue. https://challenges.cloudflare.com"
    assert central_district_viewer.is_cloudflare_challenge(payload) is True
    assert central_district_viewer.is_cloudflare_challenge("normal opinion page") is False
