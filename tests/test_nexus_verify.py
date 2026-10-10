# created-by: opus
# created: 2026-10-09
# purpose: unit tests for nexus_verify probes + feature-map integrity (stdlib only, no app import)
# lifespan: helper
# project: nexus-verify
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nexus_verify as nv  # noqa: E402

KINDS = {"fleet_check", "file_age", "glob_newest", "sqlite_max", "json_field", "dir_present", "nonempty", "user_store"}


class MapIntegrity(unittest.TestCase):
    def setUp(self):
        self.m = nv.load_map()

    def test_paths_unique_and_not_excluded(self):
        paths = [p["path"] for p in self.m["pages"]]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertFalse(set(paths) & set(self.m["excluded"]))

    def test_every_source_is_a_known_kind_with_unique_id(self):
        for p in self.m["pages"]:
            ids = [s["id"] for s in p["sources"]]
            self.assertEqual(len(ids), len(set(ids)), p["path"])
            for s in p["sources"]:
                self.assertIn(s["kind"], KINDS, f"{p['path']}:{s['id']}")

    def test_fleet_check_sources_name_real_checks(self):
        with open(os.path.join(nv.HERE, "fleet.py"), encoding="utf-8") as f:
            src = f.read()
        for p in self.m["pages"]:
            for s in p["sources"]:
                if s["kind"] == "fleet_check":
                    self.assertIn(f'"{s["check"]}":', src, s["check"])


class Probes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _file(self, name, age_h=0.0, text="x"):
        p = os.path.join(self.d, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        t = time.time() - age_h * 3600
        os.utime(p, (t, t))
        return p

    def test_file_age_pass_stale_dead(self):
        fresh, old = self._file("a", 1), self._file("b", 50)
        self.assertEqual(nv.probe({"kind": "file_age", "file": fresh, "stale_after_h": 2}, {})[0], "PASS")
        self.assertEqual(nv.probe({"kind": "file_age", "file": old, "stale_after_h": 2}, {})[0], "STALE")
        self.assertEqual(nv.probe({"kind": "file_age", "file": os.path.join(self.d, "nope")}, {})[0], "DEAD")

    def test_glob_newest_uses_newest(self):
        self._file("r_1.json", 100)
        self._file("r_2.json", 1)
        v, ev = nv.probe({"kind": "glob_newest", "glob": os.path.join(self.d, "r_*.json"), "stale_after_h": 30}, {})
        self.assertEqual(v, "PASS")
        self.assertIn("r_2.json", ev)
        self.assertEqual(nv.probe({"kind": "glob_newest", "glob": os.path.join(self.d, "z_*")}, {})[0], "DEAD")

    def test_sqlite_max_naive_timestamp_is_utc(self):
        db = os.path.join(self.d, "t.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE t (at TIMESTAMP)")
        old = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
        con.execute("INSERT INTO t VALUES (?)", (old,))
        con.commit()
        con.close()
        src = {"kind": "sqlite_max", "db": db, "sql": "SELECT MAX(at) FROM t"}
        self.assertEqual(nv.probe({**src, "stale_after_h": 6}, {})[0], "PASS")
        self.assertEqual(nv.probe({**src, "stale_after_h": 4}, {})[0], "STALE")

    def test_sqlite_max_empty_table_is_dead(self):
        db = os.path.join(self.d, "e.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE t (at TIMESTAMP)")
        con.commit()
        con.close()
        self.assertEqual(nv.probe({"kind": "sqlite_max", "db": db, "sql": "SELECT MAX(at) FROM t", "stale_after_h": 1}, {})[0], "DEAD")

    def test_json_field_dotted(self):
        ts = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        p = self._file("a.json", text=json.dumps({"daily": {"updated_at": ts}}))
        src = {"kind": "json_field", "file": p, "field": "daily.updated_at"}
        self.assertEqual(nv.probe({**src, "stale_after_h": 26}, {})[0], "STALE")
        self.assertEqual(nv.probe({**src, "field": "weekly.updated_at", "stale_after_h": 26}, {})[0], "DEAD")

    def test_dir_present(self):
        self.assertEqual(nv.probe({"kind": "dir_present", "dir": self.d}, {})[0], "DEAD")  # empty
        self._file("x")
        self.assertEqual(nv.probe({"kind": "dir_present", "dir": self.d}, {})[0], "PASS")
        self.assertEqual(nv.probe({"kind": "dir_present", "dir": os.path.join(self.d, "no")}, {})[0], "DEAD")

    def test_nonempty_catches_empty_as_dead(self):
        self.assertEqual(nv.probe({"kind": "nonempty", "call": "os:listdir"}, {})[0], "PASS")
        self.assertEqual(nv.probe({"kind": "nonempty", "call": "builtins:list"}, {})[0], "DEAD")

    def test_fleet_check_maps_status(self):
        fc = {"a": {"status": "warn", "detail": "old"}, "b": {"status": "down"}}
        self.assertEqual(nv.probe({"kind": "fleet_check", "check": "a"}, fc)[0], "STALE")
        self.assertEqual(nv.probe({"kind": "fleet_check", "check": "b"}, fc)[0], "DEAD")
        self.assertEqual(nv.probe({"kind": "fleet_check", "check": "zz"}, fc)[0], "UNKNOWN")

    def test_probe_never_raises(self):
        self.assertEqual(nv.probe({"kind": "sqlite_max", "db": self._file("bad.db", text="not sqlite"), "sql": "SELECT 1"}, {})[0], "UNKNOWN")
        self.assertEqual(nv.probe({"kind": "bogus"}, {})[0], "UNKNOWN")


class FallbackDoesNotSetVerdict(unittest.TestCase):
    def test_dead_fallback_leaves_page_pass(self):
        class R:
            status_code = 200

            def get_data(self, as_text=True):
                return "ok"

        class C:
            def get(self, path, headers=None):
                r = R()
                if headers:
                    r.status_code = 404
                return r

        class A:
            _rule = type("Rule", (), {"rule": "/p", "methods": {"GET"}})()
            url_map = type("M", (), {"iter_rules": staticmethod(lambda: [A._rule])})()

            def test_client(self):
                return C()

        with tempfile.TemporaryDirectory() as d:
            mp = os.path.join(d, "m.json")
            with open(mp, "w", encoding="utf-8") as f:
                json.dump({"pages": [{"path": "/p", "sources": [
                    {"id": "fb", "kind": "dir_present", "dir": os.path.join(d, "none"), "fallback": True}]}],
                    "excluded": {}}, f)
            orig = nv._get_app
            nv._get_app = lambda: A()
            try:
                res = nv.verify(map_path=mp)
            finally:
                nv._get_app = orig
        self.assertEqual(res["pages"][0]["verdict"], "PASS")
        self.assertEqual(res["pages"][0]["sources"][0]["verdict"], "DEAD")
        self.assertEqual(res["worst"], "PASS")


class Summary(unittest.TestCase):
    def test_summary_names_worst_first_and_drift(self):
        res = {"pages": [
            {"path": "/a", "verdict": "STALE", "sources": [{"id": "s1", "verdict": "STALE"}]},
            {"path": "/b", "verdict": "DEAD", "sources": [{"id": "s2", "verdict": "DEAD"}]},
            {"path": "/c", "verdict": "PASS", "sources": []}],
            "unmapped": ["/new"], "gone": []}
        line = nv.summary(res)
        self.assertTrue(line.startswith("1/3 pages PASS"))
        self.assertLess(line.index("/b DEAD"), line.index("/a STALE"))
        self.assertIn("DRIFT unmapped: /new", line)


if __name__ == "__main__":
    unittest.main()
