from statement_agent.agent.tools import resolve_date


class TestUnambiguousFormats:
    def test_iso_date_is_confident(self):
        r = resolve_date("2025-07-05")
        assert r["date"] == "2025-07-05"
        assert r["confidence"] == 1.0
        assert r["assumption"] == ""

    def test_textual_month_is_confident(self):
        r = resolve_date("5 July 2025")
        assert r["date"] == "2025-07-05"
        assert r["confidence"] == 1.0

    def test_numeric_with_one_part_over_12_is_confident(self):
        # 25 can't be a month, so this is unambiguous regardless of convention
        r = resolve_date("25/03/2026")
        assert r["date"] == "2026-03-25"
        assert r["confidence"] == 1.0
        assert r["assumption"] == ""


class TestGenuinelyAmbiguousDates:
    """A single date typed in a question has no other dates to disambiguate
    against (unlike a document, where DocumentDateResolver can use other rows) —
    so this must always fall back to the locale default and always flag it."""

    def test_05_07_2026_falls_back_to_locale_default_and_flags_it(self):
        r = resolve_date("05/07/2026")
        assert r["date"] == "2026-07-05"  # DD/MM locale default -> 5 July
        assert r["confidence"] < 1.0
        assert r["assumption"] != ""

    def test_ambiguous_date_never_silently_certain(self):
        # every genuinely ambiguous single date must carry a non-empty assumption,
        # regardless of which specific date it is
        for raw in ["01/02/2025", "03/04/2025", "11/12/2025"]:
            r = resolve_date(raw)
            assert r["confidence"] < 1.0
            assert r["assumption"] != ""


class TestUnrecognizedInput:
    def test_garbage_returns_none_date(self):
        r = resolve_date("not a date")
        assert r["date"] is None
        assert r["confidence"] == 0.0

    def test_raw_field_echoes_input(self):
        r = resolve_date("05/07/2026")
        assert r["raw"] == "05/07/2026"
