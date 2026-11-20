"""Installation and evaluation-rule preflight never overwrite unrelated resources."""

from __future__ import annotations

import contextlib
import copy
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from integrations import opencode_events as events


class RulesClient:
    def __init__(self, pages: list[dict]) -> None:
        self.pages = copy.deepcopy(pages)
        self.calls = 0

    def request(self, method: str, path: str) -> dict:
        assert method == "GET" and path.startswith("/api/public/v2/evaluation-rules?")
        result = self.pages[self.calls]
        self.calls += 1
        return result


class EventSetupTests(unittest.TestCase):
    def test_rule_preflight_rejects_uncertain_or_matching_rules(self) -> None:
        demo_filter = [
            {
                "column": "type",
                "operator": "any of",
                "type": "stringOptions",
                "value": ["SPAN"],
            }
        ]
        events.verify_rules(
            RulesClient(
                [{"data": [{"enabled": True, "filter": demo_filter}], "meta": {}}]
            )
        )
        events.verify_rules(RulesClient([{"data": [{"enabled": False}], "meta": {}}]))
        for value in (
            [],
            [
                {
                    "column": "type",
                    "operator": "any of",
                    "type": "stringOptions",
                    "value": [],
                }
            ],
            [
                {
                    "column": "type",
                    "operator": "any of",
                    "type": "stringOptions",
                    "value": ["UNRECOGNIZED"],
                }
            ],
            [{"column": "type", "operator": "=", "type": "string", "value": "SPAN"}],
            [
                {
                    "column": "type",
                    "operator": "any of",
                    "type": "stringOptions",
                    "value": ["SPAN", "EVENT"],
                }
            ],
            [
                {
                    "column": "environment",
                    "operator": "any of",
                    "type": "stringOptions",
                    "value": ["other"],
                }
            ],
        ):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "could process"),
            ):
                events.verify_rules(
                    RulesClient(
                        [{"data": [{"enabled": True, "filter": value}], "meta": {}}]
                    )
                )

    def test_rule_pagination_must_terminate(self) -> None:
        client = RulesClient(
            [
                {"data": [], "meta": {"cursor": "same"}},
                {"data": [], "meta": {"cursor": "same"}},
            ]
        )
        with self.assertRaisesRegex(ValueError, "invalid evaluation-rule pagination"):
            events.verify_rules(client)

    def test_install_is_repeatable_and_preserves_conflicting_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            events.install(home)
            entry = home / "opencode" / "plugins" / "ai-observatory-events.js"
            before = entry.read_text()
            events.install(home)
            self.assertEqual(entry.read_text(), before)
            targets = list((home / "ai-observatory").glob(events.PREFIX + "*"))
            self.assertEqual(len(targets), 1)
            self.assertTrue(events.safe_installed(targets[0]))
            entry.write_text("user plugin data")
            with self.assertRaisesRegex(ValueError, "left unchanged"):
                events.install(home)
            self.assertEqual(entry.read_text(), "user plugin data")

    def test_modified_managed_files_and_entry_symlink_are_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            events.install(home)
            target = next((home / "ai-observatory").glob(events.PREFIX + "*"))
            (target / "index.js").write_text("user modification")
            with self.assertRaisesRegex(ValueError, "left unchanged"):
                events.install(home)
            self.assertEqual((target / "index.js").read_text(), "user modification")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            plugins = home / "opencode" / "plugins"
            plugins.mkdir(parents=True)
            target = home / "other"
            target.write_text("user data")
            os.symlink(target, plugins / "ai-observatory-events.js")
            with self.assertRaisesRegex(ValueError, "left unchanged"):
                events.install(home)
            self.assertEqual(target.read_text(), "user data")

    def test_disconnect_removes_only_an_owned_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            events.install(home)
            entry = home / "opencode" / "plugins" / "ai-observatory-events.js"
            target = next((home / "ai-observatory").glob(events.PREFIX + "*"))
            other = home / "opencode" / "plugins" / "user.js"
            other.write_text("preserve")
            with contextlib.redirect_stdout(io.StringIO()):
                events.disconnect(home)
                events.disconnect(home)
            self.assertFalse(entry.exists())
            self.assertTrue(target.is_dir())
            self.assertEqual(other.read_text(), "preserve")

    def test_disconnect_refuses_modified_entry_or_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            events.install(home)
            entry = home / "opencode" / "plugins" / "ai-observatory-events.js"
            target = next((home / "ai-observatory").glob(events.PREFIX + "*"))
            (target / "index.js").write_text("user modification")
            with self.assertRaisesRegex(ValueError, "left unchanged"):
                events.disconnect(home)
            self.assertTrue(entry.exists())

    def test_repository_upgrade_does_not_prevent_safe_disconnect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            events.install(home)
            entry = home / "opencode" / "plugins" / "ai-observatory-events.js"
            target = next((home / "ai-observatory").glob(events.PREFIX + "*"))
            upgraded = Path(tmp) / "upgraded-source"
            upgraded.mkdir()
            for name in events.FILES:
                (upgraded / name).write_text("different version")
            with mock.patch.object(events, "SOURCE", upgraded):
                self.assertTrue(events.safe_installed(target))
                with self.assertRaisesRegex(ValueError, "left unchanged"):
                    events.install(home)
                with contextlib.redirect_stdout(io.StringIO()):
                    events.disconnect(home)
            self.assertFalse(entry.exists())
            self.assertTrue(target.is_dir())

    def test_disconnect_rejects_invalid_or_modified_install_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            events.install(home)
            entry = home / "opencode" / "plugins" / "ai-observatory-events.js"
            target = next((home / "ai-observatory").glob(events.PREFIX + "*"))
            (target / events.MARKER).write_text('{"version":1,"files":{}}')
            with self.assertRaisesRegex(ValueError, "left unchanged"):
                events.disconnect(home)
            self.assertTrue(entry.exists())


if __name__ == "__main__":
    unittest.main()
