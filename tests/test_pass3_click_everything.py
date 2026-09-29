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

# How the recorder names a control: its inline handler plus the screen or
# pop-up it is in, or else its id, or else its *-btn / *-checkbox class.
RECORDER = """(() => {
    const where = el => {
        const v = el.closest('.view, .inspect-modal, nav');
        return v ? (v.id || v.tagName.toLowerCase()) : 'page';
    };
    window.__controlId = el => {
        for (const attr of ['onclick', 'onchange', 'oninput', 'onkeyup']) {
            const h = el.getAttribute(attr);
            if (h) return h + ' @' + where(el);
        }
        if (el.id) return '#' + el.id;
        return '.' + [...el.classList].find(c => /-(btn|checkbox)$/.test(c));
    };
    window.__used = [];
    for (const [type, attr] of [['click', 'onclick'], ['change', 'onchange'],
                                ['input', 'oninput'], ['keyup', 'onkeyup']]) {
        document.addEventListener(type, e => {
            const el = e.target.closest && e.target.closest(`[${attr}], button, input, select`);
            if (el) window.__used.push(window.__controlId(el));
        }, true);
    }
})();"""

STATIC_CONTROLS = ("[...document.querySelectorAll('[onclick],[onchange],[oninput],[onkeyup]')]"
                   ".map(__controlId)")

# Controls that exist only once the page has built them from results.
DYNAMIC = [
    ".category-pill-btn", "#projects-select-all-btn", "#projects-deselect-all-btn",
    "filterProjectsList() @preview-view", ".project-checkbox", ".project-inspect-btn",
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
