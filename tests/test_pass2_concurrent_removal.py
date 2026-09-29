"""P2-04: two removal requests running at the same time could delete every copy.

The app builds a new OrganizerAPI for each request and Flask serves requests
in parallel. Each request checked that the copy it keeps still exists, then
removed its own selection, with nothing stopping two requests from
interleaving. Request 1 removing `b` (keeping `a`) and request 2 removing `a`
(keeping `b`) could both pass their check before either removed anything.
"""

import os
import threading
import unittest
from unittest import mock

import src.file_ops as file_ops
from pass2_helpers import Pass2Case, payload, read_file, write_file


class ConcurrentRemovalTest(Pass2Case):

    def test_concurrent_requests_cannot_remove_every_copy(self):
        data = payload("p2-04 contract", 4096)
        a = write_file(self.path("a", "contract.pdf"), data)
        b = write_file(self.path("b", "contract.pdf"), data)
        groups = self.scan()
        self.assertEqual(len(groups), 1)

        # Hold each request right before it removes its file until the other
        # request gets there too, or 1 s passes. Serialised requests simply
        # continue after the timeout.
        real_remove = file_ops._force_remove
        arrived = []
        cond = threading.Condition()

        def remove_when_both_arrive(path):
            with cond:
                arrived.append(path)
                cond.notify_all()
                cond.wait_for(lambda: len(arrived) >= 2, timeout=1.0)
            return real_remove(path)

        results = {}

        def request(name, selection):
            results[name] = self.api().trash_inplace_duplicates(
                selection, self.root, permanent_delete=True, groups=groups)

        with mock.patch.object(file_ops, "_force_remove", remove_when_both_arrive):
            threads = [threading.Thread(target=request, args=("keep a", [b])),
                       threading.Thread(target=request, args=("keep b", [a]))]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

        survivors = [p for p in (a, b) if os.path.exists(p)]
        self.assertEqual(len(survivors), 1, f"results: {results}")
        self.assertEqual(read_file(survivors[0]), data)
        removed = sum(r[0] for r in results.values())
        self.assertEqual(removed, 1)


if __name__ == "__main__":
    unittest.main()
