"""LM Studio model sync preserves unrelated connections, secrets, and stack state."""

from __future__ import annotations

import contextlib
import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import common

from integrations import lmstudio

PROVIDER = "ai-observatory-lmstudio-123456abcdef"


def connection(**overrides: object) -> dict:
    result = {
        "id": "connection-1",
        "provider": PROVIDER,
        "adapter": "openai",
        "baseURL": lmstudio.GATEWAY,
        "customModels": ["existing/model"],
        "withDefaultModels": False,
        "extraHeaderKeys": [],
        "config": None,
        "displaySecretKey": "...udio",
        "updatedAt": "2026-01-01T00:00:00.000Z",
    }
    result.update(overrides)
    return result


def state_for(value: dict) -> dict:
    return {
        "version": 1,
        "projectId": "test-project",
        "provider": PROVIDER,
        "connectionId": value["id"],
        "fingerprint": lmstudio.connection_fingerprint(value),
        "usesToken": False,
    }


class DiscoveryTests(unittest.TestCase):
    def test_adds_only_exposed_language_models_and_loaded_aliases(self) -> None:
        rich = {
            "models": [
                {
                    "type": "llm",
                    "key": "local/model",
                    "variants": ["local/model@q4"],
                    "loaded_instances": [{"id": "local-alias"}],
                },
                {"type": "embedding", "key": "embedding/model"},
                {"type": "llm", "key": "downloaded/not-loaded"},
            ]
        }
        wire = {
            "data": [
                {"id": x}
                for x in (
                    "local/model",
                    "local/model@q4",
                    "local-alias",
                    "embedding/model",
                )
            ]
        }
        with mock.patch.object(lmstudio, "request_json", side_effect=[rich, wire]):
            actual = lmstudio.available_language_models("synthetic-token")
        self.assertEqual(actual, ["local-alias", "local/model", "local/model@q4"])

    def test_missing_or_unsafe_ids_fail_closed(self) -> None:
        rich = {"models": [{"type": "llm", "key": "good/model"}]}
        for wire in ({"data": []}, {"data": [{"id": "bad\nheader"}]}):
            with (
                self.subTest(wire=wire),
                mock.patch.object(lmstudio, "request_json", side_effect=[rich, wire]),
                self.assertRaises(ValueError),
            ):
                lmstudio.available_language_models("test")

    def test_models_show_type_and_loaded_state_without_paths_or_tokens(self) -> None:
        available = [
            {
                "type": "llm",
                "modelKey": "local/model",
                "sizeBytes": 1000,
                "path": "/private/path",
            },
            {
                "type": "embedding",
                "modelKey": "local/embed",
                "sizeBytes": 100,
                "deviceIdentifier": "remote-private-device",
            },
        ]
        loaded = [{"modelKey": "local/model"}]
        output = io.StringIO()
        with (
            mock.patch.object(lmstudio, "lms_json", side_effect=[available, loaded]),
            contextlib.redirect_stdout(output),
        ):
            lmstudio.models()
        text = output.getvalue()
        self.assertIn("local/model", text)
        self.assertIn("loaded", text)
        self.assertIn("embedding", text)
        self.assertIn("linked device", text)
        self.assertNotIn("/private/path", text)
        self.assertNotIn("remote-private-device", text)

    def test_no_server_or_remote_link_is_not_silently_enabled(self) -> None:
        result = mock.Mock(stdout="n127.0.0.1:1234\n")
        with (
            mock.patch.object(lmstudio.sys, "platform", "darwin"),
            mock.patch.object(lmstudio.subprocess, "run", return_value=result),
            mock.patch.object(
                lmstudio,
                "lms_json",
                side_effect=[{"running": True, "port": 1234}, {"status": "online"}],
            ),
            self.assertRaisesRegex(ValueError, "LM Link must be offline"),
        ):
            lmstudio.verify_server()

    def test_network_wide_lm_studio_listener_is_rejected(self) -> None:
        result = mock.Mock(stdout="n*:1234\n")
        with (
            mock.patch.object(lmstudio.sys, "platform", "darwin"),
            mock.patch.object(lmstudio.subprocess, "run", return_value=result),
            mock.patch.object(
                lmstudio, "lms_json", return_value={"running": True, "port": 1234}
            ),
            self.assertRaisesRegex(ValueError, "listen only"),
        ):
            lmstudio.verify_server()

    def test_authenticated_server_requires_token_without_exposing_it(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.dict(
                os.environ, {"AI_OBSERVATORY_LM_STUDIO_TOKEN": "synthetic-secret"}
            ),
            mock.patch.object(lmstudio, "request_json", return_value={}) as request,
            contextlib.redirect_stdout(output),
        ):
            token, authenticated = lmstudio.model_token()
        self.assertTrue(authenticated)
        self.assertEqual(token, "synthetic-secret")
        self.assertEqual(
            request.call_args.kwargs["authorization"], "Bearer synthetic-secret"
        )
        self.assertNotIn("synthetic-secret", output.getvalue())

    def test_missing_token_never_prompts_without_tty(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(
                lmstudio,
                "request_json",
                side_effect=lmstudio.HttpFailure("LM Studio", 401),
            ),
            mock.patch.object(lmstudio.sys.stdin, "isatty", return_value=False),
            mock.patch.object(lmstudio.getpass, "getpass") as prompt,
            self.assertRaisesRegex(ValueError, "requires a token"),
        ):
            lmstudio.model_token()
        prompt.assert_not_called()

    def test_invalid_bearer_token_is_rejected_without_disclosure(self) -> None:
        for token in ("secret\nheader", "secret\x01tail", "secret\N{SNOWMAN}"):
            with (
                self.subTest(),
                mock.patch.dict(os.environ, {"AI_OBSERVATORY_LM_STUDIO_TOKEN": token}),
                mock.patch.object(lmstudio, "request_json") as request,
                self.assertRaisesRegex(ValueError, "Invalid LM Studio API token"),
            ):
                lmstudio.model_token()
            request.assert_not_called()

    def test_only_local_supported_docker_engines_are_accepted(self) -> None:
        cases = (
            ("unix:///private/docker.sock", "OrbStack", True),
            ("unix:///private/docker.sock", "Docker Desktop", True),
            ("tcp://remote.example.invalid:2376", "Docker Desktop", False),
            ("ssh://remote.example.invalid", "OrbStack", False),
            ("unix:///private/docker.sock", "Other Engine", False),
        )
        for endpoint, engine, accepted in cases:
            with (
                self.subTest(endpoint=endpoint, engine=engine),
                mock.patch.dict(os.environ, {"DOCKER_HOST": endpoint}, clear=True),
                mock.patch.object(
                    lmstudio.subprocess, "run", return_value=mock.Mock(stdout=engine)
                ),
            ):
                if accepted:
                    lmstudio.verify_docker_engine({})
                else:
                    with self.assertRaises(ValueError):
                        lmstudio.verify_docker_engine({})


class StateAndSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        patch = mock.patch.object(common, "ROOT", self.root)
        patch.start()
        self.addCleanup(patch.stop)

    def test_state_is_private_and_updates_atomically(self) -> None:
        original = state_for(connection())
        lmstudio.write_state(original, None)
        path = self.root / lmstudio.STATE_NAME
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        loaded, raw = lmstudio.read_state()
        self.assertEqual(loaded, original)
        changed = {**original, "fingerprint": "a" * 64}
        lmstudio.write_state(changed, raw)
        self.assertEqual(lmstudio.read_state()[0], changed)
        with self.assertRaisesRegex(ValueError, "changed concurrently"):
            lmstudio.write_state(original, raw)
        self.assertEqual(lmstudio.read_state()[0], changed)

    def test_state_refuses_symlinks_and_insecure_permissions(self) -> None:
        path = self.root / lmstudio.STATE_NAME
        target = self.root / "user-file"
        target.write_text("preserve")
        path.symlink_to(target)
        with self.assertRaises(OSError):
            lmstudio.read_state()
        self.assertEqual(target.read_text(), "preserve")
        path.unlink()
        lmstudio.write_state(state_for(connection()), None)
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "not private"):
            lmstudio.read_state()

    def test_existing_connection_sync_only_adds_and_preserves_old_models(self) -> None:
        existing = connection()
        state = state_for(existing)
        lmstudio.write_state(state, None)
        _, raw = lmstudio.read_state()
        changed = connection(
            customModels=["existing/model", "new/model"], updatedAt="later"
        )
        output = io.StringIO()
        with (
            mock.patch.object(lmstudio, "connections", return_value=[existing]),
            mock.patch.object(
                lmstudio, "request_json", return_value=changed
            ) as request,
            contextlib.redirect_stdout(output),
        ):
            lmstudio.sync_connection(
                "http://127.0.0.1:3000",
                "Basic secret",
                state,
                raw,
                "lm-studio",
                False,
                ["new/model"],
            )
        payload = request.call_args.kwargs["body"]
        self.assertEqual(payload["customModels"], ["existing/model", "new/model"])
        self.assertEqual(payload["baseURL"], lmstudio.GATEWAY)
        self.assertFalse(payload["withDefaultModels"])
        self.assertNotIn("Basic secret", output.getvalue())
        self.assertEqual(
            lmstudio.read_state()[0]["fingerprint"],
            lmstudio.connection_fingerprint(changed),
        )

    def test_no_changes_does_not_rewrite_connection(self) -> None:
        existing = connection()
        state = state_for(existing)
        with (
            mock.patch.object(lmstudio, "connections", return_value=[existing]),
            mock.patch.object(lmstudio, "request_json") as request,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            lmstudio.sync_connection(
                "base",
                "auth",
                state,
                b"unchanged",
                "lm-studio",
                False,
                ["existing/model"],
            )
        request.assert_not_called()

    def test_manual_connection_edits_are_never_overwritten(self) -> None:
        original = connection()
        state = state_for(original)
        changed = connection(customModels=["user/model"], updatedAt="new")
        with (
            mock.patch.object(lmstudio, "connections", return_value=[changed]),
            mock.patch.object(lmstudio, "request_json") as request,
            self.assertRaisesRegex(ValueError, "changed outside"),
        ):
            lmstudio.sync_connection(
                "base", "auth", state, b"original", "lm-studio", False, ["new/model"]
            )
        request.assert_not_called()

    def test_unmanaged_connection_and_missing_state_are_never_adopted(self) -> None:
        pending = {
            "version": 1,
            "projectId": "test-project",
            "provider": PROVIDER,
            "connectionId": None,
            "fingerprint": None,
            "usesToken": None,
        }
        with (
            mock.patch.object(lmstudio, "connections", return_value=[connection()]),
            mock.patch.object(lmstudio, "request_json") as request,
            self.assertRaisesRegex(ValueError, "untracked connection"),
        ):
            lmstudio.sync_connection(
                "base", "auth", pending, b"pending", "lm-studio", False, ["new/model"]
            )
        request.assert_not_called()

    def test_extra_headers_or_user_provider_changes_are_not_cleared(self) -> None:
        for updates in (
            {"extraHeaderKeys": ["X-User-Token"]},
            {"baseURL": "https://user.invalid/v1"},
            {"withDefaultModels": True},
            {"config": {"useResponsesApi": True}},
        ):
            with (
                self.subTest(updates=updates),
                self.assertRaisesRegex(ValueError, "not overwritten"),
            ):
                lmstudio.validate_connection(connection(**updates), PROVIDER)

    def test_real_token_is_not_replaced_by_placeholder(self) -> None:
        for display in ("...abcd", "...udio"):
            with self.subTest(display=display):
                existing = connection(displaySecretKey=display)
                state = {**state_for(existing), "usesToken": True}
                with (
                    mock.patch.object(lmstudio, "connections", return_value=[existing]),
                    mock.patch.object(lmstudio, "request_json") as request,
                    self.assertRaisesRegex(
                        ValueError, "provide AI_OBSERVATORY_LM_STUDIO_TOKEN"
                    ),
                ):
                    lmstudio.sync_connection(
                        "base",
                        "auth",
                        state,
                        b"original",
                        "lm-studio",
                        False,
                        ["new/model"],
                    )
                request.assert_not_called()

    def test_enabling_a_real_token_records_credential_mode(self) -> None:
        existing = connection()
        state = state_for(existing)
        lmstudio.write_state(state, None)
        _, raw = lmstudio.read_state()
        updated = connection(displaySecretKey="...udio", updatedAt="later")
        with (
            mock.patch.object(lmstudio, "connections", return_value=[existing]),
            mock.patch.object(lmstudio, "request_json", return_value=updated),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            lmstudio.sync_connection(
                "base",
                "auth",
                state,
                raw,
                "real-token-ending-udio",
                True,
                ["existing/model"],
            )
        self.assertTrue(lmstudio.read_state()[0]["usesToken"])

    def test_token_is_scrubbed_from_compose_environment(self) -> None:
        with mock.patch.dict(
            os.environ, {"AI_OBSERVATORY_LM_STUDIO_TOKEN": "synthetic-secret"}
        ):
            self.assertNotIn(
                "AI_OBSERVATORY_LM_STUDIO_TOKEN", common.compose_environment({})
            )

    def test_opt_in_marker_controls_compose_without_changing_env(self) -> None:
        config = {"COMPOSE_PROJECT_NAME": "aiobs-123456abcdef"}
        self.assertNotIn(
            str(common.LMSTUDIO_COMPOSE), common.compose_command(config, "ps")
        )
        marker = self.root / common.LMSTUDIO_MARKER
        lmstudio.write_new(marker, common.LMSTUDIO_MARKER_CONTENT.decode())
        self.assertIn(
            str(common.LMSTUDIO_COMPOSE), common.compose_command(config, "ps")
        )
        marker.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            common.compose_command(config, "ps")

    def test_symlinked_gateway_source_is_rejected(self) -> None:
        (self.root / "integrations").mkdir()
        (self.root / "integrations" / "lmstudio").symlink_to(
            ROOT / "integrations" / "lmstudio"
        )
        with self.assertRaisesRegex(ValueError, "gateway source"):
            lmstudio.verify_gateway_source()

    def test_connect_explicitly_enables_only_after_preflight(self) -> None:
        config = {"COMPOSE_PROJECT_NAME": "aiobs-123456abcdef", "WEB_PORT": "3000"}
        output = io.StringIO()
        with (
            mock.patch.object(common, "load_config", return_value=config),
            mock.patch.object(lmstudio, "verify_server"),
            mock.patch.object(lmstudio, "verify_docker_engine"),
            mock.patch.object(
                lmstudio, "model_token", return_value=("synthetic-token", True)
            ),
            mock.patch.object(
                lmstudio, "available_language_models", return_value=["local/model"]
            ),
            mock.patch.object(
                lmstudio,
                "project_credentials",
                return_value=("http://127.0.0.1:3000", "Basic synthetic-key"),
            ),
            mock.patch.object(lmstudio, "project_id", return_value="test-project"),
            mock.patch.object(lmstudio, "connections", return_value=[]),
            mock.patch.object(lmstudio, "verify_stack_environment"),
            mock.patch.object(lmstudio, "verify_gateway_source"),
            mock.patch.object(lmstudio, "reject_unmanaged_gateway"),
            mock.patch.object(common, "run_compose") as compose,
            mock.patch.object(lmstudio, "verify_gateway") as gateway,
            mock.patch.object(lmstudio, "sync_connection") as sync,
            contextlib.redirect_stdout(output),
        ):
            lmstudio.integrate("connect")
        self.assertTrue(common.lmstudio_enabled())
        self.assertTrue((self.root / lmstudio.STATE_NAME).is_file())
        self.assertEqual(
            compose.call_args.args[-3:], ("lmstudio-gateway", "web", "worker")
        )
        gateway.assert_called_once_with(config, "synthetic-token")
        self.assertEqual(sync.call_args.args[-1], ["local/model"])
        self.assertNotIn("synthetic-token", output.getvalue())
        self.assertNotIn("synthetic-key", output.getvalue())

    def test_failed_preflight_does_not_create_state_or_restart(self) -> None:
        with (
            mock.patch.object(common, "load_config", return_value={}),
            mock.patch.object(
                lmstudio, "verify_server", side_effect=ValueError("server unavailable")
            ),
            mock.patch.object(common, "run_compose") as compose,
            self.assertRaisesRegex(ValueError, "server unavailable"),
        ):
            lmstudio.integrate("connect")
        compose.assert_not_called()
        self.assertFalse((self.root / lmstudio.STATE_NAME).exists())
        self.assertFalse((self.root / common.LMSTUDIO_MARKER).exists())


if __name__ == "__main__":
    unittest.main()
