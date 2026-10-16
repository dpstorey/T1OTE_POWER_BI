# SPDX-License-Identifier: Apache-2.0
"""@file tests/test_export.py
@brief Offline unit/integration tests for t1ote_export.py.

No network access is needed: every HTTP call is answered by a fake
`httpx.MockTransport` that is swapped in through `t1ote_export.make_client`
(the program's single HTTP-client factory). Run with either:

    python -m unittest discover -s tests -v
    pytest -q
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import t1ote_export as hx  # noqa: E402

SITE_A = "11111111-1111-4111-8111-111111111111"
SITE_B = "22222222-2222-4222-8222-222222222222"


def _asset(i, crit="HighCriticality"):
    """A Tenable asset node in the shape the GraphQL query returns."""
    return {
        "id": f"asset-{i}",
        "name": f"PLC-{i}",
        "vendor": "Siemens",
        "model": "S7-1500",
        "criticality": crit,
        "ips": {"nodes": [f"10.0.0.{i % 250}", "10.0.1.1"]},
        "lastSeen": "2026-10-01T00:00:00Z",
        "risk": {"totalRisk": 12.5},
    }


class FakeWorld:
    """Routes every HTTP request the program makes to a canned answer, and
    records what was sent so tests can assert on it."""

    def __init__(self, sites=None, assets=None):
        self.sites = sites if sites is not None else [("Houston", SITE_A)]
        self.assets = assets if assets is not None else [_asset(i) for i in range(3)]
        self.requests = []          # every httpx.Request seen, in order
        self.rows_pushed = []       # rows received by Power BI
        self.refresh_token_returned = "refresh-2"
        self.tenable_status = 200
        self.tenable_body = None    # override raw body (e.g. HTML)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if "login.microsoftonline.com" in url and url.endswith("/token"):
            return httpx.Response(
                200,
                json={"access_token": "access-1", "refresh_token": self.refresh_token_returned, "expires_in": 3600},
            )
        if "api.powerbi.com" in url:
            if request.method == "POST" and "/datasets" in url and "/tables/" not in url:
                return httpx.Response(201, json={"id": "new-dataset-id"})
            if request.method == "DELETE":
                return httpx.Response(200)
            if request.method == "POST":
                self.rows_pushed.extend(json.loads(request.content)["rows"])
                return httpx.Response(200)
        if self.tenable_status != 200:
            return httpx.Response(self.tenable_status, text="denied")
        if self.tenable_body is not None:
            return httpx.Response(200, text=self.tenable_body)
        payload = json.loads(request.content)
        if "emPairedIcps" in payload["query"]:
            edges = [{"node": {"site": {"machineId": m, "name": n}}} for n, m in self.sites]
            return httpx.Response(200, json={"data": {"emPairedIcps": {"edges": edges}}})
        if "assets(" in payload["query"]:
            v = payload["variables"]
            start = int(v["after"]) if v.get("after") else 0
            page = self.assets[start : start + v["pageSize"]]
            end = start + len(page)
            return httpx.Response(
                200,
                json={
                    "data": {
                        "assets": {
                            "totalCount": len(self.assets),
                            "pageInfo": {"hasNextPage": end < len(self.assets), "endCursor": str(end)},
                            "nodes": page,
                        }
                    }
                },
            )
        return httpx.Response(500, text="unexpected request")

    def make_client(self, verify=True, timeout=30.0):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


class Base(unittest.TestCase):
    """Common setup: temp dir, clean environment, fake HTTP world."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.token_file = Path(self.tmp.name) / "token.json"
        env = {
            "TENABLE_URL": "https://em.example/",
            "TENABLE_API_KEY": "accessKey=a;secretKey=b",
            "AZURE_TENANT_ID": "tenant",
            "AZURE_CLIENT_ID": "client",
            "POWERBI_WORKSPACE_ID": "ws-1",
            "POWERBI_DATASET_ID": "ds-1",
            "TOKEN_CACHE_FILE": str(self.token_file),
        }
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.world = FakeWorld()
        p = mock.patch.object(hx, "make_client", self.world.make_client)
        p.start()
        self.addCleanup(p.stop)
        hx._save_refresh_token(self.token_file, "refresh-1")

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = hx.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class FilterTests(unittest.TestCase):
    def test_no_scope_means_no_filter(self):
        self.assertIsNone(hx.build_filter(None, None, None))

    def test_criticality_includes_levels_above(self):
        f = hx.build_filter("medium", None, None)
        self.assertEqual(f["values"], ["MediumCriticality", "HighCriticality"])

    def test_subnet_uses_first_and_last_address(self):
        f = hx.build_filter(None, "10.253.10.128/25", None)
        self.assertEqual(f, {"field": "ips", "op": "Between", "values": ["10.253.10.128", "10.253.10.255"]})

    def test_ip_range(self):
        f = hx.build_filter(None, None, "10.0.0.10 - 10.0.0.99")
        self.assertEqual(f["values"], ["10.0.0.10", "10.0.0.99"])

    def test_combined_is_anded(self):
        f = hx.build_filter("high", "10.1.0.0/24", None)
        self.assertEqual(f["op"], "And")
        self.assertEqual(len(f["expressions"]), 2)

    def test_bad_input_rejected(self):
        for args in [(None, "10.1.0.0/24", "10.0.0.1-10.0.0.2"), (None, "nonsense", None),
                     (None, None, "10.0.0.9-10.0.0.1"), (None, None, "10.0.0.1"), (None, None, "10.0.0.1-::1")]:
            with self.assertRaises(hx.ExportError, msg=args):
                hx.build_filter(*args)


