# Harness metric contract and identity model

**Contract version:** `aiobs.metric-contract.v1.1`
**Plan:** `epic_133`, pickup map `memory_415`, shared planning contract `memory_398`
**State:** schema and component registry are implemented; metric producers remain individually gated by the readiness and delivery state in `harness-metric-contracts.json`.

This contract makes a dashboard value auditable. It does not claim that every listed source is installed, every event is captured, or every metric is currently available. The registry contains the 82 approved family IDs and their primary `oc` owner IDs; composite families are decomposed into 262 named, single-unit components without changing ownership. Component count is versioned detail, not a new stable family catalog.

## Required metric instance

Every emitted or computed metric instance must carry these fields, even if some values are `null` with a reason:

| Field | Requirement |
| --- | --- |
| `schema_version`, `metric_id`, `definition_version`, `formula` | Stable component ID, versioned formula and definition; never reuse an ID for changed semantics. |
| `population`, `eligibility`, `exclusions` | Explicit source-backed cohort, sampling method/probability, admission/closure basis, follow-up/censoring and late-data policy. Snapshot selection is not lifecycle evidence. |
| `value`, `unit`, `aggregation`, `sample_size` | One atomic measure and declared aggregation. `value` may be null only with an availability reason; sample size may be null only with a provenance reason. Do not combine sibling components. |
| `numerator`, `denominator` | Required for rates/fractions; report both and sample size. A zero or undefined denominator yields unavailable, never zero or success. Paired differences use their own aggregation and are not rates. |
| `evidence_class` | `observed`, `derived`, `attested`, `judged`, or `unavailable`; do not collapse classes. |
| `source`, `source_version`, `producer_version`, `transform_version` | Exact source and transformation provenance. A null version needs a reason; readiness is a proposal, not evidence of installed capture. |
| `readiness`, `delivery_state` | Readiness is `N`, `R`, `I`, `E`, or a declared combination. Delivery is one of `planned`, `source-supported`, `implemented`, `validated`, or `explicitly-unavailable-with-reason`. |
| `coverage` | Separate eligible, reported, missing, excluded, ambiguous, pending/censored and unavailable counts, with reason codes. Counts may be null when unknown; missing is never zero. |
| `time`, `counter_epoch` | Exact timestamp basis, UTC window (`from` inclusive, `to` exclusive), timezone, clock uncertainty, late-data watermark/policy, and `computed_at`. Counter resets/restarts are scoped to an explicit epoch; non-counter measures explain why epoch is not applicable. |
| `uncertainty` | Explicit reported/not-applicable/small-sample/no-target/unavailable status; method, cluster unit and interval where justified. Never invent precision. |
| `dimensions`, `interpretation`, `drilldown` | Only evidenced dimensions and authorized opaque references. Explain limits and keep private evidence opt-in. |

The JSON registry includes a JSON Schema for metric instances, named atomic component IDs, formulas, numerator/denominator semantics, eligibility, exclusions, timestamp basis, definition version, delivery evidence, chart designs, readiness and ownership. For future/unsupported sources, `timestamp_basis_status: requires_source_probe` is explicit; the producer must replace that proposal with a verified field before emitting data. Runtime producers must supply the instance fields above; the registry itself is not evidence that those values have been collected.

## Evidence and readiness

- **Observed:** directly recorded source fields or events. Preserve the producer's meaning, including missing fields and explicit zero.
- **Derived:** deterministic computation over observed records. Include its source IDs, transform version, cohort, denominator, exclusions and ambiguity.
- **Attested:** an explicit human/agent/automation claim. It is not independently verified by its presence.
- **Judged:** a versioned human/model assessment with rubric, assessor, selected evidence, uncertainty and abstention. It is not an observed fact.
- **Unavailable:** unsupported, inaccessible, uncaptured, invalid, truncated, ambiguous, pending, censored or not yet measured. Include the specific reason; never substitute zero.

Readiness meanings: `N` existing native Langfuse data; `R` existing structural report data; `I` new instrumentation/adapter required; `E` independent assessment or controlled experiment required. A combined value means the component has multiple required inputs, not that it is partly available. A family can contain components with different readiness; component rows retain the family's declared readiness until an owner validates a more precise source.

## Identity and relationship rules

| Identity | Evidence rule |
| --- | --- |
| Source event / observation | Use the exact source-issued ID with source namespace/version. Deduplicate only by that identity. Conflicting duplicates fail closed. |
| Request attempt | One source-issued attempt ID. Retries are separate attempts and may link to one logical operation only when the producer provides that relation. |
| Logical operation | Explicit source grouping or an exact authored binding. Never infer it from adjacent timestamps, matching names, similar arguments or a wrapper count. |
| Nested envelope / child operation | Keep the wrapper and each child distinct. Link only via source parent/child IDs. An outer Code Mode or parallel call does not prove which nested operations ran. |
| Session / turn / busy period | Keep source session, observed Langfuse turn, and runtime busy-period identities separate. A terminal busy-period event may cover coalesced prompts; never attach it to the latest prompt by time. |
| Task / artifact revision / check | Require an explicit opaque task binding, immutable artifact revision identity, and check record linked to that revision. Snapshot status and command presence are claims, not verified outcomes. Do not join by title, path, timestamp or text similarity. |
| Agent / delegation | Use the actual source role and explicit parent/child session and handoff IDs. Dispatch, acknowledgement, child start, terminal state and accepted/useful consumption are separate facts. |
| Producer epoch / counter | Scope monotonic counters to an explicit producer/process epoch. Reset, restart, wrap, drop or missing upstream accounting makes completeness unavailable for the affected interval. |

