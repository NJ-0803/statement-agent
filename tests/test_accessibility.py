"""Guards for the accessibility fixes found in the 18 Sep 2026 keyboard and zoom pass.

These are static checks on the page's own markup and script. They cannot replace a screen reader —
that check is still outstanding, and IMPLEMENTATION_STATUS.md says so — but each one pins a specific
failure that was reproduced in a browser first, so a later edit cannot quietly bring it back:

  * a focus stop nobody can see (the skip link, and the two visually hidden file inputs)
  * a control outline below the 3:1 that WCAG 2.2 AA 1.4.11 asks for
  * focus falling to <body> when the element holding it is replaced
"""

import os
import re

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "statement_agent", "web")
HTML = open(os.path.join(WEB, "templates", "index.html"), encoding="utf-8").read()
APP_JS = open(os.path.join(WEB, "static", "app.js"), encoding="utf-8").read()


def _contrast(fg: str, bg: str) -> float:
    def lum(value: str) -> float:
        value = value.lstrip("#")
        channels = (int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    high, low = sorted((lum(fg), lum(bg)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _token(name: str, *, dark: bool) -> str:
    """A CSS custom property's value, from the Evening (default, dark) or Daylight block.

    Evening is :root; Daylight lives under prefers-color-scheme: light.
    """
    split = HTML.split("prefers-color-scheme: light")
    block = split[0] if dark else split[1]
    match = re.search(rf"{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})", block)
    assert match, f"{name} not defined in the {'Evening' if dark else 'Daylight'} theme"
    return match.group(1)


class TestNothingTakesFocusInvisibly:
    def test_skip_link_becomes_visible_when_it_is_focused(self):
        # it is the first tab stop on the page; clipped to 1px it is a focus stop with no indicator
        assert 'class="visually-hidden skip"' in HTML
        rule = re.search(r"\.skip:focus\s*\{([^}]*)\}", HTML)
        assert rule, "the skip link has no :focus rule, so it stays clipped when tabbed to"
        body = rule.group(1)
        assert "clip: auto" in body and "width: auto" in body and "height: auto" in body

    def test_hidden_file_inputs_show_their_focus_ring_on_the_label_that_stands_for_them(self):
        # #file-input and #photo-input are visually hidden but still tab stops; the ring has to land
        # on something the user can see — the label immediately before each one
        assert "label:has(+ input.visually-hidden:focus-visible)" in HTML
        for input_id in ("file-input", "photo-input"):
            label = re.search(rf'<label[^>]*for="{input_id}"', HTML)
            assert label, f"{input_id} has no label to carry its focus ring"
            after_label = HTML[label.end():]
            assert re.match(r'[^<]*(?:<[^>]*>[^<]*)*?<input id="%s"' % input_id, after_label, re.S), \
                f"the label for {input_id} is no longer its immediate sibling, so the :has rule cannot match"


class TestControlOutlinesMeetNonTextContrast:
    """1.4.11: a border that shows where a control is needs 3:1 against every background behind it."""

    def test_field_line_token_passes_on_every_surface_in_both_themes(self):
        for dark in (False, True):
            field_line = _token("--field-line", dark=dark)
            for surface in ("--surface", "--bg", "--surface-2"):
                ratio = _contrast(field_line, _token(surface, dark=dark))
                assert ratio >= 3.0, (
                    f"{'dark' if dark else 'light'} control border {field_line} on {surface} is {ratio:.2f}:1"
                )

    def test_controls_use_the_accessible_token_rather_than_the_decorative_one(self):
        for selector in ("select, input", ".dropzone", "fieldset", ".btn.quiet"):
            block = HTML.split(selector, 1)[1].split("}", 1)[0]
            assert "var(--field-line)" in block, f"{selector} still outlines itself with the decorative --line"


class TestFocusIsNeverDroppedOnTheBody:
    def test_finishing_a_batch_focuses_the_file_input_not_the_label(self):
        # #dropzone is a <label>: calling .focus() on it silently does nothing
        assert "$('#dropzone').focus()" not in APP_JS
        reset = APP_JS.split("function resetToFiles()", 1)[1].split("}", 1)[0]
        assert "$('#file-input').focus()" in reset

    def test_opening_and_closing_a_transaction_editor_keeps_focus_in_the_row(self):
        # the Change button removes itself from the DOM, so focus has to be placed deliberately
        assert "button[data-change]" in APP_JS, "no stable handle to return focus to"
        assert "'data-change': '1'" in APP_JS
        assert "$('select, input', li).focus()" in APP_JS, "opening the editor leaves focus on <body>"
        assert "$('button[data-change]', li).focus()" in APP_JS, "closing the editor leaves focus on <body>"

    def test_saving_a_correction_returns_focus_to_the_row_that_was_edited(self):
        # a save rebuilds every row in the list
        assert 'li[data-txn="${CSS.escape(t.id)}"] button[data-change]' in APP_JS
        assert "'data-txn': t.id" in APP_JS


class TestTheBhookmarkPaletteStillMeetsTextContrast:
    """Adopting the house palette must not cost any of the contrast checked on 18 Sep 2026."""

    PAIRS = [
        ("--ink", "--bg", "body text"),
        ("--ink", "--surface", "text on a card"),
        ("--muted", "--bg", "secondary text on the page"),
        ("--muted", "--surface", "secondary text on a card"),
        ("--muted", "--surface-2", "secondary text on a step chip and table head"),
        ("--rose", "--bg", "links"),
        ("--rose", "--surface", "outline button labels"),
        ("--accent-ink", "--accent", "text on a burgundy button"),
        ("--good", "--good-soft", "the good status chip"),
        ("--warn-ink", "--warn-soft", "the check-this status chip"),
        ("--bad", "--bad-soft", "the problem status chip"),
        ("--bad", "--surface", "the danger button label"),
        ("--ink", "--accent-soft", "the current step chip and the demo banner"),
    ]

    def test_every_text_pair_clears_four_and_a_half_to_one(self):
        failures = []
        for fg, bg, what in self.PAIRS:
            for dark in (False, True):
                ratio = _contrast(_token(fg, dark=dark), _token(bg, dark=dark))
                if ratio < 4.5:
                    failures.append(f"{'Evening' if dark else 'Daylight'} {what}: {ratio:.2f}:1")
        assert not failures, failures

    def test_the_focus_ring_is_visible_against_every_background(self):
        for dark in (False, True):
            focus = _token("--focus", dark=dark)
            for surface in ("--bg", "--surface", "--surface-2"):
                ratio = _contrast(focus, _token(surface, dark=dark))
                assert ratio >= 3.0, f"{'Evening' if dark else 'Daylight'} focus on {surface}: {ratio:.2f}:1"

    def test_no_gold_saffron_or_amber_entered_the_palette(self):
        # a standing rule of the design system, and easy to reintroduce by reaching for a warning colour
        tokens = re.findall(r"--[\w-]+:\s*(#[0-9a-fA-F]{6})", HTML)
        offenders = []
        for value in set(tokens):
            r, g, b = (int(value[i:i + 2], 16) for i in (1, 3, 5))
            if r > 150 and 90 < g < 210 and b < 90 and r - b > 90 and g - b > 40:
                offenders.append(value)
        assert not offenders, f"amber/gold tones in the palette: {offenders}"

    def test_the_fonts_are_served_from_this_app_not_a_third_party(self):
        assert "fonts.googleapis.com" not in HTML and "fonts.gstatic.com" not in HTML
        assert HTML.count("@font-face") >= 2
        for name in ("instrument-serif-400-latin.woff2", "inter-latin.woff2"):
            assert name in HTML
            assert os.path.exists(os.path.join(WEB, "static", "fonts", name)), f"{name} is referenced but missing"
