# created-by: opus
# created: 2026-10-09
# purpose: enforce the Nexus connector contract - every connector registered, empty always carries a reason
# lifespan: helper
# project: nexus-verify
import ast
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import nexus_contract as nc  # noqa: E402
import nexus_sources as ns  # noqa: E402

REQUIRED = {"name", "data", "as_of", "source", "freshness", "empty_reason", "warning"}


def _public_defs():
    with open(os.path.join(ROOT, "nexus_sources.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    return {n.name for n in tree.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}


class Registry(unittest.TestCase):
    def test_every_public_connector_is_registered(self):
        missing = _public_defs() - set(nc.CONNECTORS) - nc.NOT_CONNECTORS
        self.assertFalse(missing, f"register in nexus_contract.CONNECTORS or NOT_CONNECTORS: {sorted(missing)}")

    def test_no_stale_registrations(self):
        gone = (set(nc.CONNECTORS) | nc.NOT_CONNECTORS) - _public_defs()
        self.assertFalse(gone, f"registered but not in nexus_sources: {sorted(gone)}")

    def test_specs_complete(self):
        for name, spec in nc.CONNECTORS.items():
            self.assertIn(spec["freshness"], {"live", "data", "user"}, name)
            self.assertTrue(spec["source"], name)
            self.assertTrue(callable(spec["call"]) and callable(spec["as_of"]), name)
            if spec["freshness"] == "live":
                self.assertTrue(spec.get("health"), f"{name}: live connectors must record health")


def _fake(data, freshness="live", health=None, as_of=None, retired=None, raises=False):
    def call():
        if raises:
            raise RuntimeError("boom")
        return data
    spec = {"call": call, "freshness": freshness, "source": "fake", "as_of": lambda d: as_of}
    if health is not None:
        spec["health"] = "fake"
        ns._mark("fake", *health)
    if retired:
        spec["retired"] = retired
    return spec


class Envelope(unittest.TestCase):
    def run_fake(self, spec):
        with mock.patch.dict(nc.CONNECTORS, {"fake": spec}):
            env = nc.envelope("fake")
        self.assertEqual(set(env), REQUIRED)
        return env

    def test_api_failure_is_not_all_clear(self):
        env = self.run_fake(_fake([], health=(False, "error: ConnectionError")))
        self.assertEqual(env["empty_reason"], "error: ConnectionError")

    def test_missing_token_is_source_missing(self):
        env = self.run_fake(_fake({}, health=(False, "source_missing: no token")))
        self.assertTrue(env["empty_reason"].startswith("source_missing"))

    def test_genuinely_nothing_due(self):
        env = self.run_fake(_fake([], health=(True, "")))
        self.assertEqual(env["empty_reason"], "none_due")

    def test_data_connector_empty_without_timestamp_is_missing(self):
        env = self.run_fake(_fake([], freshness="data"))
        self.assertTrue(env["empty_reason"].startswith("source_missing"))

    def test_retired_leg_says_so(self):
        env = self.run_fake(_fake([], freshness="data", retired="mount gone"))
        self.assertEqual(env["empty_reason"], "source_missing: mount gone")

    def test_connector_that_raises(self):
        env = self.run_fake(_fake(None, raises=True))
        self.assertTrue(env["empty_reason"].startswith("error"))

    def test_partial_read_is_a_warning_with_data(self):
        env = self.run_fake(_fake([1], health=(True, "partial: stopped at 10 pages")))
        self.assertIsNone(env["empty_reason"])
        self.assertEqual(env["warning"], "partial: stopped at 10 pages")

    def test_dict_of_zeros_is_empty(self):
        self.assertTrue(nc._is_empty({"a": 0, "b": [], "c": None}))
        self.assertFalse(nc._is_empty({"a": 271.0}))

    def test_bare_date_is_central_midnight(self):
        d = nc._parse("2026-10-09")
        self.assertEqual((d.year, d.month, d.day, d.hour), (2026, 10, 9, 0))
        self.assertEqual(str(d.tzinfo), "America/Chicago")


if __name__ == "__main__":
    unittest.main()
