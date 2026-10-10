# created-by: opus
# created: 2026-10-10
# purpose: /api/needs-aern carries queue asks (ask headline, options, repeat_count, ask_missing) for the /nexus/aern buttons
# lifespan: helper
# project: queue-asks
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import second_brain as sb  # noqa: E402

QUEUE = {"items": [
    {"id": "a1", "dir": "to_aern", "status": "open", "text": "Long body with the evidence",
     "ask": "Rebuild the relay today?", "options": ["Yes, after 18:30", "Tomorrow"],
     "repeat_count": 3, "created_by": "trainer-claude", "priority": 2},
    {"id": "b2", "dir": "to_aern", "status": "open", "text": "Old-style item", "source": "x.log",
     "ask_missing": True, "created_by": "aernbot-bizmail"},
    {"id": "e5", "dir": "to_aern", "status": "open", "text": "Posted before asks existed",
     "created_by": "phoenix-claude"},
    {"id": "c3", "dir": "to_fleet", "status": "open", "text": "not for Aern"},
    {"id": "d4", "dir": "to_aern", "status": "resolved", "text": "closed"},
]}


class NeedsFromQueue(unittest.TestCase):
    def setUp(self):
        p1 = mock.patch.object(sb, "load_queue", return_value=QUEUE)
        p2 = mock.patch.object(sb, "_project_index", return_value={})
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)
        self.items = {i["id"]: i for i in sb._needs_from_queue()}

    def test_only_open_to_aern(self):
        self.assertEqual(set(self.items), {"a1", "b2", "e5"})

    def test_legacy_item_without_flag_is_still_missing_an_ask(self):
        self.assertTrue(self.items["e5"]["ask_missing"])
        self.assertEqual(self.items["e5"]["created_by"], "phoenix-claude")

    def test_ask_is_headline_text_is_detail(self):
        a = self.items["a1"]
        self.assertEqual(a["title"], "Rebuild the relay today?")
        self.assertEqual(a["detail"], "Long body with the evidence")
        self.assertEqual(a["options"], ["Yes, after 18:30", "Tomorrow"])
        self.assertEqual(a["repeat_count"], 3)
        self.assertFalse(a["ask_missing"])

    def test_old_item_keeps_text_and_flags_missing_ask(self):
        b = self.items["b2"]
        self.assertEqual(b["title"], "Old-style item")
        self.assertEqual(b["detail"], "source: x.log")
        self.assertEqual(b["options"], [])
        self.assertEqual(b["repeat_count"], 1)
        self.assertTrue(b["ask_missing"])
        self.assertEqual(b["created_by"], "aernbot-bizmail")


if __name__ == "__main__":
    unittest.main()
