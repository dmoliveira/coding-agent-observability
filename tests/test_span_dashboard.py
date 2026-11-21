"""Dashboard provisioning is scoped, repeatable, and non-destructive."""

from __future__ import annotations

import contextlib
import copy
import io
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import common

from integrations import span_dashboard as dashboard
from integrations import span_dashboard_definition as spec


class FakeClient:
    base = "http://127.0.0.1:3000"
    auth = "Basic synthetic-secret"

    def __init__(self) -> None:
        self.resources: dict[str, dict] = {}
        self.writes: list[tuple[str, str, dict]] = []
        self.counter = 0
        self.fail_write = False

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        if path.startswith("/api/public/v2/evaluation-rules?"):
            return {
                "data": [
                    copy.deepcopy(v)
                    for k, v in self.resources.items()
                    if k.startswith(dashboard.PATHS["rule"] + "/")
                ],
                "meta": {},
            }
        if method == "GET":
            return copy.deepcopy(self.resources[path])
        self.writes.append((method, path, copy.deepcopy(body)))
        if self.fail_write:
            raise ValueError("synthetic network failure")
        if path == "/api/public/otel/v1/traces":
            return {}
        if method == "POST":
            self.counter += 1
            identifier = f"resource-{self.counter}"
            path = f"{path}/{identifier}"
            value = {"id": identifier, **copy.deepcopy(body)}
            if value.get("type") == "llm_as_judge":
                value.update({"status": "active", "evaluationRuleAssignments": []})
            self.resources[path] = value
        elif method == "PATCH":
            self.resources[path].update(copy.deepcopy(body))
        return copy.deepcopy(self.resources[path])


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.client = FakeClient()
        for patch in (
            mock.patch.object(common, "ROOT", self.root),
            mock.patch.object(
                dashboard, "context", return_value=(self.client, {}, "project-1")
            ),
            mock.patch.object(dashboard, "connection", return_value="local-provider"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def apply(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            dashboard.apply()

    def test_apply_is_repeatable_and_demo_rule_is_narrow(self) -> None:
        self.apply()
        first = len(self.client.writes)
        self.apply()
        self.assertEqual(len(self.client.writes), first)
        state, _ = dashboard.read_state()
        self.assertEqual(len(state["resources"]), 7)
        rule = self.client.resources[
            dashboard.PATHS["rule"] + "/" + state["resources"]["rule"]["id"]
        ]
        self.assertTrue(rule["enabled"])
        self.assertEqual(rule["sampling"], 1)
        filters = {f["column"]: f["value"] for f in rule["filter"]}
        self.assertEqual(filters["environment"], [spec.ENVIRONMENT])
        self.assertEqual(filters["type"], ["SPAN"])
        self.assertEqual(
            filters["name"],
            [spec.definitions("local-provider", state["nonce"])["span_name"]],
        )
        evaluator = self.client.resources[
            dashboard.PATHS["evaluator"] + "/" + state["resources"]["evaluator"]["id"]
        ]
        self.assertEqual(
            evaluator["modelConfig"],
            {"provider": "local-provider", "model": spec.MODEL},
        )
        self.assertEqual(
            evaluator["outputDefinition"]["categories"],
            ["inspect", "change", "test", "other"],
        )
        self.assertEqual(
            (self.root / dashboard.STATE_NAME).stat().st_mode & 0o777, 0o600
        )

    def test_manual_edits_abort_before_any_updates(self) -> None:
        self.apply()
        state, _ = dashboard.read_state()
        widget_path = (
            dashboard.PATHS["widget"] + "/" + state["resources"]["widget:spans"]["id"]
        )
        self.client.resources[widget_path]["name"] = "User edited widget"
        writes = len(self.client.writes)
        with self.assertRaisesRegex(ValueError, "changed outside"):
            self.apply()
        self.assertEqual(len(self.client.writes), writes)
        self.assertEqual(
            self.client.resources[widget_path]["name"], "User edited widget"
        )

    def test_code_change_updates_owned_widget_without_duplication(self) -> None:
        self.apply()
        original = spec.definitions

        def changed(provider: str, nonce: str) -> dict:
            data = original(provider, nonce)
            data["widgets"]["spans"]["description"] = "Improved description"
            return data

        before = len(self.client.writes)
        with mock.patch.object(spec, "definitions", side_effect=changed):
            self.apply()
            self.apply()
        self.assertEqual(len(self.client.writes), before + 1)
        self.assertEqual(self.client.writes[-1][0], "PATCH")

    def test_uncertain_create_is_not_retried_or_adopted(self) -> None:
        self.client.fail_write = True
        with self.assertRaisesRegex(ValueError, "synthetic network failure"):
            self.apply()
        state, _ = dashboard.read_state()
        self.assertEqual(state["pending"], "evaluator")
        self.client.fail_write = False
        with self.assertRaisesRegex(ValueError, "earlier dashboard operation"):
            self.apply()
        self.assertEqual(len(self.client.writes), 1)

    def test_different_project_and_unsafe_state_are_rejected(self) -> None:
        self.apply()
        with (
            mock.patch.object(
                dashboard, "context", return_value=(self.client, {}, "other-project")
            ),
            self.assertRaisesRegex(ValueError, "different project"),
        ):
            self.apply()
        (self.root / dashboard.STATE_NAME).chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private file"):
            dashboard.read_state()

    def test_demo_sends_only_three_named_synthetic_spans(self) -> None:
        self.apply()
        state, _ = dashboard.read_state()
        with contextlib.redirect_stdout(io.StringIO()):
            dashboard.demo()
        method, path, payload = self.client.writes[-1]
        self.assertEqual((method, path), ("POST", "/api/public/otel/v1/traces"))
        resource = payload["resourceSpans"][0]
        spans = resource["scopeSpans"][0]["spans"]
        self.assertEqual(len(spans), 3)
        self.assertEqual(
            {s["name"] for s in spans},
            {spec.definitions("local-provider", state["nonce"])["span_name"]},
        )
        self.assertEqual(
            [s["attributes"][0]["value"]["stringValue"] for s in spans],
            list(spec.EXAMPLES),
        )
        self.assertIn(spec.ENVIRONMENT, str(resource["resource"]))
        self.assertEqual(len({s["traceId"] for s in spans}), 1)

    def test_demo_will_not_trigger_an_unrelated_enabled_rule(self) -> None:
        self.apply()
        self.client.resources[dashboard.PATHS["rule"] + "/user-rule"] = {
            "id": "user-rule",
            "enabled": True,
        }
        before = len(self.client.writes)
        with self.assertRaisesRegex(ValueError, "Another enabled evaluation rule"):
            dashboard.demo()
        self.assertEqual(len(self.client.writes), before)

    def test_demo_rejects_an_unrecognizable_rule(self) -> None:
        self.apply()
        self.client.resources[dashboard.PATHS["rule"] + "/unknown-rule"] = {
            "id": "unknown-rule"
        }
        before = len(self.client.writes)
        with self.assertRaisesRegex(ValueError, "Another enabled evaluation rule"):
            dashboard.demo()
        self.assertEqual(len(self.client.writes), before)

    def test_demo_requires_new_definitions_to_be_applied_first(self) -> None:
        self.apply()
        original = spec.definitions

        def changed(provider: str, nonce: str) -> dict:
            data = original(provider, nonce)
            data["evaluator"]["description"] = "Changed definition"
            return data

        before = len(self.client.writes)
        with (
            mock.patch.object(spec, "definitions", side_effect=changed),
            self.assertRaisesRegex(ValueError, "run make span-dashboard"),
        ):
            dashboard.demo()
        self.assertEqual(len(self.client.writes), before)

    def test_token_not_in_fake_client_output(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            dashboard.apply()
        self.assertNotIn("synthetic-secret", output.getvalue())

    def test_symlink_state_is_not_followed(self) -> None:
        target = self.root / "preserve"
        target.write_text("user data")
        os.symlink(target, self.root / dashboard.STATE_NAME)
        with self.assertRaises(OSError):
            dashboard.read_state()
        self.assertEqual(target.read_text(), "user data")

    def test_client_caps_response_bytes_and_tracks_successful_reads(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                pass

            def read(self, maximum: int) -> bytes:
                return b"x" * maximum

        client = dashboard.Client("http://127.0.0.1:3000", "synthetic-auth")
        client.opener = mock.Mock()
        client.opener.open.return_value = Response()
        with self.assertRaisesRegex(ValueError, "response-byte budget"):
            client.request("GET", "/synthetic", timeout=0.5)
        client.opener.open.assert_called_once()
        self.assertEqual(client.opener.open.call_args.kwargs["timeout"], 0.5)
        self.assertEqual(client.response_bytes_received, dashboard.MAX_RESPONSE + 1)

        client.opener.open.side_effect = urllib.error.URLError(TimeoutError())
        with self.assertRaisesRegex(TimeoutError, "request-time budget"):
            client.request("GET", "/synthetic", timeout=0.5)


if __name__ == "__main__":
    unittest.main()
