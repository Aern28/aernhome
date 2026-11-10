# created-by: opus
# created: 2026-11-09
# purpose: keep_sync keeps one Keep line per Nexus row - re-adds never revive or tick old lines; retyped open lines relabel Nexus
# lifespan: helper
# project: household-restock
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import keep_sync as ks


class FakeNexus:
    """Same add() contract as nexus_writes.add_restock: open same name+category -> same id,
    closed -> reopen same id, else a new row."""

    def __init__(self):
        self.rows = {}   # id -> {"item","category","done"}
        self.next_id = 1

    def open_items(self):
        return {i: {"id": i, "item": r["item"], "category": r["category"]}
                for i, r in self.rows.items() if not r["done"]}

    def add(self, item, category):
        for i in sorted(self.rows, reverse=True):
            r = self.rows[i]
            if r["item"].lower() == item.strip().lower() and r["category"] == category:
                r["done"] = False
                return i
        i = self.next_id
        self.next_id += 1
        self.rows[i] = {"item": item.strip(), "category": category, "done": False}
        return i

    def done(self, rid):
        if rid in self.rows and not self.rows[rid]["done"]:
            self.rows[rid]["done"] = True
            return 1
        return 0


class FakeKeep:
    def __init__(self):
        self.lines = {}
        self.n = 0

    def items(self):
        return {k: dict(v) for k, v in self.lines.items()}

    def add(self, text):
        self.n += 1
        kid = f"k{self.n}"
        self.lines[kid] = {"id": kid, "text": text, "checked": False}
        return kid

    def set_checked(self, kid, checked):
        self.lines[kid]["checked"] = checked

    def set_text(self, kid, text):
        self.lines[kid]["text"] = text

    def delete(self, kid):
        del self.lines[kid]

    def commit(self):
        pass


class KeepSyncTest(unittest.TestCase):
    def setUp(self):
        self.nexus, self.keep, self.state, self.log = FakeNexus(), FakeKeep(), {"links": {}}, []

    def run_sync(self, times=1):
        for _ in range(times):
            ks.reconcile(self.nexus, self.keep, self.state, self.log.append)

    def checked(self):
        return {k: v["checked"] for k, v in self.keep.lines.items()}

    def test_readd_after_buying_does_not_untick_the_old_line(self):
        old = self.keep.add("milk")
        self.run_sync()
        self.keep.set_checked(old, True)           # bought it
        self.run_sync()
        self.assertEqual(self.nexus.open_items(), {})
        new = self.keep.add("milk")                # need it again
        self.run_sync(3)
        self.assertTrue(self.keep.lines[old]["checked"], "old ticked line was revived")
        self.assertFalse(self.keep.lines[new]["checked"])
        self.assertEqual(len(self.nexus.open_items()), 1)

    def test_ticking_the_new_line_leaves_nothing_else_touched(self):
        a = self.keep.add("apple juice")
        self.run_sync()
        self.keep.set_checked(a, True)
        self.run_sync()
        b = self.keep.add("apple juice")
        self.run_sync()
        self.keep.set_checked(b, True)
        self.run_sync(2)
        self.assertEqual(self.checked(), {a: True, b: True})
        self.assertEqual(self.nexus.open_items(), {})

    def test_second_open_line_is_left_alone_and_never_ticked(self):
        a = self.keep.add("Lucky Charms")
        self.run_sync()
        b = self.keep.add("Lucky Charms")          # two people add it the same day
        self.run_sync(2)
        self.assertEqual(self.checked(), {a: False, b: False})
        self.assertEqual(len(self.nexus.open_items()), 1)
        self.keep.set_checked(a, True)             # one box bought; b still says "need"
        self.run_sync(3)
        self.assertFalse(self.keep.lines[b]["checked"], "sync ticked a line nobody ticked")
        self.assertEqual([r["item"] for r in self.nexus.open_items().values()], ["Lucky Charms"])

    def test_retyped_open_line_relabels_nexus(self):
        a = self.keep.add("Eggs")
        self.run_sync()
        self.keep.set_text(a, "Eggs (groceries or Costco)")
        self.run_sync(2)
        self.assertEqual([r["item"] for r in self.nexus.open_items().values()], ["Eggs (groceries or Costco)"])
        self.assertEqual(self.keep.lines, {a: {"id": a, "text": "Eggs (groceries or Costco)", "checked": False}},
                         "relabel must not push the old row back into Keep as a new line")
        self.keep.set_checked(a, True)
        self.run_sync()
        self.assertEqual(self.nexus.open_items(), {})

    def test_retyped_line_ticks_only_itself(self):
        sheets = self.keep.add("Jaina bedsheets")
        reused = self.keep.add("Jaina bedsheets")
        self.run_sync()
        self.keep.set_text(reused, "Milk")         # line retyped into another item
        self.run_sync()
        self.keep.set_checked(reused, True)
        self.run_sync(2)
        self.assertFalse(self.keep.lines[sheets]["checked"], "bedsheets ticked because milk was bought")
        self.assertEqual(set(self.keep.lines), {sheets, reused}, "sync added a line nobody asked for")

    def test_existing_double_links_are_repaired(self):
        a, b = self.keep.add("grapes"), self.keep.add("grapes")
        self.keep.set_checked(a, True)
        self.keep.set_checked(b, True)
        rid = self.nexus.add("grapes", "grocery")
        self.nexus.done(rid)
        self.state["links"] = {k: {"nexus_id": rid, "nexus_open": False, "keep_checked": True, "text": "grapes"}
                               for k in (a, b)}
        self.run_sync()
        self.assertEqual(len(self.state["links"]), 1)
        self.nexus.add("grapes", "grocery")        # Aernbot "add grapes" reopens the row
        self.run_sync(2)
        self.assertEqual(sum(not c for c in self.checked().values()), 1, "reopen must revive one line, not both")

    def test_pet_tags_still_route_and_dont_count_as_duplicates(self):
        r = self.keep.add("dry food (Rowan)")
        self.run_sync()
        j = self.keep.add("dry food (Jace)")
        self.run_sync(2)
        cats = sorted(row["category"] for row in self.nexus.open_items().values())
        self.assertEqual(cats, ["jace", "rowan"])
        self.assertEqual(self.checked(), {r: False, j: False})


if __name__ == "__main__":
    unittest.main()
