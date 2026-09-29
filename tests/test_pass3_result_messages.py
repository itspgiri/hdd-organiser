"""P3-09: the review tools' results were hard to read.

- Six warnings in the review tools, among them "Verification could not run
  ... Your files were not verified - do not delete the source drive.", were
  meant to be red, but used `var(--danger-color)`, a CSS variable that is
  defined nowhere, so they showed in the ordinary text colour.
- The verification result sits in a box limited to 240 px. After P3-06 made
  the result say what the check does not cover, that note fell below the
  box's edge, behind a scroll bar. (This part is a regression from P3-06:
  the shorter result on 92ed5c1 fitted.)
"""

import unittest

from pass3_helpers import UITestCase

VERIFY_BUTTON = "button:has-text('Run Integrity Verification Checker')"


class ResultMessagesTest(UITestCase):

    def verify(self):
        with self.page.expect_response(lambda r: "/api/verify_transfer" in r.url):
            self.click(VERIFY_BUTTON)
        self.page.wait_for_timeout(200)

    def test_verification_failure_warning_is_red(self):
        self.run_transfer()
        self.page.route("**/api/verify_transfer*",
                        lambda route: route.fulfill(status=500, body="{}"))
        self.verify()
        rgb = self.page.evaluate("""() => {
            const div = document.querySelector('#review-content-area div');
            return getComputedStyle(div).color.match(/\\d+/g).map(Number);
        }""")
        self.assertIn("not verified", self.page.inner_text("#review-content-area"))
        r, g, b = rgb[:3]
        self.assertTrue(r > 150 and g < 110 and b < 110, "warning colour is rgb%s" % (rgb,))
        self.assertNoJsErrors(allowed_http=(500,))

    def test_warning_is_red_in_dark_mode_too(self):
        self.run_transfer(dark=True)
        self.page.route("**/api/verify_transfer*",
                        lambda route: route.fulfill(status=500, body="{}"))
        self.verify()
        rgb = self.page.evaluate("""() => getComputedStyle(
            document.querySelector('#review-content-area div')).color.match(/\\d+/g).map(Number)""")
        r, g, b = rgb[:3]
        self.assertTrue(r > 200 and g < 160 and b < 160, "warning colour is rgb%s" % (rgb,))
        self.assertNoJsErrors(allowed_http=(500,))

    def test_the_whole_verification_result_is_visible(self):
        self.run_transfer()
        self.verify()
        box = self.page.evaluate("""() => {
            const a = document.getElementById('review-content-area');
            return {scroll: a.scrollHeight, client: a.clientHeight, text: a.innerText};
        }""")
        self.assertIn("Not covered by this check", box["text"])
        self.assertLessEqual(box["scroll"], box["client"] + 1,
                             "the result is cut off by its box: %s" % box)
        self.assertNoJsErrors()


if __name__ == "__main__":
    unittest.main()
