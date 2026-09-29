"""P3-02: a folder name with an apostrophe broke the preview's project controls.

The project rows put the folder's path inside a JavaScript string in an
inline handler: onchange="...('<path>', this.checked)". escapeHtml turns '
into &#39;, but the HTML parser turns it back before the JavaScript runs, so
an apostrophe ended the string: "Bob's app" threw a SyntaxError, and a folder
named like `x');doSomething();('` ran its own code with the page's API token.
"""

import os
import unittest

from pass3_helpers import TRICKY_PROJECT, UITestCase, make_source_drive, write

EVIL_PROJECT = "evil');window.__ranFromFolderName=1;('"


class QuotesInPathsTest(UITestCase):

    def row(self, name):
        return self.page.locator(".project-row-item", has_text=name).first

    def test_checkbox_for_a_project_with_an_apostrophe(self):
        source, _ = self.run_preview()
        self.row("Bob's").locator(".project-checkbox").uncheck()
        self.assertNoJsErrors()
        self.assertEqual(self.page.evaluate("excludedProjects"),
                         [os.path.join(source, "Projects", TRICKY_PROJECT)])
        self.assertIn("Split up", self.row("Bob's").inner_text())

    def test_inspect_button_for_a_project_with_an_apostrophe(self):
        self.run_preview()
        self.row("Bob's").locator("button").click()
        self.assertNoJsErrors()
        self.page.wait_for_function(
            "!document.getElementById('inspect-files-list').innerText.includes('Loading')")
        self.assertIn(TRICKY_PROJECT, self.page.inner_text("#inspect-folder-title"))
        self.assertIn("main.py", self.page.inner_text("#inspect-files-list"))
        # The pop-up's visibility is P3-03; this checks the path it acts on.
        self.page.dispatch_event("#toggle-exclude-btn", "click")
        self.assertNoJsErrors()
        self.assertEqual(len(self.page.evaluate("excludedProjects")), 1)

    def test_a_folder_name_cannot_run_code(self):
        source = make_source_drive(self.path("source"))
        write(os.path.join(source, "Projects", EVIL_PROJECT, "package.json"), b"{}")
        self.run_preview(source=source)
        self.row("evil").locator(".project-checkbox").click()
        self.row("evil").locator("button").click()
        self.assertIsNone(self.page.evaluate("window.__ranFromFolderName"))
        self.assertEqual(self.page.evaluate("excludedProjects"),
                         [os.path.join(source, "Projects", EVIL_PROJECT)])
        self.assertNoJsErrors()


if __name__ == "__main__":
    unittest.main()
