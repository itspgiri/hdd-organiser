"""P3-01: the preview dashboard's buttons called functions that do not exist.

Clicking a category ("View samples"), "Inspect Ignored Files" or a code
project's checkbox threw a ReferenceError (`showCategoryPreview`,
`showGarbageModal`, `toggleProjectExclusion` are not defined), and the
project's inspect button called `inspectProject(path)` without the name, so
the pop-up's title read "Inspect Folder: undefined".

These tests use the project without an apostrophe (`my-app`); P3-02 covers
names with one. Whether the pop-up is actually visible is P3-03.
"""

import unittest

from pass3_helpers import UITestCase


class PreviewDashboardButtonsTest(UITestCase):

    def modal_opened(self, modal_id):
        return self.page.evaluate(
            "id => !document.getElementById(id).classList.contains('hidden')", modal_id)

    def project_row(self, name):
        return self.page.locator(".project-row-item", has_text=name).first

    def test_category_button_fills_and_opens_the_samples_popup(self):
        self.run_preview()
        self.page.locator(".category-pill-btn", has_text="Documents/PDF").click()
        self.assertNoJsErrors()
        self.assertTrue(self.modal_opened("category-modal"))
        self.assertIn("Documents/PDF", self.page.inner_text("#category-modal-title"))
        self.assertIn("report.pdf", self.page.inner_text("#category-files-list"))

    def test_inspect_ignored_files_fills_and_opens_the_breakdown(self):
        self.run_preview()
        self.page.locator("button", has_text="Inspect Ignored Files").click()
        self.assertNoJsErrors()
        self.assertTrue(self.modal_opened("garbage-modal"))
        self.assertIn(".DS_Store", self.page.inner_text("#garbage-breakdown-list"))

    def test_project_checkbox_marks_project_to_be_split_up(self):
        self.run_preview()
        self.project_row("my-app").locator(".project-checkbox").uncheck()
        self.assertNoJsErrors()
        excluded = self.page.evaluate("excludedProjects")
        self.assertEqual([p.rsplit("/", 1)[-1] for p in excluded], ["my-app"])
        self.assertIn("Split up", self.project_row("my-app").inner_text())
        self.project_row("my-app").locator(".project-checkbox").check()
        self.assertNoJsErrors()
        self.assertEqual(self.page.evaluate("excludedProjects"), [])

    def test_project_inspect_button_names_the_project(self):
        self.run_preview()
        self.project_row("my-app").locator("button").click()
        self.assertNoJsErrors()
        self.assertTrue(self.modal_opened("inspect-modal"))
        self.page.wait_for_function(
            "!document.getElementById('inspect-files-list').innerText.includes('Loading')")
        title = self.page.inner_text("#inspect-folder-title")
        self.assertIn("my-app", title)
        self.assertNotIn("undefined", title)
        self.assertIn("index.js", self.page.inner_text("#inspect-files-list"))


if __name__ == "__main__":
    unittest.main()
