# created-by: opus
# created: 2026-10-12
# purpose: unit tests for notebook_page (entry parsing, thread mapping, opinion marking, sanitizing)
# lifespan: helper
# project: notebook-page
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import notebook_page as nb  # noqa: E402

THREADS = {"night-sky": {"name": "Night sky", "question": "q", "status": "open", "colour": "#7fa7d9"},
           "gulf-garden": {"name": "Gulf garden", "question": "", "status": "open", "colour": "#9cbf6a"}}

SAMPLE = """# Notebook
preamble that is not an entry
<!-- ENTRIES BELOW - newest first -->

## 2026-10-10 06:32 · Gardening · Older one

Lead paragraph.

Second paragraph with `code` and a list:

- one
- two

My read: this is opinion.

> my read: explicit marker.

Thread: gulf-garden

---

## 2026-10-11 10:20 · Saturn musing · Newer one [find]

Only a lead. <script>alert(1)</script>

---

## not-a-date · x · y

skipped
"""


class Parse(unittest.TestCase):
    def setUp(self):
        self.entries = nb.parse(SAMPLE, THREADS)

    def test_newest_first_and_bad_header_skipped(self):
        self.assertEqual([e["title"] for e in self.entries], ["Newer one", "Older one"])

    def test_thread_line_wins_then_topic_hint(self):
        self.assertEqual(self.entries[1]["slug"], "gulf-garden")
        self.assertEqual(self.entries[0]["slug"], "night-sky")   # "Saturn" -> night-sky hint

    def test_unknown_topic_is_wandering(self):
        e = nb.parse("## 2026-10-01 09:00 · Soken · Music\n\nBody.\n", THREADS)[0]
        self.assertEqual(e["slug"], "wander")

    def test_lead_rest_split_and_thread_line_removed(self):
        older = self.entries[1]
        self.assertEqual(older["lead_html"], "<p>Lead paragraph.</p>")
        self.assertNotIn("Thread:", older["rest_html"])
        self.assertEqual(self.entries[0]["rest_html"], "")

    def test_markdown_renders(self):
        rest = self.entries[1]["rest_html"]
        self.assertIn("<code>code</code>", rest)
        self.assertIn("<li>one</li>", rest)

    def test_opinion_marked_both_ways(self):
        rest = self.entries[1]["rest_html"]
        self.assertIn('<p class="nb-mine">My read: this is opinion.</p>', rest)
        self.assertIn("<blockquote>", rest)

    def test_script_is_stripped(self):
        self.assertNotIn("<script", self.entries[0]["lead_html"])

    def test_find_suffix_dropped(self):
        self.assertEqual(self.entries[0]["title_html"], "Newer one")


if __name__ == "__main__":
    unittest.main()
