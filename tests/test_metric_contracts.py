"""Contract-registry invariants for harness metrics and identities."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "docs/specs/harness-metric-contracts.json"


class MetricContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
        cls.families = cls.registry["families"]

    def test_registry_has_all_stable_families_and_owned_components(self) -> None:
        families = {family["id"]: family for family in self.families}
        self.assertEqual(len(families), 82)
        self.assertEqual(self.registry["metric_family_count"], 82)
        self.assertEqual(len(self.registry["families"]), 82)
        self.assertEqual(self.registry["metric_component_count"], 262)
        self.assertTrue(
            all(re.fullmatch(r"[CTFLQAMRX][0-9]+", key) for key in families)
        )

        components = [
            component for family in self.families for component in family["components"]
        ]
        component_ids = [component["id"] for component in components]
        self.assertEqual(len(component_ids), len(set(component_ids)))
        self.assertEqual(len(components), self.registry["metric_component_count"])
        self.assertGreater(len(components), len(self.families))
        for family in self.families:
            self.assertTrue(family["owner_task_id"].startswith("task_"))
            self.assertTrue(family["definition"])
            self.assertTrue(family["chart"])
            self.assertTrue(family["readiness"])
            self.assertNotIn("delivery_state", family)
            self.assertEqual(
                sum(family["delivery_state_summary"].values()),
                len(family["components"]),
            )
            self.assertGreaterEqual(len(family["components"]), 1)
            for component in family["components"]:
                self.assertTrue(component["name"])
                self.assertTrue(component["calculation_rule"])
                self.assertTrue(component["formula"])
                self.assertTrue(component["eligibility"])
                self.assertTrue(component["exclusions"])
                self.assertTrue(component["timestamp_basis"])
                self.assertIn(
                    component["timestamp_basis_status"],
                    {"observed", "requires_source_probe"},
                )
                self.assertEqual(component["id"].split(".", 1)[0], family["id"])
                self.assertNotIn("/", component["unit"])
                self.assertIn(
                    component["delivery_state"], self.registry["delivery_states"]
                )
                if component["delivery_state"] == "implemented":
                    self.assertTrue(component["delivery_evidence"])
                    self.assertEqual(component["contract_conformance"], "legacy")
                    for reference in component["delivery_evidence"]:
                        path, separator, symbol = reference.partition("::")
                        evidence_path = ROOT / path
                        self.assertTrue(evidence_path.is_file(), reference)
                        if separator and symbol:
                            evidence = evidence_path.read_text(encoding="utf-8")
                            self.assertTrue(
                                symbol in evidence
                                or symbol.rsplit(".", 1)[-1] in evidence,
                                reference,
                            )
                if component["delivery_state"] == "explicitly-unavailable-with-reason":
                    self.assertTrue(component["delivery_reason"])
                if component["unit"] == "fraction":
                    self.assertTrue(component["numerator"])
                    self.assertTrue(component["denominator"])
                    self.assertIn("/", component["formula"])
                if component["aggregation"] == "count":
                    self.assertIn(component["id"], component["formula"])

    def test_metric_instances_require_formula_population_provenance_and_time_basis(
        self,
    ) -> None:
        required = set(self.registry["metric_fields_required"])
        self.assertTrue(
            {
                "schema_version",
                "metric_id",
                "definition_version",
                "formula",
                "population",
                "eligibility",
                "exclusions",
                "value",
                "unit",
                "aggregation",
                "numerator",
                "denominator",
                "evidence_class",
                "source_version",
                "producer_version",
                "transform_version",
                "transform_version_reason",
                "readiness",
                "delivery_state",
                "coverage",
                "sample_size",
                "counter_epoch",
                "counter_epoch_reason",
                "source_clock_uncertainty_ms",
                "watermark",
                "late_data_policy",
                "uncertainty",
                "timestamp_basis",
                "window_from",
                "window_to",
                "timezone",
                "computed_at",
                "dimensions",
                "interpretation",
                "drilldown",
                "availability_reason",
                "provenance_notes",
            }
            <= required
        )
        self.assertIn("unavailable", self.registry["missing_states"])
        self.assertIn("censored", self.registry["missing_states"])
        self.assertIn(
            "explicitly-unavailable-with-reason", self.registry["delivery_states"]
        )

    def test_json_schema_is_closed_versioned_and_requires_reasons_for_unknowns(
        self,
    ) -> None:
        schema = self.registry["metric_instance_schema"]
        self.assertEqual(
            schema["$schema"], "https://json-schema.org/draft/2020-12/schema"
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["required"]), set(self.registry["metric_fields_required"])
        )
        self.assertEqual(
            schema["properties"]["schema_version"]["const"],
            self.registry["schema_version"],
        )
        self.assertEqual(
            schema["properties"]["delivery_state"]["enum"],
            self.registry["delivery_states"],
        )
        self.assertIn("null", schema["properties"]["value"]["type"])
        self.assertTrue(
            any(
                "availability_reason" in item.get("then", {}).get("properties", {})
                for item in schema["allOf"]
            )
        )
        self.assertTrue(
            any(
                "transform_version_reason" in item.get("then", {}).get("properties", {})
                for item in schema["allOf"]
            )
        )
        self.assertTrue(
            any(
                item.get("if", {})
                .get("properties", {})
                .get("aggregation", {})
                .get("enum")
                == ["fraction", "ratio"]
                and item.get("then", {})
                .get("properties", {})
                .get("numerator", {})
                .get("type")
                == "number"
                for item in schema["allOf"]
            )
        )
        self.assertFalse(schema["properties"]["dimensions"]["additionalProperties"])
        self.assertFalse(schema["properties"]["coverage"]["additionalProperties"])
        self.assertIn("reasons", schema["properties"]["coverage"]["required"])
        self.assertIn("sampling_notes", schema["properties"]["population"]["required"])
        self.assertIn(
            "late_data_policy", schema["properties"]["population"]["required"]
        )
        self.assertIn(
            "null", schema["properties"]["coverage"]["properties"]["eligible"]["type"]
        )
        metric_id_pattern = schema["properties"]["metric_id"]["pattern"]
        self.assertRegex("L4.input_tokens", metric_id_pattern)
        self.assertNotRegex("L4/input_tokens", metric_id_pattern)
        self.assertNotIn("input", schema["properties"])
        self.assertNotIn("output", schema["properties"])

    def test_identity_and_outcome_contracts_prohibit_inferred_successful_joins(
        self,
    ) -> None:
        identities = self.registry["identity_types"]
        self.assertTrue(
            {
                "request_attempt",
                "logical_operation",
                "nested_envelope",
                "session_turn_busy_period",
                "task_artifact_revision_check",
                "agent_delegation",
                "producer_epoch",
            }
            <= identities.keys()
        )
        self.assertIn("timestamp", identities["logical_operation"])
        self.assertIn("unknown", self.registry["execution_states"])
        self.assertIn("expected-negative", self.registry["terminal_results"])
        self.assertIn("unknown", self.registry["terminal_results"])
        self.assertTrue(
            any("No title" in rule for rule in self.registry["privacy_rules"])
        )
        self.assertTrue(
            any("prompts" in rule for rule in self.registry["privacy_rules"])
        )

    def test_documentation_explains_atomic_components_and_delivery_ledger(self) -> None:
        document = (ROOT / "docs/specs/harness-metric-contracts.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("single-unit components", document)
        self.assertIn("missing is never zero", document)
        self.assertIn("Delivery status", document)
        self.assertIn("busy-period", document)
        self.assertIn("watermark", document)
        self.assertIn("Do not create a universal AI/harness health score", document)
        self.assertIn("Current source limitations", document)
        unsupported = {item["id"] for item in self.registry["unsupported_correlations"]}
        self.assertTrue(
            {
                "busy-period-to-turn-or-task",
                "event-replay-or-completeness",
                "langfuse-native-parent-child-and-high-cardinality-joins",
                "nested-envelope-to-child-operation",
                "reported-versus-inferred-cost",
                "compaction-start-to-terminal",
                "legacy-structural-id-scope",
            }
            <= unsupported
        )

    def test_registry_matches_pinned_source_versions_and_does_not_infer_late_identity(
        self,
    ) -> None:
        compatibility = self.registry["source_compatibility"]
        package_lock = json.loads(
            (ROOT / "integrations/opencode-global/package-lock.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            compatibility["langfuse_plugin"],
            "@langfuse/opencode-observability-plugin "
            + package_lock["packages"][
                "node_modules/@langfuse/opencode-observability-plugin"
            ]["version"],
        )
        self.assertEqual(compatibility["opencode"], "2.2.5")
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn("langfuse/langfuse:4.82.0", compose)
        self.assertEqual(compatibility["langfuse"].split(";")[0], "4.82.0")
        self.assertEqual(len(compatibility["evidence"]), 3)
        self.assertIn("opencode --version", compatibility["evidence"][0]["evidence"])
        self.assertIn("timestamp", self.registry["identity_types"]["logical_operation"])
        self.assertIn(
            "turn", self.registry["identity_types"]["session_turn_busy_period"]
        )
        self.assertTrue(
            any("latest-prompt" in rule for rule in self.registry["privacy_rules"])
        )

    def test_current_delivery_ledger_does_not_overstate_legacy_or_unavailable_paths(
        self,
    ) -> None:
        components = {
            component["id"]: component
            for family in self.families
            for component in family["components"]
        }
        self.assertEqual(components["L4.input_tokens"]["delivery_state"], "implemented")
        self.assertEqual(
            components["L4.input_tokens"]["contract_conformance"], "legacy"
        )
        self.assertEqual(
            components["L5.inferred_cost"]["delivery_state"],
            "explicitly-unavailable-with-reason",
        )
        self.assertEqual(
            components["L8.compaction_duration"]["delivery_state"],
            "explicitly-unavailable-with-reason",
        )
        self.assertEqual(components["Q3.acceptance_rate"]["delivery_state"], "planned")


if __name__ == "__main__":
    unittest.main()
