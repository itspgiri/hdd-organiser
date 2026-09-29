"""Pass 3: open every screen and use every control, with synthetic data only.

Each test walks one screen the way the owner would, with real clicks, and
checks that the console has no JavaScript errors. An init script records the
controls that receive a click, change, input or keyup event. The last test
compares that record with every control in the page's markup, plus the
controls the page builds later (DYNAMIC). So a control added later without a
test here fails the suite.

Nothing outside the test's sandbox folder is touched: the folder pickers,
Finder and `open -R` go to the SubprocessRecorder, which runs nothing.
"""

import os
import threading
import unittest

from pass3_helpers import UITestCase, api_mod, app_mod, make_source_drive, write

# How the recorder names a control: the inline handler for the event (click
# handlers bare, others as "onchange=..."), plus the screen or pop-up it is
# in; or else its id; or else its *-btn / *-checkbox class.
RECORDER = """(() => {
    const where = el => {
        const v = el.closest('.view, .inspect-modal, nav');
        return v ? (v.id || v.tagName.toLowerCase()) : 'page';
    };
    const named = (el, attr) =>
        (attr === 'onclick' ? '' : attr + '=') + el.getAttribute(attr) + ' @' + where(el);
    window.__controlId = el => {
        for (const attr of ['onclick', 'onchange', 'oninput', 'onkeyup']) {
            if (el.hasAttribute(attr)) return named(el, attr);
        }
        if (el.id) return '#' + el.id;
        return '.' + [...el.classList].find(c => /-(btn|checkbox)$/.test(c));
    };
    window.__used = [];
    for (const [type, attr] of [['click', 'onclick'], ['change', 'onchange'],
                                ['input', 'oninput'], ['keyup', 'onkeyup']]) {
        document.addEventListener(type, e => {
            const el = e.target.closest && e.target.closest(`[${attr}], button, input, select`);
            if (el) window.__used.push(el.hasAttribute(attr) ? named(el, attr) : window.__controlId(el));
        }, true);
    }
})();"""

STATIC_CONTROLS = ("[...document.querySelectorAll('[onclick],[onchange],[oninput],[onkeyup]')]"
                   ".map(__controlId)")

# Controls that exist only once the page has built them from results.
DYNAMIC = [
    ".category-pill-btn", "#projects-select-all-btn", "#projects-deselect-all-btn",
    "onkeyup=filterProjectsList() @preview-view", ".project-checkbox", ".project-inspect-btn",
    "inspectGarbageFiles() @preview-view",
    ".project-dissolve-btn", "trashAllDuplicates() @progress-view",
    "restoreAllDuplicates() @progress-view", "#repair-resync-btn",
    ".history-csv-btn", ".history-verify-btn", ".history-finder-btn",
    ".dup-checkbox", ".dup-reveal-btn", "showMoreDuplicateGroups() @duplicates-view",
]


def on(view, handler):
    """Selector for the control in `view` whose onclick is exactly `handler`."""
    return f"#{view} [onclick=\"{handler}\"]"


GO_SETUP = "try { showView('setup-view'); } catch(e) { alert('Error: ' + e.message); }"
GO_DUPS = "try { showView('duplicates-view'); } catch(e) { alert('Error: ' + e.message); }"


class HeldRun(api_mod.OrganizerAPI):
    """An organizer whose run() lasts until `go` is set or it is cancelled."""

    go = threading.Event()

    def run(self, *args, **kwargs):
        while not HeldRun.go.wait(0.05):
            if self.cancelled:
                return False
        return True


class HeldDupScan(api_mod.OrganizerAPI):
    """A duplicate scan that lasts until it is cancelled (or 20 s pass)."""

    def find_duplicates_inplace(self, folder_abs):
        for _ in range(400):
            self._emit_progress(0, 0, "Held by the test")  # lets the app cancel it
            if self.cancelled:
                return []
            threading.Event().wait(0.05)
        return []


