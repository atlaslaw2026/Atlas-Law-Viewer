from supreme_court_viewer import extract_authorities


def test_extract_authorities_splits_multiple_usc_sections():
    text = "The Court considered 18 U. S. C. §§ 1111, 1112 and 1113 in reaching its holding."

    result = extract_authorities(text=text, title="United States v. Example", citation="")
    statutes = result["authorities"]["statutes"]

    assert "18 U.S.C. §1111" in statutes
    assert "18 U.S.C. §1112" in statutes
    assert "18 U.S.C. §1113" in statutes


def test_extract_authorities_handles_title_section_format():
    text = "Relief is available under Section 1983 of Title 42 and Title 28 United States Code, Section 1254."

    result = extract_authorities(text=text, title="Example v. City", citation="")
    statutes = result["authorities"]["statutes"]

    assert "42 U.S.C. §1983" in statutes
    assert "28 U.S.C. §1254" in statutes


def test_extract_authorities_filters_noisy_case_prefixes():
    text = "Justice Kennedy adopted in Missouri v. Seibert, 542 U. S. 600 (2004)."

    result = extract_authorities(text=text, title="McCarthy v. Hernandez", citation="")
    cases = result["authorities"]["cases"]

    assert "Missouri v. Seibert" in cases
    assert all("Justice Kennedy adopted in" not in value for value in cases)
