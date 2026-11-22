# Reversible rollout and report-schema compatibility

**Plan:** `task_6307`, brief `memory_403`, shared contract `memory_398`, plan revision 1.1

**Status:** proposed rollout and validation contract; no runtime behavior is implemented by this document.

**Authority:** planning/tracking only. Any code, dashboard write, report migration, backup change, or real-content processing requires a separate implementation task and authorization.

**Evidence boundary:** current repository code, tests, and documentation were inspected read-only. No live Langfuse API, report, trace, credential, backup archive, or restore was accessed.

## Decision

Keep the current dashboards and report as the stable baseline. Roll out future report fields as an opt-in additive schema, and future native-dashboard changes as separately enabled slices with an authenticated read-only desired-state preview before any write. Preserve ownership state and old definitions so a disabled slice can fall back without deleting Langfuse resources or altering traces, annotations, backups, credentials, or user changes.

Recovery is fail-closed: a missing ownership file does not prove that resources are unowned, and a matching name is not proof of ownership. Never adopt or delete a resource by name. If ownership, project identity, resource fingerprint, or the result of a previous write is uncertain, stop and require operator inspection of exact resource IDs.

This plan does not authorize a custom browser viewer. Use the authenticated Langfuse UI only for supported source aggregates and the bounded CLI for derived report/query output, as decided in [`private-report-view-feasibility.md`](private-report-view-feasibility.md). Unsupported views and metrics remain unavailable.

## Current baseline and compatibility hazards

- `scripts/integrations/agent_report.py` emits `schema_version: "1.0"`, the source `langfuse-v2-observations`, `expected_langfuse_version`, `computed_at`, and the explicit UTC window. It contains legacy fields including `reported_total_tokens` and `generations_with_total_tokens`; existing tests assert their values and omission behavior. Report files are operator-selected outputs, not a managed database.
- The report's token detail distinguishes reported numeric zero from missing values. Its existing fields and semantics must not be renamed, reinterpreted, synthesized, or backfilled by an additive release.
- `scripts/integrations/agent_dashboard.py` uses a private `.ai-observatory.agent-dashboard-state.json`; `span_dashboard.py` owns a separate private state file. The owner state validates project, file mode/owner, resource IDs and fingerprints. Drift is rejected, and definitions that remove a recorded resource are rejected rather than deleting it.
- Dashboard writes use an unstable Langfuse API without conditional updates. The manager records a pending operation before a write and refuses retry when the outcome could be uncertain. This is a safety invariant, not an automatic recovery mechanism.
- Ownership files are intentionally excluded from the manual backup archive. The archive and restore format are out of scope; after loss/restore, resource ownership cannot be inferred from a display name. Existing docs require inspection before recreation.
- Current dashboard application has no read-only desired-state diff. Its `apply` path may create or patch resources; it is not a dry-run.

## Report schema and version policy

1. **Freeze the current default.** Until consumers are inventoried and tested, default output remains schema `1.0` with the current shape and values. Do not silently switch the default or rewrite saved reports.
2. **Introduce opt-in revisions.** An additive implementation may expose `--schema-version 1.1`. Version `1.1` must preserve every `1.0` key, type, value meaning, and legacy total-token key, and may add fields only. A breaking semantic revision may use `--schema-version 2.0`; it must document every changed meaning and provide an explicit migration/consumer plan. Both remain opt-in, and a consumer must explicitly declare support for each version; do not assume strict consumers ignore unknown fields.
3. **Separate versions by responsibility.** The report envelope should carry independent `definition_version` (metric formulas/populations) and `transform_version` (code mapping source rows to output), in addition to its schema version and existing source-version fields. Dashboard code should have its own `dashboard_definition_version`; do not overload the report transform version or invent unsupported Langfuse API metadata fields.
4. **Version semantic changes.** Adding a field without changing prior meanings is a minor additive revision. Removing/renaming a field, changing a formula/population, changing missingness, or changing attribution is a breaking revision and requires a new major schema plus an explicit migration/consumer plan. For token coverage, schema `1.0` keeps its legacy `missing` meaning; schema `2.0` may distinguish absent/null `missing` from present-invalid `excluded` only when explicitly selected. Increment definition and transform versions independently when only one changes.
5. **Preserve historical evidence.** Treat each generated report as a point-in-time snapshot of its own source window, versions, and computation time; saved copies remain operator-owned. Do not auto-migrate, recompute, or backfill historical reports. Mixed-version consumers dispatch by each report's declared schema and retain unknown/unavailable values rather than coercing them to zero.

The exact `1.1` schema and consumer inventory must be reviewed before implementation. Any `2.0` implementation must have synthetic compatibility tests and document its changed semantics and opt-in control. If compatibility cannot be proven, retain the `1.0` default and defer the new output rather than weakening validation.

