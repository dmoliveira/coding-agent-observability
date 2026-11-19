"""Tailscale changes must preserve unrelated Serve and Funnel configuration."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from integrations import tailscale


class TailscaleConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host = "node.example.ts.net"
        self.local = {"WEB_PORT": "3000", "MEDIA_PORT": "9090"}
        self.existing = {
            "TCP": {"443": {"HTTPS": True}},
            "Web": {
                "node.example.ts.net:443": {"Handlers": {"/blog": {"Text": "existing"}}}
            },
            "AllowFunnel": {"node.example.ts.net:443": True},
        }

    def test_add_and_remove_preserve_unrelated_routes(self) -> None:
        original = copy.deepcopy(self.existing)
        added = tailscale.add_routes(self.existing, self.host, self.local)
        self.assertEqual(self.existing, original)
        self.assertEqual(added["AllowFunnel"], original["AllowFunnel"])
        removed = tailscale.remove_routes(added, self.host, self.local)
        self.assertEqual(removed, original)

    def test_occupied_foreground_or_funnel_port_fails_closed(self) -> None:
        for occupied in (
            {"TCP": {"15443": {"TCPForward": "127.0.0.1:1"}}},
            {"AllowFunnel": {"node.example.ts.net:15444": True}},
            {"Foreground": {"other-session": {"TCP": {"15443": {"HTTPS": True}}}}},
        ):
            original = copy.deepcopy(occupied)
            with (
                self.subTest(occupied=occupied),
                self.assertRaisesRegex(ValueError, "already in use"),
            ):
                tailscale.add_routes(occupied, self.host, self.local)
            self.assertEqual(occupied, original)

    def test_removal_refuses_modified_or_shared_port(self) -> None:
        configured = tailscale.add_routes({}, self.host, self.local)
        for mutation in ("funnel", "other_host", "extra_handler", "target"):
            changed = copy.deepcopy(configured)
            if mutation == "funnel":
                changed["AllowFunnel"] = {f"{self.host}:15443": True}
            elif mutation == "other_host":
                changed["Web"]["other.ts.net:15443"] = {
                    "Handlers": {"/": {"Text": "keep"}}
                }
            elif mutation == "extra_handler":
                changed["Web"][f"{self.host}:15443"]["Handlers"]["/other"] = {
                    "Text": "keep"
                }
            else:
                changed["Web"][f"{self.host}:15443"]["Handlers"]["/"]["Proxy"] = (
                    "http://127.0.0.1:1"
                )
            original = copy.deepcopy(changed)
            with (
                self.subTest(mutation=mutation),
                self.assertRaisesRegex(ValueError, "no route was removed"),
            ):
                tailscale.remove_routes(changed, self.host, self.local)
            self.assertEqual(changed, original)

    def test_disable_recovers_when_both_routes_are_absent(self) -> None:
        remote = {
            **self.local,
            "NEXTAUTH_URL": f"https://{self.host}:15443",
            "MEDIA_URL": f"https://{self.host}:15444",
        }
        api = mock.Mock()
        api.request.return_value = (self.existing, '"version"')
        with (
            mock.patch.object(tailscale, "load_config", return_value=remote),
            mock.patch.object(tailscale, "tailscale_hostname", return_value=self.host),
            mock.patch.object(tailscale, "LocalAPI", return_value=api),
            mock.patch.object(tailscale, "replace_config") as replace,
        ):
            tailscale.disable(restart=False)
        api.request.assert_called_once_with("GET")
        self.assertEqual(
            replace.call_args.args[1]["NEXTAUTH_URL"], "http://127.0.0.1:3000"
        )

    def test_update_without_etag_is_refused(self) -> None:
        api = object.__new__(tailscale.LocalAPI)
        with self.assertRaisesRegex(ValueError, "version is required"):
            api.request("POST", {})

    def test_conditional_update_uses_snapshot_etag_and_rejects_conflict(self) -> None:
        api = object.__new__(tailscale.LocalAPI)
        api.socket_path, api.port, api.authorization = None, 1234, None
        response = mock.Mock(status=412)
        response.read.return_value = b""
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with (
            mock.patch.object(
                tailscale.http.client, "HTTPConnection", return_value=connection
            ),
            self.assertRaisesRegex(ValueError, "changed concurrently"),
        ):
            api.request("POST", {"TCP": {}}, '"snapshot"')
        self.assertEqual(
            connection.request.call_args.kwargs["headers"]["If-Match"], '"snapshot"'
        )
        connection.close.assert_called_once()

    def test_disable_refuses_partial_or_foreign_routes(self) -> None:
        remote = {
            **self.local,
            "NEXTAUTH_URL": f"https://{self.host}:15443",
            "MEDIA_URL": f"https://{self.host}:15444",
        }
        api = mock.Mock()
        api.request.return_value = ({"TCP": {"15443": {"HTTPS": True}}}, '"version"')
        with (
            mock.patch.object(tailscale, "load_config", return_value=remote),
            mock.patch.object(tailscale, "tailscale_hostname", return_value=self.host),
            mock.patch.object(tailscale, "LocalAPI", return_value=api),
            mock.patch.object(tailscale, "replace_config") as replace,
            self.assertRaisesRegex(ValueError, "no route was removed"),
        ):
            tailscale.disable(restart=False)
        api.request.assert_called_once_with("GET")
        replace.assert_not_called()

    def test_confirmed_conflict_restores_local_mode(self) -> None:
        local = {
            **self.local,
            "NEXTAUTH_URL": "http://127.0.0.1:3000",
            "MEDIA_URL": "http://127.0.0.1:9090",
        }
        api = mock.Mock()
        api.request.side_effect = [
            ({}, '"version"'),
            tailscale.ConcurrentUpdate("conflict"),
        ]
        with (
            mock.patch.object(tailscale, "load_config", return_value=local),
            mock.patch.object(tailscale, "require_running"),
            mock.patch.object(tailscale, "tailscale_hostname", return_value=self.host),
            mock.patch.object(tailscale, "LocalAPI", return_value=api),
            mock.patch.object(tailscale, "replace_config") as replace,
            mock.patch.object(tailscale, "run_compose") as compose,
            self.assertRaises(tailscale.ConcurrentUpdate),
        ):
            tailscale.enable()
        self.assertEqual(replace.call_count, 2)
        self.assertEqual(replace.call_args.args[1], local)
        self.assertEqual(compose.call_count, 2)
        self.assertEqual(compose.call_args.args[0], local)

    def test_unknown_update_outcome_is_not_blindly_reverted(self) -> None:
        local = {
            **self.local,
            "NEXTAUTH_URL": "http://127.0.0.1:3000",
            "MEDIA_URL": "http://127.0.0.1:9090",
        }
        api = mock.Mock()
        api.request.side_effect = [({}, '"version"'), ValueError("transport failure")]
        with (
            mock.patch.object(tailscale, "load_config", return_value=local),
            mock.patch.object(tailscale, "require_running"),
            mock.patch.object(tailscale, "tailscale_hostname", return_value=self.host),
            mock.patch.object(tailscale, "LocalAPI", return_value=api),
            mock.patch.object(tailscale, "replace_config") as replace,
            mock.patch.object(tailscale, "run_compose"),
            self.assertRaisesRegex(ValueError, "transport failure"),
        ):
            tailscale.enable()
        self.assertEqual(replace.call_count, 1)


if __name__ == "__main__":
    unittest.main()
