"""Pass-1 audit, finding P1-03: a crash left a half-copied code project.

copy_project_intact() copied a code project straight into its final folder
(Code/<name>), file by file. A crash or power cut in the middle left
Code/<name> with some files missing and one truncated. The next run did not
recognise it as a copy of the project, so it copied the project again as
Code/<name>_1 and left the broken folder behind, where it looks like a real
project that the owner might keep (or dissolve into the library) instead of
the good copy.

After the fix, the project is copied into a hidden staging folder
(Code/.<name>.organizer-partial) and renamed into place only when complete.

Run with:  .venv/bin/python -m unittest tests.test_pass1_project_copy_atomic -v
"""

import filecmp
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pass1_helpers import Pass1Case, unique_payload, write_file  # noqa: E402
from src.file_ops import project_already_copied  # noqa: E402


class ProjectCopyAtomicTests(Pass1Case):

    def _make_project(self):
        proj = os.path.join(self.src, "webapp")
        write_file(os.path.join(proj, "package.json"), b'{"name": "webapp"}')
        write_file(os.path.join(proj, ".git", "HEAD"), b"ref: refs/heads/main\n")
        for i in range(6):
            write_file(os.path.join(proj, "src", f"module{i}.js"),
                       unique_payload(f"module{i}", 50_000))
        return proj

    def _assert_faithful_copy(self, proj, copy):
        cmp = filecmp.dircmp(proj, copy)
        stack = [cmp]
        while stack:
            c = stack.pop()
            self.assertEqual(c.left_only + c.right_only, [], f"file sets differ in {c.right}")
            _match, mismatch, errors = filecmp.cmpfiles(c.left, c.right, c.common_files, shallow=False)
            self.assertEqual(mismatch + errors, [], f"file contents differ in {c.right}")
            stack.extend(c.subdirs.values())

    def test_crash_during_project_copy_leaves_no_half_copied_project(self):
        proj = self._make_project()
        final = os.path.join(self.dest, "Code", "webapp")

        # Projects are copied before loose files; the 4th file write is cut
        # off halfway and the process dies.
        self.run_until_crash(crash_after=3)

        if os.path.exists(final):
            self.assertTrue(project_already_copied(proj, final),
                            "a half-copied project was left under its final name")

        self.organize(mode="merge")

        self.assertEqual(sorted(os.listdir(os.path.join(self.dest, "Code"))), ["webapp"],
                         "the resume left a broken copy behind and/or made a second one")
        self._assert_faithful_copy(proj, final)

    def test_leftover_staging_folder_is_not_offered_as_a_project(self):
        code = os.path.join(self.dest, "Code")
        write_file(os.path.join(code, ".webapp.organizer-partial", "package.json"), b"{")
        write_file(os.path.join(code, "other", "main.py"), b"print(1)")

        names = [p["name"] for p in self.make_api().list_code_projects(self.dest)]

        self.assertEqual(names, ["other"])