class ClickEverythingTest(UITestCase):

    dark = False

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.used, cls.ran = set(), set()

    def setUp(self):
        super().setUp()
        self.page.add_init_script(RECORDER)
        self.addCleanup(self._collect)  # before the context closes
        type(self).ran.add(self._testMethodName)

    def _collect(self):
        try:
            type(self).used.update(self.page.evaluate("window.__used || []"))
        except Exception:  # the page is gone; nothing more to record
            pass

    def open_app(self, dark=False):
        self._collect()  # a reload forgets what this page recorded
        return super().open_app(dark=dark or self.dark)

    def view(self):
        return self.page.evaluate("document.querySelector('.view.active').id")

    def assertView(self, view_id):
        self.assertEqual(self.view(), view_id)

    def pick(self, folder, button):
        """Clicks a Browse button; the stand-in folder picker answers `folder`."""
        self.server.recorder.picker_result = folder + "\n"
        with self.page.expect_response(lambda r: "/api/select_folder" in r.url):
            self.click(button)
        self.page.wait_for_timeout(100)

    def wait_for_dialog(self, text):
        """Waits (up to 10 s) for an alert or confirm that contains `text`."""
        for _ in range(200):
            if any(text in message for _, message in self.dialogs):
                return
            self.page.wait_for_timeout(50)
        self.fail("no dialog with %r; dialogs: %s" % (text, self.dialogs))

    # -- screens ------------------------------------------------------------

    def test_welcome_screen_and_navigation(self):
        self.open_app()
        steps = [
            (on("guide-view", GO_SETUP), "setup-view"),
            ("#nav-back-btn", "guide-view"),
            (on("guide-view", "loadHistoryView()"), "history-view"),
            (on("history-view", GO_SETUP), "setup-view"),
            (on("setup-view", "showView('guide-view')"), "guide-view"),
            (on("guide-view", GO_DUPS), "duplicates-view"),
            (on("duplicates-view", "showView('guide-view')"), "guide-view"),
            ("#crumb-setup", "setup-view"),
            ("#crumb-history", "history-view"),
            ("#crumb-duplicates", "duplicates-view"),
            ("#crumb-guide", "guide-view"),
        ]
        for selector, expected in steps:
            self.click(selector)
            self.page.wait_for_timeout(150)
            self.assertView(expected)
        self.assertNoJsErrors()

    def test_setup_screen(self):
        source = make_source_drive(self.path("source"))
        dest = self.path("dest")
        os.makedirs(dest)
        self.open_app()
        self.click(on("guide-view", GO_SETUP))
        self.pick(source, on("setup-view", "selectFolder('source')"))
        self.assertEqual(self.page.input_value("#source-path"), source)
        self.page.fill("#source-path", source)
        self.page.dispatch_event("#source-path", "change")
        self.pick(dest, on("setup-view", "selectFolder('dest')"))
        self.assertEqual(self.page.input_value("#dest-path"), dest)
        for card in ("#card-merge", "#card-new"):
            self.click(card)
            self.assertIn("active", self.page.get_attribute(card, "class"))
        self.page.set_checked("#preview-mode", True)
        self.click("#start-btn")
        self.wait_for("document.getElementById('preview-view').classList.contains('active')")
        self.assertEqual(len(self.server.recorder.commands("osascript")), 2)
        self.assertNoJsErrors()

    def test_preview_dashboard(self):
        self.run_preview()
        self.click(".category-pill-btn")
        self.assertModalVisible("category-modal")
        self.click("#category-modal [onclick^='close']")
        self.page.locator("button", has_text="Inspect Ignored Files").click()
        self.assertModalVisible("garbage-modal")
        self.click("#garbage-modal [onclick^='close']")

        def row():
            return self.page.locator(".project-row-item", has_text="my-app").first

        row().locator(".project-inspect-btn").click()
        self.assertModalVisible("inspect-modal")
        self.click("#toggle-exclude-btn")  # "Sort as regular files"
        self.assertEqual(len(self.page.evaluate("excludedProjects")), 1)
        row().locator(".project-inspect-btn").click()
        self.click("#inspect-modal [onclick^='close']")
        # The project named with ' " < > & (known issue 4: inline handlers).
        bob = self.page.locator(".project-row-item", has_text="Bob's").first
        bob.locator(".project-inspect-btn").click()
        self.assertModalVisible("inspect-modal")
        self.click("#inspect-modal [onclick^='close']")
        # The footer's buttons are inside the card (known issue 3).
        self.assertTrue(self.page.evaluate("""() => {
            const card = document.querySelector('main').getBoundingClientRect();
            const buttons = [...document.querySelectorAll('#preview-view .sticky button')];
            return buttons.length > 0 && buttons.every(b => {
                const r = b.getBoundingClientRect();
                return r.left >= card.left && r.right <= card.right; });
        }"""), "a footer button is outside the card")
        self.click("#projects-deselect-all-btn")
        self.assertEqual(len(self.page.evaluate("excludedProjects")), 2)
        self.click("#projects-select-all-btn")
        self.assertEqual(self.page.evaluate("excludedProjects"), [])
        row().locator(".project-checkbox").uncheck()
        row().locator(".project-checkbox").check()
        self.page.type("#project-search-filter", "my")
        self.assertFalse(self.page.locator(".project-row-item", has_text="Bob").first.is_visible())
        for _ in range(2):
            self.page.press("#project-search-filter", "Backspace")

        self.click(on("preview-view", "resetToSetup()"))  # Cancel & Edit Settings
        self.assertView("setup-view")
        self.click("#start-btn")  # the dry run again
        self.wait_for("document.getElementById('preview-view').classList.contains('active')")
        self.click("#confirm-go-btn")  # the real copy, into the sandbox
        self.assertEqual(self.server.wait_run_ended(), "complete")
        self.wait_for("pollInterval === null")
        self.assertEqual(self.page.inner_text("#status-heading").strip(), "Organization Complete!")
        self.assertNoJsErrors()

    def test_progress_screen_and_review_tools(self):
        source, dest, status = self.run_transfer()
        self.assertEqual(status, "complete")
        self.page.type("#log-filter", "zz")
        self.page.press("#log-filter", "Backspace")
        self.page.press("#log-filter", "Backspace")

        self.click(on("progress-view", "loadProjectReview()"))
        dissolve = self.page.locator(".project-dissolve-btn").first
        dissolve.wait_for()
        with self.page.expect_response(lambda r: "/api/dissolve_project" in r.url):
            dissolve.click()

        self.click(on("progress-view", "loadDuplicateCleaner()"))
        trash = self.page.locator("[onclick='trashAllDuplicates()']")
        trash.wait_for()
        with self.page.expect_response(lambda r: "/api/trash_duplicates" in r.url):
            trash.click()
        restore = self.page.locator("[onclick='restoreAllDuplicates()']")
        restore.wait_for()
        with self.page.expect_response(lambda r: "/api/restore_duplicates" in r.url):
            restore.click()
        self.assertTrue(os.path.exists(os.path.join(source, "Camera/copy/IMG_0001 copy.jpg")))

        with self.page.expect_response(lambda r: "/api/open_finder" in r.url):
            self.click(on("progress-view", "openDestinationInFinder()"))
        self.assertIn(["open", "--", os.path.realpath(dest)], self.server.recorder.calls)
        with self.page.expect_download() as download:
            self.click(on("progress-view", "downloadAuditReport()"))
        self.assertTrue(download.value.suggested_filename.endswith(".csv"))
        with self.page.expect_response(lambda r: "/api/verify_transfer" in r.url):
            self.click(on("progress-view", "runVerificationChecker()"))
        self.click("#done-btn")
        self.assertView("setup-view")
        self.assertNoJsErrors()

    def test_verification_repair_button(self):
        source, dest, _ = self.run_transfer()
        victim = next(os.path.join(d, f) for d, _, fs in os.walk(dest)
                      for f in fs if f == "report.pdf")
        os.remove(victim)  # a copy lost at the destination (inside the sandbox)
        with self.page.expect_response(lambda r: "/api/verify_transfer" in r.url):
            self.click(on("progress-view", "runVerificationChecker()"))
        self.page.wait_for_selector("#repair-resync-btn")
        with self.page.expect_response(lambda r: "/api/start" in r.url):
            self.click("#repair-resync-btn")
        self.wait_for("isTransferActive === false && pollInterval === null")
        self.assertTrue(os.path.exists(victim), "the repair did not copy the file again: %s"
                        % self.dialogs)
        self.assertNoJsErrors()

    def test_cancel_button_during_a_run(self):
        saved = app_mod.OrganizerAPI
        app_mod.OrganizerAPI = HeldRun
        self.addCleanup(setattr, app_mod, "OrganizerAPI", saved)
        HeldRun.go.clear()
        self.addCleanup(HeldRun.go.set)
        self.open_app()
        self.fill_setup(make_source_drive(self.path("source")), self.path("dest", "Sorted"),
                        preview=False)
        self.click("#start-btn")
        self.wait_for("isTransferActive === true")
        self.click("#cancel-btn")
        self.assertEqual(self.server.wait_run_ended(), "cancelled")
        self.wait_for("pollInterval === null")
        self.assertEqual(self.page.inner_text("#status-heading").strip(), "Operation Cancelled")
        self.assertNoJsErrors()

    def test_history_screen(self):
        _, dest, _ = self.run_transfer()
        self.click("#crumb-history")
        self.page.wait_for_selector(".history-csv-btn")
        with self.page.expect_download() as download:
            self.click(".history-csv-btn")
        self.assertTrue(download.value.suggested_filename.endswith(".csv"))
        with self.page.expect_response(lambda r: "/api/open_finder" in r.url):
            self.click(".history-finder-btn")
        self.assertIn(["open", "--", os.path.realpath(dest)], self.server.recorder.calls)
        with self.page.expect_response(lambda r: "/api/verify_transfer" in r.url):
            self.click(".history-verify-btn")
        self.assertView("progress-view")
        self.click("#crumb-history")
        self.page.wait_for_selector(".history-csv-btn")
        with self.page.expect_response(lambda r: "/api/clear_history" in r.url):
            self.click(on("history-view", "clearHistoryLog()"))
        self.page.wait_for_timeout(300)
        self.assertEqual(self.page.locator(".history-csv-btn").count(), 0)
        self.assertNoJsErrors()

    # -- the duplicates utility ---------------------------------------------

    def duplicate_folder(self, groups):
        root = self.path("dups")
        for i in range(groups):
            data = b"group %d " % i * 40
            write(os.path.join(root, "a", "file%d.txt" % i), data)
            write(os.path.join(root, "b", "file%d copy.txt" % i), data)
        write(os.path.join(root, "photos", "p.jpg"), b"jpeg" * 300)
        write(os.path.join(root, "photos", "p copy.jpg"), b"jpeg" * 300)
        return root

    def open_duplicates(self, root):
        self.open_app()
        self.click(on("guide-view", GO_DUPS))
        self.pick(root, on("duplicates-view", "selectDupFolder()"))
        self.assertEqual(self.page.input_value("#dup-source-path"), root)

    def test_duplicates_screen(self):
        self.open_duplicates(self.duplicate_folder(151))  # 152 groups; 150 are shown first
        self.click("#start-dup-scan-btn")
        self.page.wait_for_selector(".dup-checkbox", timeout=60000)
        self.click(on("duplicates-view", "showMoreDuplicateGroups()"))
        self.assertEqual(self.page.locator("[onclick='showMoreDuplicateGroups()']").count(), 0)
        self.page.select_option("#dup-type-filter", "photo")
        self.assertEqual(self.page.locator(".dup-reveal-btn").count(), 2)
        self.page.select_option("#dup-type-filter", "all")
        self.page.type("#dup-search-input", "file7")
        self.page.fill("#dup-search-input", "")
        self.click(on("duplicates-view", "selectAllDuplicates(true)"))
        self.click(on("duplicates-view", "selectAllDuplicates(false)"))
        with self.page.expect_response(lambda r: "/api/dup_reveal" in r.url):
            self.click(".dup-reveal-btn")
        self.assertEqual(len(self.server.recorder.commands("open")), 1)

        for button in ("#dup-quarantine-btn", "#dup-perm-delete-btn"):
            self.click(on("duplicates-view", "selectAllDuplicates(false)"))
            self.page.locator(".dup-checkbox").nth(1).check()
            with self.page.expect_response(lambda r: "/api/dup_trash_inplace" in r.url):
                self.click(button)
            self.page.wait_for_timeout(200)
        self.assertIn("Successfully deleted 1 ", self.last_dialog())
        with self.page.expect_response(lambda r: "/api/dup_empty_trash" in r.url):
            self.click("#empty-dup-trash-btn")
        self.wait_for_dialog("Emptied .Duplicates_Trash")
        self.assertNoJsErrors()

    def test_duplicate_scan_cancel_button(self):
        saved = app_mod.OrganizerAPI
        app_mod.OrganizerAPI = HeldDupScan
        self.addCleanup(setattr, app_mod, "OrganizerAPI", saved)
        self.open_duplicates(self.duplicate_folder(2))
        self.click("#start-dup-scan-btn")
        cancel = self.page.locator("[onclick='cancelDuplicateScan()']")
        cancel.wait_for()
        cancel.click()
        self.assertTrue(self.server.wait_idle(10))
        with app_mod.state_lock:
            self.assertEqual(app_mod.state.dup_status, "cancelled")
        self.assertNoJsErrors()

    # -- coverage -------------------------------------------------------------

    def test_zz_every_control_in_the_page_was_used(self):
        """Runs last (unittest sorts tests by name) and needs the whole class."""
        others = set(unittest.TestLoader().getTestCaseNames(type(self))) - {self._testMethodName}
        if not others <= type(self).ran:
            self.skipTest("run the whole class; not run: %s" % sorted(others - type(self).ran))
        self.open_app()
        static = self.page.evaluate(STATIC_CONTROLS)
        self.assertEqual(len(static), len(set(static)), "two controls share a name: %s" % static)
        self.assertEqual(sorted((set(static) | set(DYNAMIC)) - type(self).used), [],
                         "controls that no test used")


class ClickEverythingDarkTest(ClickEverythingTest):
    """The same walk in dark mode. Playwright clicks only controls that are
    visible and not covered, so this also checks dark mode's layering."""

    dark = True


if __name__ == "__main__":
    unittest.main()