class RowTests(unittest.TestCase):
    def test_rows_match_table_schema_exactly(self):
        rows = hx.build_rows([_asset(1)], "Houston")
        self.assertEqual(list(rows[0]), [c for c, _ in hx.TABLE_COLUMNS])

    def test_values_are_shaped_for_display(self):
        r = hx.build_rows([_asset(1, "LowCriticality")], "Houston")[0]
        self.assertEqual((r["criticality"], r["site"], r["ips"]), ("Low", "Houston", "10.0.0.1, 10.0.1.1"))

    def test_sparse_asset_does_not_break(self):
        r = hx.build_rows([{"id": "x"}], "S")[0]
        self.assertEqual((r["name"], r["ips"], r["total_risk"]), ("", "", None))


class TenableTests(Base):
    def tenable(self):
        return hx.Tenable(hx.TenableConfig.from_env())

    def test_single_site_is_used_automatically(self):
        self.assertEqual(self.tenable().resolve_site(None, None), (SITE_A, "Houston"))

    def test_multiple_sites_require_a_choice(self):
        self.world.sites = [("Houston", SITE_A), ("Dallas", SITE_B)]
        t = self.tenable()
        with self.assertRaisesRegex(hx.ExportError, "Multiple sites"):
            t.resolve_site(None, None)
        self.assertEqual(t.resolve_site(None, "dallas"), (SITE_B, "Dallas"))
        self.assertEqual(t.resolve_site(SITE_B, None), (SITE_B, "Dallas"))

    def test_env_default_site_is_used(self):
        self.world.sites = [("Houston", SITE_A), ("Dallas", SITE_B)]
        with mock.patch.dict(os.environ, {"TENABLE_SITE_UUID": SITE_B}):
            self.assertEqual(self.tenable().resolve_site(None, None)[1], "Dallas")

    def test_unknown_or_malformed_site_rejected(self):
        t = self.tenable()
        with self.assertRaisesRegex(hx.ExportError, "not a paired site"):
            t.resolve_site(SITE_B, None)
        with self.assertRaisesRegex(hx.ExportError, "complete UUID"):
            t.resolve_site("abc", None)
        with self.assertRaisesRegex(hx.ExportError, "No site named"):
            t.resolve_site(None, "Nowhere")

    def test_pagination_collects_everything(self):
        self.world.assets = [_asset(i) for i in range(1203)]
        total, nodes = self.tenable().fetch_assets(SITE_A, None, None)
        self.assertEqual((total, len(nodes)), (1203, 1203))
        asset_calls = [r for r in self.world.requests if b"assets(" in r.content]
        self.assertEqual(len(asset_calls), 3)  # 500 + 500 + 203
        self.assertTrue(str(asset_calls[0].url).endswith(f"/{SITE_A}/graphql"))  # site endpoint, not EM root

    def test_limit_is_respected_and_never_overfetches(self):
        self.world.assets = [_asset(i) for i in range(1203)]
        total, nodes = self.tenable().fetch_assets(SITE_A, None, 7)
        self.assertEqual(len(nodes), 7)
        sizes = [json.loads(r.content)["variables"]["pageSize"] for r in self.world.requests if b"assets(" in r.content]
        self.assertEqual(sizes, [7])

    def test_error_paths_are_friendly(self):
        t = self.tenable()
        self.world.tenable_status = 401
        with self.assertRaisesRegex(hx.ExportError, "rejected the API key"):
            t.paired_sites()
        self.world.tenable_status = 200
        self.world.tenable_body = "<html>login</html>"
        with self.assertRaisesRegex(hx.ExportError, "non-JSON"):
            t.paired_sites()
        self.world.tenable_body = json.dumps({"errors": [{"message": "boom"}]})
        with self.assertRaisesRegex(hx.ExportError, "boom"):
            t.paired_sites()

    def test_api_key_header_sent(self):
        self.tenable().paired_sites()
        self.assertEqual(self.world.requests[0].headers["X-APIKeys"], "key=accessKey=a;secretKey=b")