## Incremental rollout and switches

| Slice | Proposed control | Default | Reversal |
| --- | --- | --- | --- |
| Existing report | No new switch; freeze the existing `1.0` contract. | On as today | Revert code only if a regression is found; historical output is untouched. |
| Additive report fields | Explicit per-run schema selection, initially opt-in `1.1`. | `1.0` | Select `1.0` or use the prior code; no report files are rewritten. |
| Breaking report semantics | Explicit per-run major schema selection (for token missingness, `2.0`). | `1.0` | Select `1.0`; no report files are rewritten or migrated. |
| New native view/resource | Per-view enable switch, separate from applying definitions; require a reviewed dry-run and explicit apply. | Off | Disable the view. Restore prior definitions only for still-owned, unchanged resources; retain new resources rather than deleting them. |
| New producer or independent evaluation | Its own source/readiness gate and switch, outside this plan. | Off/unavailable | Disable that producer/evaluation only; retain already-recorded evidence and annotations. |

Switches are per slice, not a global “all metrics ready” toggle. A feature switch may change presentation or future collection only; it never deletes traces, scores, annotations, backups, or user-owned resources. No automatic schedule, historical evaluation, or backfill is introduced by enabling a view.

## Desired-state preview and ownership contract

A future `--dry-run` must be read-only with respect to Langfuse: GETs only, no POST/PATCH/DELETE, no ownership-file creation or rewrite, no pending-operation marker, no report persistence, and no output of credentials or raw trace fields. It may acquire the existing private checkout maintenance lock for concurrency safety; if that coordination file has to be created, document it as the sole permitted local side effect, not as an application-state change. It should show a bounded diff per resource with only these outcomes:

| Outcome | Meaning | Safe action |
| --- | --- | --- |
| `unchanged` | Exact owned resource matches the desired projection. | No write. |
| `update-owned` | Exact owned ID is unchanged from its recorded fingerprint but differs from desired state. | Show the projected field diff; an explicit apply is required. |
| `create` | A valid owner manifest establishes a new resource key, or a separate explicit first-install initialization has completed read-only discovery with no conflicting candidate. | Show planned creation; explicit apply required. Missing state alone never qualifies. |
| `drift-conflict` | A recorded resource differs from its ownership fingerprint or project. | Stop the whole apply; do not overwrite or adopt. |
| `ownership-unknown` | State is missing/invalid, or a name/other weak match suggests a prior resource. | Stop; list no guessed ownership and require exact-ID operator reconciliation. |
| `pending-uncertain` | The state records a write whose server outcome is unknown. | Stop; do not retry or clear the marker automatically. |
| `retained-not-deleted` | A previously owned resource is no longer in desired definitions. | Keep it and its ownership record; require a separately reviewed retirement plan. |

Dry-run must prove zero remote writes and no persistent ownership/report/application-state changes with a synthetic fake client/filesystem; tests may separately assert the documented maintenance-lock side effect. It must not claim that a GET is a transaction or that the server cannot change between preview and apply. Because the Langfuse API lacks conditional writes, apply must re-fetch and re-check all relevant fingerprints immediately before mutation, remain serialized by the checkout maintenance lock, and fail closed on drift.

Ownership state remains private, regular-file-only, current-user-owned, no-follow, and atomically compare-and-replaced. Preserve the project check, exact IDs, fingerprints, nonce, and pending-write safety. Do not adopt by dashboard/widget name and do not expand a write to resources absent from the ownership manifest.

### Ownership-state versions, loss, and restore

- Read the existing v1 state without changing it during preview. Any future state migration is a separately versioned, explicit apply with a private migration record and rollback path; never silently rewrite or delete the original state.
- Keep the v1 manifest's known resource keys stable. Put new resource families in a separately versioned manifest so an older binary does not reject an unknown key or mistake a new resource for an old one. Existing resources can be restored by the old definition only when the same exact ID remains recorded and its current fingerprint is verified.
- If state is missing after restore, a read-only discovery may enumerate exact project resources where the API supports it, but must label all matches `ownership-unknown`. Names, descriptions, timestamps, and visual similarity are not identity. Do not recreate, adopt, patch, or delete until an operator reconciles exact IDs from an authorized private record.
- The manual backup archive is intentionally unchanged. Its contents do not include dashboard ownership state; this plan adds no archive member and performs no restore.

## Rollback procedure

