"""Stopping remains possible if Tailscale route cleanup fails."""

from __future__ import annotations

import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from stack import manage

from integrations import tailscale


class StackTests(unittest.TestCase):
    def test_down_stops_stack_when_route_cleanup_fails(self) -> None:
        config = {"NEXTAUTH_URL": "https://node.example.ts.net:15443"}
        with (
            mock.patch.object(manage, "load_config", return_value=config),
            mock.patch.object(manage, "run_compose") as run,
            mock.patch.object(manage, "maintenance_lock", return_value=nullcontext()),
            mock.patch.object(
                tailscale, "disable", side_effect=ValueError("route changed")
            ),
            self.assertRaisesRegex(ValueError, "route changed"),
        ):
            manage.main("down")
        run.assert_called_once_with(config, "down")


if __name__ == "__main__":
    unittest.main()
