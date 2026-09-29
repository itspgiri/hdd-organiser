"""P3-03: the preview dashboard's pop-ups never appeared on screen.

The three pop-ups (code-project inspection, ignored files, category samples)
were written inside the progress view, but only the preview dashboard opens
them, and the progress view is hidden while the dashboard shows. Opening one
removed its `hidden` class and nothing appeared. Inside any view a pop-up
would still be misplaced: every `.view` keeps the `transform` left by its
fade-in animation, so a `position: fixed` child is placed against the view
instead of the window, and the card's `overflow-hidden` cuts it off.

The tests open each pop-up with the dashboard's own buttons, check that it is
displayed, fits in the 700x550 window and is on top, in light and dark mode,
and use the buttons inside it. They also check the backdrop: leftover card
styles in style.css made it rounded and bordered and, in dark mode, opaque in
the pop-up's own colour, so the page behind vanished and the card had no edge.
"""

import unittest

from pass3_helpers import UITestCase

CLICK_TIMEOUT = 5000  # ms; a pop-up that is not on screen cannot be clicked


class PreviewPopupsVisibleTest(UITestCase):

    def open_category(self):
        self.page.locator(".category-pill-btn", has_text="Documents/PDF").click()
        return "category-modal"

    def open_garbage(self):
        self.page.locator("button", has_text="Inspect Ignored Files").click()
        return "garbage-modal"

    def open_inspect(self):
        # The row's only button (its class was added by P3-02; 92ed5c1 lacks it).
        row = self.page.locator(".project-row-item", has_text="my-app").first
        row.locator("button").click()
        return "inspect-modal"

    def close_with_x(self, modal_id):
        self.page.locator(f"#{modal_id} button[onclick^='close']").click(
            timeout=CLICK_TIMEOUT)
        self.assertFalse(self.modal_state(modal_id)["displayed"],
                         f"#{modal_id} is still displayed after its X was clicked")

    def assertBackdropSeeThrough(self, modal_id):
        """The full-window backdrop must dim the page, not hide it or look like a card."""
        bg, radius = self.page.evaluate("""id => {
            const s = getComputedStyle(document.getElementById(id));
            return [s.backgroundColor, s.borderTopLeftRadius];
        }""", modal_id)
        self.assertTrue(bg.startswith("rgba(") and not bg.endswith(", 1)"),
                        f"#{modal_id} backdrop is opaque: {bg}")
        self.assertEqual(radius, "0px", f"#{modal_id} backdrop has rounded corners")

    def check_popups(self, dark):
        self.run_preview(dark=dark)
        for opener in (self.open_category, self.open_garbage, self.open_inspect):
            modal_id = opener()
            self.assertModalVisible(modal_id)
            self.assertBackdropSeeThrough(modal_id)
            self.close_with_x(modal_id)
        self.assertNoJsErrors()

    def test_popups_are_visible_in_light_mode(self):
        self.check_popups(dark=False)

    def test_popups_are_visible_in_dark_mode(self):
        self.check_popups(dark=True)

    def test_sort_as_regular_files_button_in_the_inspect_popup(self):
        self.run_preview()
        self.assertModalVisible(self.open_inspect())
        self.page.click("#toggle-exclude-btn", timeout=CLICK_TIMEOUT)
        self.assertFalse(self.modal_state("inspect-modal")["displayed"])
        excluded = self.page.evaluate("excludedProjects")
        self.assertEqual([p.rsplit("/", 1)[-1] for p in excluded], ["my-app"])
        row = self.page.locator(".project-row-item", has_text="my-app").first
        self.assertIn("Split up", row.inner_text())
        # ...and the same button in the re-opened pop-up undoes it.
        self.assertModalVisible(self.open_inspect())
        self.assertIn("Re-Enable", self.page.inner_text("#toggle-exclude-btn"))
        self.page.click("#toggle-exclude-btn", timeout=CLICK_TIMEOUT)
        self.assertEqual(self.page.evaluate("excludedProjects"), [])
        self.assertNoJsErrors()

    def test_popups_are_outside_every_view(self):
        """Structural check: no pop-up may sit inside a view or the card."""
        inside = self.open_app().evaluate("""() =>
            [...document.querySelectorAll('.inspect-modal')]
                .filter(m => m.closest('.view, main'))
                .map(m => m.id)""")
        self.assertEqual(inside, [])


if __name__ == "__main__":
    unittest.main()
