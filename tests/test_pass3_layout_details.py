"""P3-08: small layout defects on the setup and progress screens.

- The nav's Back button showed its arrow above the word "Back": showView set
  its display to inline-block, which undid the button's flex layout.
- "Run Integrity Verification Checker" had no icon: `check-shield` is not a
  Lucide icon name (`shield-check` is), so Lucide left the placeholder empty.
- The progress heading's spinner had two class attributes; browsers keep the
  first, so it lost its size, colour and spin.
"""

import os
import unittest
from html.parser import HTMLParser

from pass3_helpers import UITestCase

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "src", "templates", "index.html")


class _DuplicateAttributes(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found = []

    def handle_starttag(self, tag, attrs):
        names = [name for name, _ in attrs]
        for name in set(names):
            if names.count(name) > 1:
                self.found.append((self.getpos()[0], tag, name))


class TemplateTest(unittest.TestCase):

    def test_no_element_has_an_attribute_twice(self):
        parser = _DuplicateAttributes()
        with open(TEMPLATE, encoding="utf-8") as f:
            parser.feed(f.read())
        self.assertEqual(parser.found, [], "(line, tag, attribute) given twice")


class LayoutDetailsTest(UITestCase):

    def test_back_button_arrow_sits_beside_its_label(self):
        self.open_app()
        self.click("#crumb-setup")
        box = self.page.evaluate("""() => {
            const btn = document.getElementById('nav-back-btn');
            const icon = btn.querySelector('svg');
            const text = [...btn.childNodes].find(n => n.nodeType === 3 && n.textContent.trim());
            const range = document.createRange(); range.selectNodeContents(text);
            const r = e => { const b = e.getBoundingClientRect();
                             return {left: b.left, right: b.right, mid: (b.top + b.bottom) / 2}; };
            return {icon: r(icon), text: r(range)};
        }""")
        self.assertLessEqual(box["icon"]["right"], box["text"]["left"] + 1,
                             "the arrow is not to the left of 'Back': %s" % box)
        self.assertLess(abs(box["icon"]["mid"] - box["text"]["mid"]), 6,
                        "the arrow is not on the same line as 'Back': %s" % box)
        self.assertNoJsErrors()

    def test_every_icon_in_the_page_exists(self):
        self.open_app()
        missing = self.page.evaluate(
            "[...document.querySelectorAll('i[data-lucide]')].map(e => e.dataset.lucide)")
        self.assertEqual(missing, [], "Lucide has no icon with these names")
        self.assertEqual([w for w in self.js_warnings if "icon name was not found" in w], [])
        self.assertNoJsErrors()


if __name__ == "__main__":
    unittest.main()