1. Stop applying the new slice and turn off its per-run/per-view switch.
2. Run the read-only preview against current private ownership state. If it reports drift, a different project, missing ownership, or a pending write, stop and inspect; do not attempt automatic rollback.
3. For unchanged resources still owned by exact ID, use the compatible prior definition to restore the previous projection. Re-fetch fingerprints and require explicit apply; never delete resources as part of rollback.
4. For newly created resources, leave them retained and disabled/unreferenced. Their later removal, if ever needed, is a separate explicit task requiring proof of ownership and review. An older binary must continue to operate the untouched v1 manifest and baseline dashboard resources.
5. Select report schema `1.0` or the previous code path. Leave previously written reports and all Langfuse evidence unchanged; no rollback rewrites history.

Rollback is unavailable when ownership is uncertain or a user edited a managed resource; preserving that edit takes precedence over automatic restoration. Record the conflict and resolve it manually under the project authorization boundary.

## Per-family delivery and evidence ledger

Keep delivery state separate from evidence class. Use the existing `docs/specs/harness-metric-contracts.json` as the normative per-component ledger; do not create a competing status registry. Each ledger row is per **atomic component and output surface** (native dashboard, structural report, query, or future renderer), so an implemented report field cannot be mistaken for a shipped visual chart. Record component/family ID, primary owner task, readiness/source, definition and transform versions, evidence class, population/denominator, missing/excluded/ambiguous/pending counts or reason, supported filters, delivery state, validation reference, and last verified window/version. Only the owning task may advance a delivery state, with links to the source and scoped validation evidence.

Use only these delivery states: `planned`, `source-supported`, `implemented`, `validated`, and `explicitly-unavailable-with-reason`. `source-supported` is not implementation; `implemented` is not validation; creating a chart or changing an `oc` status is not evidence. Preserve evidence classes `observed`, `derived`, `attested`, `judged`, and `unavailable`; these are not delivery states. Missing remains missing, not zero.

For the current-data milestone, keep future-instrumentation (`I`) and independent-evaluation (`E`) components explicitly unavailable with the missing source/validation gate. Current Langfuse/report-supported (`N`/`R`) fields may be listed only with their actual project-linked population and limitations; they are not full-capture or task-success measures. Visual coverage views remain unavailable under the separate renderer decision.

## Synthetic implementation gate (future task; not executed here)

The following disposable fixtures are required before an implementation slice can claim the rollout behavior. Existing tests already cover some safety invariants, but they do not implement or certify the proposed dry-run/migration behavior.

| Fixture | Required assertions | Current status |
| --- | --- | --- |
| Desired-state dry-run | Fake API sees only GET; no writes; state bytes, mode, and existence are unchanged; every diff outcome is explicit. | Not implemented. |
| UI drift and unrelated resources | Drift aborts before all writes; unmanaged resources remain byte-for-byte unchanged; no name-based adoption/deletion. | Partial existing coverage for edited managed resources; extend for dry-run and unrelated resources. |
| Ownership-state loss / restored installation | Missing state produces `ownership-unknown`; candidate names are not adopted; no duplicate create/delete; prior private evidence can be reconciled by exact ID only. | Recovery instructions exist; executable fixture not present. |
| Interrupted apply | Simulate server completion followed by lost response and persisted `pending`; repeated apply/dry-run does not retry or clear it. | Existing uncertain-create test covers no retry; add mixed-update and dry-run assertions. |
| Mixed report versions | Synthetic `1.0` and `1.1` reports load independently; `1.0` legacy totals remain exact; new fields are additive; zero, missing, excluded, and unavailable stay distinct. | Not implemented. |
| Rollback after additive view | A newer manifest can be disabled; prior binary still reads the untouched v1 manifest; prior baseline definitions are recoverable; no resource deletion occurs. | Not implemented. |
| Restore/archive boundary | Disposable restore/discovery fixture preserves the existing archive member contract; no ownership file is injected or manual archive rewritten. | Existing backup code uses an exact allowlist; no dashboard-state restore fixture. |

All fixtures must use temporary directories, synthetic identifiers/data, fake API clients, and no live restore or real telemetry. `make check`, `make site-build`, and `git diff --check` are required for the future implementation gate; the site output must remain on its explicit public allowlist.

## Sources checked

- `scripts/integrations/agent_report.py` and `tests/test_agent_observability.py` for report version and legacy token semantics.
- `scripts/integrations/agent_dashboard.py`, `scripts/integrations/span_dashboard.py`, and `tests/test_span_dashboard.py` for ownership, drift, uncertain writes, and current synthetic coverage.
- `scripts/backup/manage.py`, `docs/security.md`, and `docs/agent-observability.md` for the unchanged manual backup/restore and private state boundary.
- `docs/specs/harness-metric-contracts.md`, `docs/specs/harness-metric-contracts.json`, `docs/specs/harness-telemetry-threat-model.md`, and `docs/specs/private-report-view-feasibility.md` for evidence, delivery states, privacy, and renderer decisions.

This specification is a rollout plan only. No code, test fixture, report, dashboard, backup, credential, or live service was changed or exercised.
