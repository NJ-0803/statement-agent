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
    """The value of a CSS custom property, from the light block or the dark-mode block."""
    block = HTML.split("prefers-color-scheme: dark")[1] if dark else HTML.split("prefers-color-scheme: dark")[0]
    match = re.search(rf"{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})", block)
    assert match, f"{name} not defined in the {'dark' if dark else 'light'} theme"
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
