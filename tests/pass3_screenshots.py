"""Saves a screenshot of every screen and pop-up, in light and dark mode.

    .venv/bin/python tests/pass3_screenshots.py

Writes audit_screenshots/pass3/<light|dark>/NN-name.png at the app window's
size (700 x 550). It uses the browser tests' sandboxed server and synthetic
data. Tall screens are captured whole, with the sticky nav and footer made
static for the capture, so that nothing is drawn in the middle of the page.
Every screen is also checked for JavaScript errors. This is not part of
`make test`.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pass3_helpers import (REPO_ROOT, UITestCase, app_mod,  # noqa: E402
                           make_source_drive, make_unreadable)
from test_pass3_click_everything import (GO_DUPS, HeldDupScan, HeldRun,  # noqa: E402
                                         on, write)

OUT = os.path.join(REPO_ROOT, "audit_screenshots", "pass3")
UNSTICK = "nav, #preview-view .sticky { position: static !important; }"


class LightScreens(UITestCase):
    mode = "light"

    def open_app(self, dark=False):
        return super().open_app(dark=self.mode == "dark")

    def shot(self, name, full=True):
        folder = os.path.join(OUT, self.mode)
        os.makedirs(folder, exist_ok=True)
        self.page.wait_for_timeout(300)
        style = self.page.add_style_tag(content=UNSTICK) if full else None
        self.page.screenshot(path=os.path.join(folder, name + ".png"),
                             full_page=full, animations="disabled")
        if style:
            style.evaluate("s => s.remove()")

    def close_popup(self, modal_id):
        self.click(f"#{modal_id} [onclick^='close']")

    def verify(self, button="button:has-text('Run Integrity Verification Checker')"):
        with self.page.expect_response(lambda r: "/api/verify_transfer" in r.url):
            self.click(button)

    def hold(self, cls):
        saved = app_mod.OrganizerAPI
        app_mod.OrganizerAPI = cls
        self.addCleanup(setattr, app_mod, "OrganizerAPI", saved)
        return saved

    def test_1_start_screens(self):
        self.open_app()
        self.shot("01-welcome")
        self.fill_setup(self.path("Source Drive"), self.path("Backup", "Sorted"))
        self.shot("02-setup")
        self.click("#crumb-history")
        self.shot("03-history-empty")
        self.click("#crumb-duplicates")
        self.shot("04-duplicates-empty")
        self.assertNoJsErrors()

    def test_2_preview_dashboard_and_popups(self):
        self.run_preview()
        self.shot("05-preview-dashboard")
        self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        self.shot("06-preview-dashboard-window-bottom", full=False)
        self.click(".category-pill-btn")
        self.shot("07-popup-category", full=False)
        self.close_popup("category-modal")
        self.page.locator("button", has_text="Inspect Ignored Files").click()
        self.shot("08-popup-ignored-files", full=False)
        self.close_popup("garbage-modal")
        self.page.locator(".project-row-item", has_text="Bob's").first \
            .locator(".project-inspect-btn").click()
        self.shot("09-popup-inspect-project", full=False)
        self.close_popup("inspect-modal")
        self.assertNoJsErrors()

    def test_3_run_in_progress(self):
        self.hold(HeldRun)
        HeldRun.go.clear()
        self.addCleanup(HeldRun.go.set)
        self.open_app()
        self.fill_setup(make_source_drive(self.path("source")), self.path("dest", "Sorted"),
                        preview=False)
        self.click("#start-btn")
        self.wait_for("isTransferActive === true")
        self.shot("10-progress-running")
        HeldRun.go.set()
        self.server.wait_run_ended()
        self.assertNoJsErrors()

    def test_4_finished_run_and_review_tools(self):
        self.run_transfer()
        self.shot("11-progress-complete")
        self.verify()
        self.shot("12-verification-result")
        self.click("button:has-text('Review Code Projects')")
        self.page.locator(".project-dissolve-btn").first.wait_for()
        self.shot("13-review-code-projects")
        self.click("button:has-text('Clean Source Duplicates')")
        self.page.locator("[onclick='trashAllDuplicates()']").wait_for()
        self.shot("14-review-source-duplicates")
        self.click("#crumb-history")
        self.page.locator(".history-verify-btn").wait_for()
        self.shot("15-history")
        self.verify(".history-verify-btn")
        self.shot("16-history-integrity-check")
        self.assertNoJsErrors()

    def test_5_finished_with_problems(self):
        source = make_source_drive(self.path("source"))
        make_unreadable(os.path.join(source, "Projects", "my-app", "index.js"))
        self.run_transfer(source)
        self.shot("17-finished-with-problems")
        self.verify()
        self.shot("18-verification-after-problems")
        self.assertNoJsErrors()

    def test_6_duplicates_utility(self):
        root = self.path("Photos Library")
        for i in range(6):
            data = b"photo %d " % i * 500
            write(os.path.join(root, "2019", "IMG_%04d.jpg" % i), data)
            write(os.path.join(root, "2019 backup", "IMG_%04d.jpg" % i), data)
        saved = self.hold(HeldDupScan)
        self.open_app()
        self.click(on("guide-view", GO_DUPS))
        self.page.fill("#dup-source-path", root)
        self.click("#start-dup-scan-btn")
        self.page.locator("[onclick='cancelDuplicateScan()']").wait_for()
        self.shot("19-duplicates-scanning")
        self.click("[onclick='cancelDuplicateScan()']")
        self.server.wait_idle(10)
        app_mod.OrganizerAPI = saved
        self.click("#start-dup-scan-btn")
        self.page.locator(".dup-checkbox").first.wait_for()
        self.shot("20-duplicates-results")
        self.assertNoJsErrors()

    def test_7_offline_evidence(self):
        """Evidence for the report: the welcome screen when the CDNs are unreachable."""
        if self.mode != "light":
            self.skipTest("one capture is enough")
        self.page.route(lambda url: not url.startswith(self.server.url),
                        lambda route: route.abort())
        self.page.goto(self.server.url, wait_until="load")
        self.page.wait_for_timeout(500)
        path = os.path.join(OUT, "evidence", "offline-welcome.png")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.page.screenshot(path=path, full_page=True, animations="disabled")
        print("\noffline console errors:", self.js_errors)


class DarkScreens(LightScreens):
    mode = "dark"


if __name__ == "__main__":
    unittest.main(verbosity=2)