class PowerBITests(Base):
    def pbi(self, **env):
        with mock.patch.dict(os.environ, env):
            return hx.PowerBI(hx.PowerBIConfig.from_env(need_dataset=True))

    def test_create_dataset_request_shape(self):
        self.assertEqual(self.pbi().create_dataset(), "new-dataset-id")
        req = [r for r in self.world.requests if "api.powerbi.com" in str(r.url)][0]
        # retention policy must be a query parameter, never a body field
        self.assertIn("defaultRetentionPolicy=basicFIFO", str(req.url))
        body = json.loads(req.content)
        self.assertNotIn("defaultRetentionPolicy", body)
        self.assertEqual(body["defaultMode"], "Push")
        self.assertEqual([c["name"] for c in body["tables"][0]["columns"]], [c for c, _ in hx.TABLE_COLUMNS])

    def test_my_workspace_omits_groups_segment(self):
        pbi = self.pbi(POWERBI_WORKSPACE_ID="")
        pbi.create_dataset()
        url = str([r for r in self.world.requests if "api.powerbi.com" in str(r.url)][0].url)
        self.assertNotIn("/groups/", url)

    def test_rotated_refresh_token_is_persisted(self):
        self.pbi().clear_table()
        self.assertEqual(json.loads(self.token_file.read_text())["refresh_token"], "refresh-2")

    def test_token_file_is_owner_only(self):
        if os.name == "posix":
            self.assertEqual(self.token_file.stat().st_mode & 0o777, 0o600)

    def test_rows_are_pushed_in_batches_of_10k(self):
        rows = [{"asset_id": str(i)} for i in range(25_000)]
        self.assertEqual(self.pbi().push_rows(rows), 3)
        self.assertEqual(len(self.world.rows_pushed), 25_000)

    def test_not_signed_in_message(self):
        self.token_file.unlink()
        with self.assertRaisesRegex(hx.ExportError, "login"):
            self.pbi().clear_table()


class ExportCommandTests(Base):
    def test_full_export_clears_then_pushes(self):
        code, out, _ = self.run_main("export", "--criticality-at-least", "high")
        self.assertEqual(code, 0, out)
        self.assertIn("3 asset(s) written", out)
        methods = [r.method for r in self.world.requests if "api.powerbi.com" in str(r.url)]
        self.assertEqual(methods, ["DELETE", "POST"])  # clear first, then push
        self.assertEqual(len(self.world.rows_pushed), 3)
        sent = json.loads([r for r in self.world.requests if b"assets(" in r.content][0].content)
        self.assertEqual(sent["variables"]["filter"]["field"], "criticality")

    def test_dry_run_never_touches_power_bi_or_its_settings(self):
        for k in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "POWERBI_DATASET_ID"):
            os.environ.pop(k)
        code, out, _ = self.run_main("export", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("[dry run] 3 row(s)", out)
        self.assertFalse([r for r in self.world.requests if "api.powerbi.com" in str(r.url)])

    def test_bad_scope_fails_before_any_network_call(self):
        code, _, err = self.run_main("export", "--subnet", "bogus")
        self.assertEqual(code, 1)
        self.assertIn("not a valid CIDR", err)
        self.assertEqual(self.world.requests, [])

    def test_missing_settings_listed_together(self):
        os.environ.pop("TENABLE_URL")
        os.environ.pop("TENABLE_API_KEY")
        code, _, err = self.run_main("export", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("TENABLE_URL, TENABLE_API_KEY", err)

    def test_sites_command(self):
        code, out, _ = self.run_main("sites")
        self.assertEqual(code, 0)
        self.assertIn(SITE_A, out)

    def test_create_dataset_prints_env_line(self):
        code, out, _ = self.run_main("create-dataset")
        self.assertEqual((code, "POWERBI_DATASET_ID=new-dataset-id" in out), (0, True))


class LoginTests(Base):
    def test_device_flow_waits_then_saves_token(self):
        calls = {"n": 0}

        def handler(request):
            url = str(request.url)
            if url.endswith("/devicecode"):
                return httpx.Response(200, json={"device_code": "dc", "user_code": "ABC", "interval": 1,
                                                 "expires_in": 60, "message": "Go sign in"})
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(400, json={"error": "authorization_pending"})
            return httpx.Response(200, json={"access_token": "a", "refresh_token": "fresh"})

        self.token_file.unlink()
        with mock.patch.object(hx, "make_client", lambda **k: httpx.Client(transport=httpx.MockTransport(handler))), \
             mock.patch.object(hx.time, "sleep"):
            code, out, _ = self.run_main("login")
        self.assertEqual(code, 0)
        self.assertIn("Go sign in", out)
        self.assertEqual(json.loads(self.token_file.read_text())["refresh_token"], "fresh")


if __name__ == "__main__":
    unittest.main()
