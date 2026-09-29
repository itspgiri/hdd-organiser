"""P3-04: the dry-run dashboard sat outside the card's padded area.

Every view lives in the card's padded container (`div.p-6.sm:p-10`) except
`#preview-view`, which was placed after it, directly in `<main>`. So:
- the dashboard touched the card's left and right edges, below an empty band
  (the container's padding, with every view in it hidden);
- the footer's negative margins (`-mx-10`), meant to cancel that padding,
  pushed the footer 40 px past both sides of the card, and the card's
  `overflow: hidden` cut off the right end of "Execute Full Transfer";
- the footer is `sticky bottom-0`, but `overflow: hidden` also makes the card
  the footer's scroll container, and the card never scrolls (the page does),
  so the footer was not pinned: on a long dashboard the button that starts
  the transfer was below the fold.
"""

import unittest

from pass3_helpers import UITestCase, make_source_drive

FOOTER_BUTTONS = ("#confirm-go-btn", "#preview-view button[onclick='resetToSetup()']")


class PreviewLayoutTest(UITestCase):

    def settle(self):
        self.page.evaluate("Promise.all(document.getAnimations().map(a => a.finished))")

    def box_in_card(self, selector):
        """[left, top, right, bottom] of the element, relative to the card (<main>)."""
        return self.page.evaluate("""sel => {
            const m = document.querySelector('main').getBoundingClientRect();
            const r = document.querySelector(sel).getBoundingClientRect();
            return [r.left - m.left, r.top - m.top, r.right - m.left, r.bottom - m.top]
                .map(Math.round);
        }""", selector)

    def on_screen_and_on_top(self, selector):
        return self.page.evaluate("""sel => {
            const el = document.querySelector(sel);
            const r = el.getBoundingClientRect();
            const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
            return r.top >= 0 && r.bottom <= innerHeight && !!hit && el.contains(hit);
        }""", selector)

    def test_footer_buttons_are_inside_the_card(self):
        self.run_preview()
        self.settle()
        card_width = self.page.evaluate(
            "document.querySelector('main').getBoundingClientRect().width")
        for sel in FOOTER_BUTTONS:
            left, _, right, _ = self.box_in_card(sel)
            self.assertGreaterEqual(left, 0, f"{sel} starts left of the card")
            self.assertLessEqual(right, card_width, f"{sel} is cut off by the card's edge")
        self.assertNoJsErrors()

    def test_footer_stays_in_the_window_while_scrolling(self):
        self.run_preview()
        self.settle()
        page_height = self.page.evaluate("document.scrollingElement.scrollHeight")
        self.assertGreater(page_height, 550 + 200, "dashboard too short to test pinning")
        for fraction in (0, 0.5):
            self.page.evaluate(
                f"window.scrollTo(0, {fraction} * (document.scrollingElement.scrollHeight"
                " - innerHeight))")
            for sel in FOOTER_BUTTONS:
                self.assertTrue(self.on_screen_and_on_top(sel),
                                f"{sel} is not visible with the page scrolled {fraction:.0%}")
        self.assertNoJsErrors()

    def test_dashboard_lines_up_with_the_other_screens(self):
        source = self.path("source")
        make_source_drive(source)
        self.open_app()
        self.fill_setup(source, self.path("dest", "Sorted"), preview=True)
        self.settle()
        setup_box = self.box_in_card("#setup-view")
        self.click("#start-btn")
        self.wait_for("document.getElementById('preview-view').classList.contains('active')")
        self.page.evaluate("window.scrollTo(0, 0)")
        self.settle()
        preview_box = self.box_in_card("#preview-view")
        self.assertEqual(preview_box[:3], setup_box[:3],
                         "the dashboard's left/top/right edges differ from the setup screen's")
        self.assertNoJsErrors()


if __name__ == "__main__":
    unittest.main()
