"""Local judge is opt-in, bounded, private and treats output as untrusted."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from integrations import agent_evaluate as judge


class Response:
    def __init__(self, value: object) -> None:
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def read(self, _: int) -> bytes:
        return json.dumps(self.value).encode()


class JudgeTests(unittest.TestCase):
    def test_private_bounded_evidence_file_and_symlink_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.json"
            path.write_text(json.dumps(judge.SYNTHETIC[0]))
            path.chmod(0o600)
            self.assertEqual(
                judge.read_evidence(path)["case_id"], "synthetic-completed"
            )
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "private regular file"):
                judge.read_evidence(path)
            link = Path(tmp) / "link"
            os.symlink(path, link)
            with self.assertRaisesRegex(ValueError, "safely read"):
                judge.read_evidence(link)
            self.assertTrue(path.exists())

    def test_case_and_model_output_schema_are_strict(self) -> None:
        case = copy.deepcopy(judge.SYNTHETIC[0])
        case["task"] = "x" * 4001
        with self.assertRaisesRegex(ValueError, "4000"):
            judge.validate_case(case)
        case = copy.deepcopy(judge.SYNTHETIC[0])
        case["case_id"] = "secret/path"
        with self.assertRaisesRegex(ValueError, "case_id"):
            judge.validate_case(case)
        invalid = {**judge.EXPECTED[0], "extra": "SYNTHETIC_SECRET"}
        with self.assertRaisesRegex(ValueError, "invalid category schema"):
            judge.validate_result(invalid)
        invalid = {**judge.EXPECTED[0], "completion_evidence": "certain_success"}
        with self.assertRaisesRegex(ValueError, "unrecognized category"):
            judge.validate_result(invalid)

    def test_selected_evidence_requires_explicit_sensitive_flag_before_read(
        self,
    ) -> None:
        with (
            mock.patch.object(judge, "read_evidence") as read,
            self.assertRaisesRegex(ValueError, "requires --allow-sensitive"),
        ):
            judge.main(["--evidence", "/synthetic/not-read"])
        read.assert_not_called()

    def test_only_local_downloaded_models_are_allowed(self) -> None:
        linked = [
            {
                "type": "llm",
                "modelKey": "test-model",
                "deviceIdentifier": "other-device",
            }
        ]
        with (
            mock.patch.object(judge.lmstudio, "verify_server"),
            mock.patch.object(judge.lmstudio, "lms_json", return_value=linked),
            mock.patch.object(judge.lmstudio, "model_token") as token,
            self.assertRaisesRegex(ValueError, "locally downloaded"),
        ):
            judge.local_model("test-model")
        token.assert_not_called()

    def test_wire_request_is_fixed_loopback_and_returns_only_schema(self) -> None:
        response = {
            "choices": [{"message": {"content": json.dumps(judge.EXPECTED[0])}}]
        }
        opener = mock.Mock()
        opener.open.return_value = Response(response)
        case = {**judge.SYNTHETIC[0], "result": "SYNTHETIC_SECRET"}
        with mock.patch.object(
            judge.urllib.request, "build_opener", return_value=opener
        ):
            result = judge.evaluate(case, "local-model", "synthetic-token")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:1234/v1/chat/completions")
        self.assertEqual(
            json.loads(request.data)["response_format"]["type"], "json_schema"
        )
        self.assertEqual(result, judge.EXPECTED[0])
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))

    def test_run_outputs_no_input_and_checks_routing_per_case(self) -> None:
        cases = [{**judge.SYNTHETIC[0], "result": "SYNTHETIC_SECRET"}]
        with (
            mock.patch.object(judge, "local_model", return_value="synthetic-token"),
            mock.patch.object(judge.lmstudio, "verify_server") as verify,
            mock.patch.object(judge, "evaluate", return_value=judge.EXPECTED[0]),
        ):
            output = judge.run(cases, "test-model", "selected_evidence")
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(output["evidence_class"], "local_model_judgment")
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(output))
        self.assertNotIn("synthetic-token", json.dumps(output))

    def test_demo_result_includes_only_illustrative_comparison(self) -> None:
        with (
            mock.patch.object(judge, "local_model", return_value="synthetic-token"),
            mock.patch.object(judge.lmstudio, "verify_server"),
            mock.patch.object(judge, "evaluate", side_effect=judge.EXPECTED),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            judge.main(["--demo"])
        result = json.loads(output.getvalue())
        self.assertEqual(result["synthetic_comparison"]["matched_fields"], 15)
        self.assertEqual(result["synthetic_comparison"]["total_fields"], 15)
        self.assertNotIn("synthetic-token", output.getvalue())


if __name__ == "__main__":
    unittest.main()
