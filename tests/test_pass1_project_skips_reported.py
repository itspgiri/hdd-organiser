"""Pass-1 audit, finding P1-11: the preview did not report what a code-project
copy leaves out.

copy_project_intact() leaves out every name in PROJECT_IGNORE_PATTERNS
(node_modules, venv, build, dist, target, ...). The preview's "Skipped ...
will NOT be copied" list is meant to tell the owner about exactly those, so
nothing is lost by wiping the source after organizing. But it reported a
different list (SKIP_BUILD_DIRS): `build`, `dist` and `target` were left out
without being reported, and `Caches` / `.tmp` were reported although they are
copied.

Run with:  .venv/bin/python -m unittest tests.test_pass1_project_skips_reported -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pass1_helpers import CONFIG_PATH, Pass1Case, write_file  # noqa: E402
from src.categorizer import Categorizer  # noqa: E402
from src.scanner import Scanner  # noqa: E402

PROJECT_FILES = {
    ".git/HEAD": b"ref: refs/heads/main\n",
    "main.py": b"print('hi')\n",
    "build": b"#!/bin/sh\n# the project's own build script\n",
    "dist/MyApp-1.0.dmg": b"the release that was shipped",
    "target/design-targets.xlsx": b"quarterly targets",
    "node_modules/pkg/index.js": b"// dependency",
    "Caches/notes.txt": b"notes in a folder named Caches",
}


class ProjectSkipsReportedTests(Pass1Case):

    def test_preview_reports_exactly_what_the_project_copy_leaves_out(self):
        proj = os.path.join(self.src, "my_app")
        for rel, data in PROJECT_FILES.items():
            write_file(os.path.join(proj, rel), data)

        scanner = Scanner(Categorizer(CONFIG_PATH), staging_root=os.path.join(self.tmp, "staging"))
        scanner.scan_directory(self.src, is_preview=True)
        self.assertEqual(scanner.projects_found, [proj])
        reported = set(scanner.skipped_dir_breakdown)

        self.organize()
        dest_proj = os.path.join(self.dest, "Code", "my_app")
        left_out = {rel.split("/")[0] for rel in PROJECT_FILES
                    if not os.path.exists(os.path.join(dest_proj, rel))}

        self.assertEqual(sorted(left_out - reported), [],
                         "left out of the project copy without being reported by the preview")
        self.assertEqual(sorted(reported - left_out), [],
                         "reported as not copied, but the project copy includes it")