IDs in exported views must be opaque and project-scoped. If source IDs require pseudonymization, use a private keyed mapping with controlled rotation; unsalted hashes of prompts, arguments, paths, tool output or memory are not an acceptable identity scheme. Never export those payloads to create a join key.

## Current source limitations

The version-specific evidence and handling rules are enumerated in `unsupported_correlations` in the JSON registry. In particular, OpenCode terminal execution events describe session busy periods that may coalesce prompts and cannot be joined to a turn/task; the default event bus does not guarantee replay or completeness; Langfuse native metrics cannot generally join parent/child observations; and project-wide tool observations are not necessarily OpenCode-originated. Auxiliary model-call purpose, nested wrapper children, reported-versus-inferred cost, and compaction start-to-terminal pairing remain unknown or unavailable without new source evidence. Existing structural IDs are legacy deterministic digests without project-keying and must not be treated as v1.1-conformant.

## Execution state and result are separate

Execution state: `queued`, `running`, `terminal`, `unknown`.
Terminal result: `acceptable`, `expected-negative`, `unexpected-error`, `timeout`, `canceled`, `policy-blocked`, `unknown`.

An outcome classifier needs a source-backed, versioned producer/task contract. Empty search results, correct denials, and tool-specific negative responses are not failures by default. A non-error level, successful drain, acknowledgement, or lack of an explicit failure is not proof of acceptance or task success. Keep unresolved, interrupted, censored, expected-negative and policy-blocked observations distinct.

## Dashboard semantics

Keep reliability, effectiveness, efficiency and measurement coverage as separate views, with one overview and focused drilldowns. Do not create a universal AI/harness health score. A health state requires a configured objective, eligible sample and adequate evidence coverage; `no-target`, `unknown`, and measured zero are distinct, and low sample size must not hide explicit severe failures.

## Cohorts, time, and aggregation

- State the admission, observation-start, completion or follow-up basis for every measure. Do not mix bases in a chart.
- For lifecycle rates, define a closed eligible cohort, left-truncation policy, follow-up horizon, late-data watermark, reopen handling and right-censoring. Show pending and unresolved counts beside classified outcomes.
- For duration percentiles, include only eligible completed intervals, show sample size and censored count, and compute from observations—not from averaged percentiles.
- For counters, record epoch and reset/drop evidence. Without independent producer receipt/admission/emission/acknowledgement/ingestion counts, capture completeness is unavailable.
- Label pooled versus weighted estimates and their selection probabilities. Hand-selected examples cannot be generalized to a production population.
- Use task/run clusters as the uncertainty unit when repeated observations share a task. Report small-sample/no-target states and effect sizes; do not invent precision or universal thresholds.
- Attribute every leaf once. Parent-inclusive and descendant-inclusive usage are separate views and must not be added. Provider token fields may overlap; do not synthesize `total` from other fields.
- A fraction with no eligible denominator is unavailable. Empty/missing source maps are not zero observations. Explicit reported zero remains a reported value.
- For paired comparisons, publish pair count, unresolved/missing pairs, effect size and task/run-clustered uncertainty. A paired difference is not causal unless assignment and order controls support that claim.

## Version and privacy boundary

Compatibility claims must name the tested OpenCode, Langfuse/plugin, API and producer versions. The current repository documents OpenCode `2.2.5`, `@langfuse/opencode-observability-plugin` `0.7.0`, Langfuse `4.82.0`, and Langfuse v2 observations/metrics interfaces; any other deployment remains unverified until isolated contract tests pass.

Never add prompts, reasoning, tool arguments/results, command lines, error text, paths, memory contents, secrets or raw evidence to metric records. Names and IDs may themselves be sensitive. Keep explicitly selected private drilldowns separate from default aggregates. Do not backfill history, schedule processing, invoke private evaluations, change credentials/retention, or restart a live agent as part of contract validation.

## Delivery status

The shared ledger states are `planned`, `source-supported`, `implemented`, `validated`, and `explicitly-unavailable-with-reason`. Existing pre-v1.1 outputs are marked `implemented` with `contract_conformance: legacy`; they are not falsely labeled validated. `validated` requires the component to emit the v1.1 instance schema and pass cohort, missingness, privacy, and negative-path checks. A family only carries per-state component counts; family rollups are not metric instances. Unsupported components retain a specific unavailable reason. Task completion or a rendered chart alone cannot advance this ledger.
