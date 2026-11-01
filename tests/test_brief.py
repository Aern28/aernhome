# created-by: opus
# created: 2026-11-01
# purpose: Brief page + API: post an entry, list it, mark read/useful/skip/note, reject non-brief ids
# lifespan: helper
# project: digest-synthesis
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp()
os.environ["DATA_DIR"] = _TMP

import app as nexus_app  # noqa: E402


class BriefTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        nexus_app.init_db()
        nexus_app.init_nexus_db()
        cls.c = nexus_app.app.test_client()

    def post_entry(self, source="brief-market", title="Hiyori ran", body="**EB01-013** $2.96 -> $17.58"):
        r = self.c.post("/api/nexus/feed", json={"source": source, "title": title, "body": body, "tag": "market"})
        self.assertEqual(r.status_code, 200)
        return r.get_json()["id"]

    def test_flow(self):
        fid = self.post_entry()
        other = self.post_entry(source="optcg", title="digest")
        page = self.c.get("/nexus/brief")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("Hiyori ran", html)
        self.assertIn("<strong>EB01-013</strong>", html)
        self.assertNotIn(">digest<", html)  # non-brief feed items stay off the page

        items = self.c.get("/api/nexus/brief?unread=1").get_json()["items"]
        self.assertEqual([i["id"] for i in items], [fid])
        self.assertEqual(items[0]["beat"], "market")

        self.assertTrue(self.c.post("/api/nexus/brief/mark", json={"id": fid, "verdict": "skip", "read": True,
                                                                    "note": "don't care about promos"}).get_json()["ok"])
        self.assertEqual(self.c.get("/api/nexus/brief?unread=1").get_json()["items"], [])
        it = self.c.get("/api/nexus/brief?beat=market").get_json()["items"][0]
        self.assertEqual((it["verdict"], it["note"]), ("skip", "don't care about promos"))
        self.assertIsNotNone(it["read_at"])

        # clearing the verdict leaves read + note alone
        self.c.post("/api/nexus/brief/mark", json={"id": fid, "verdict": ""})
        it = self.c.get("/api/nexus/brief?beat=market").get_json()["items"][0]
        self.assertEqual((it["verdict"], it["note"]), (None, "don't care about promos"))

        bad = self.c.post("/api/nexus/brief/mark", json={"id": other, "read": True})
        self.assertEqual(bad.status_code, 400)
        bad = self.c.post("/api/nexus/brief/mark", json={"id": fid, "verdict": "love"})
        self.assertEqual(bad.status_code, 400)

    def test_public_tunnel_refused(self):
        r = self.c.get("/nexus/brief", headers={"CF-Connecting-IP": "1.2.3.4"})
        self.assertEqual(r.status_code, 404)
        r = self.c.post("/api/nexus/brief/mark", json={"id": 1, "read": True}, headers={"CF-Connecting-IP": "1.2.3.4"})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
