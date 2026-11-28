# Agent observability dashboards and reports

The measured dashboards and report use existing Langfuse data. They **do not call an LLM, add evaluation rules, request observation input/output, or backfill scores**. They do not need LM Studio to run. The optional evaluator below is separate. The independent [span-classification demo](../README.md#example-classify-spans-on-a-dashboard) remains scoped to synthetic spans.

The source-backed [capability and instrumentation matrix](https://github.com/dmoliveira/coding-agent-observability/blob/main/docs/specs/harness-capability-matrix.md) defines planned C1/C2/C10 inventory contracts and documents paths that are only partially observed or currently opaque. It is a specification, not a live capability listing or dashboard.

## Dashboards

After connecting the global OpenCode plugin and starting Langfuse, run:

```sh
make agent-dashboards
```

The command prints three URLs:

- **OpenCode runtime events:** recorded busy-period start/success/failure/interruption and compaction lifecycle events, when the optional structural capture below is enabled.
- **OpenCode operations:** compact counts and latency indicators, root-turn level mix, and line/bar trends for prompt/model volume, p95 latency and explicit errors.
- **Models and project tools:** model-level p50/p95 latency, reported input/output/total token and cost totals/trends, raw usage-detail categories by model, explicit model errors, plus **project-wide** tool counts, errors, latency and trends.

Use the dashboard's time range and supported Langfuse filters to explore. Click through to individual observations or traces for detailed evidence, which may contain sensitive content. Counts are of observations that reached Langfuse, grouped by their start time; dropped or in-flight observations are not visible. Charts and API access require the existing project key. The definitions are editable in `scripts/integrations/agent_dashboard_definition.py`; rerun the command after changing them.

Time-series charts show volume, p95 latency, input/output/total token and cost sums, and tool activity; bars show explicit error or runtime-event categories. Input/output measures use Langfuse's v2 usage-detail key matching; provider keys can overlap, so these are not normalized token shares and must not be added to `totalTokens`. The usage-detail pivot shows up to 20 raw key/model pairs; additional pairs may be omitted by the cap, category values may overlap, and absent keys are not a coverage measure. Do not add these values to each other or to the input/output/total sums. Native Langfuse sums have no reported/missing denominator and an empty usage or cost map may appear as zero; that zero is not evidence of zero usage/cost or complete coverage. The structural report preserves per-token reported zero versus missing usage fields; it does not currently provide cost-field coverage. A pie is used only for the low-cardinality Langfuse level mix of recorded root turns; `DEFAULT` is not success, and missing levels or observations are unknown. Compact count widgets and charts use the dashboard's 12-column layout. Native charts intentionally omit success/failure rates because unknown or unexported observations would make their denominators incomplete.

**Interpretation matters:** a root turn is a prompt turn, not an epic or necessarily a completed task. A turn that lacks an `ERROR` level is not proven successful: the current plugin ends interrupted and successful executions similarly. An error can be recovered. Tool charts include all `TOOL` observations in the Langfuse project, including other integrations; their latency is elapsed time, not evidence the tool was inefficient. Model cost and usage depend on what the provider recorded. The separate report shows token coverage. The managed OpenCode adapter preserves explicit numeric zero and omits unavailable usage/cost fields, including on reload recovery; it does not synthesize total tokens from other fields. A zero in a native chart does not prove that unavailable or unexported observations did not occur.

The native project-tool name, error, p50 and p95 bar charts show at most 20 rows. Their descriptions disclose the cap; omitted names are not zero. Use Langfuse's authenticated **Observations** table to search the project-wide long tail. For the distinct harness-linked population, `make agent-report` emits every linked tool name found in its successful bounded scan under `linked_tools.by_name`, sorted by name; it is not a top-N list. A report with more than 2,000 distinct tool names, or one that exceeds its other row, response, time or output budgets, fails rather than returning a truncated JSON result. Reaching the end of a scan does not prove complete Langfuse capture. To opt in to T5's exact interval calculation, run `python3 scripts/integrations/agent_report.py --schema-version 2.0`; `linked_tools.elapsed_latency` and each `by_name[].elapsed_latency` then report nearest-rank p50/p95 with eligible, reported, missing and invalid counts from ordered `endTime - startTime` intervals. The existing `latency` fields in schema 1.0 retain their source-latency meaning and values. Missing endpoints are not assumed to be zero or classified as pending; source capture coverage remains unknown. Explicit `ERROR` levels are observed lower bounds, not failure rates, and a linked observation does not establish which integration emitted it.

## Optional runtime lifecycle capture

After `make connect-opencode-global`, install a **separate** OpenCode plugin for structural execution and compaction events:

```sh
make connect-opencode-events
make agent-dashboards
```

It reuses the existing private project credentials and does not change the original tracing plugin or project configuration. The running OpenCode service watches plugin entries and may load it immediately. It records an opaque session ID, hashes of event IDs and permission-request IDs, timestamps, event types, limited enumerated reasons/replies, and valid numeric token fields on compaction completion/failure. It does **not** export prompts, tool input/output, permission actions or resource paths, permission text, error text or compaction summaries. It records starts, successful/failed/interrupted **session busy periods**, compaction starts/completions/failures, and permission asks/replies; one busy period may include several prompts. A busy period that drains successfully is not evidence that a request or task succeeded. No historical events are backfilled.

Capture is best effort: disabled plugins, restarts, disconnects, queue limits and export failures can lose events. It retries a disconnected event subscription, uses a bounded queue, and logs only generic warnings. A zero or mismatch between starts and outcomes is not proof of success, failure, or complete coverage. The default OpenCode event bus may not retain historical event payloads, so missing events cannot necessarily be replayed. The report keeps these events separate from individual turns and marks coverage unknown.

When OpenCode provides them, compaction input, output, reasoning and cache token fields are reported separately from generation usage. The report gives a reported/missing count for each field among observed compaction completion/failure events; lost events are not included in that denominator. Provider conventions and other instrumentation may overlap, so do not blindly add these counts to generation totals. Dashboard token sums do not themselves establish field coverage.

Permission asks and replies share an opaque hash of their explicit OpenCode session and request IDs. The report calculates elapsed wait only when exactly one ask and one reply are present in that trace, their session IDs match, and timestamps are valid and ordered. It separately counts unmatched, ambiguous, invalid and reversed pairs. An ask before the reporting window or a reply after it cannot be paired; dropped events and disabled capture are unknown. Durations describe elapsed time pending a permission response, not task success, active work or blame. A reply of `once`, `always` or `reject` is reported as an event category only.

Installation checks the project's enabled Langfuse evaluation rules and refuses if it cannot prove that they exclude `EVENT` observations. Rules can change later; an administrator can add a broad rule that evaluates new structural events. Review such rules and any LM Studio/network routing before changing them. The command never adopts or overwrites an existing plugin entry, and refuses a modified or different installed copy. It does not enable raw OpenCode event persistence.

To remove only the verified managed plugin entry, run `make disconnect-opencode-events`. The plugin directory and recorded data are preserved; the watched OpenCode server may unload it immediately. The installed copy can be disconnected even after the repository code is upgraded; reconnecting a different version first requires disconnecting the verified prior entry. Do not edit the entry concurrently with connect/disconnect: the filesystem does not provide a conditional unlink. If the entry, installation manifest or installed files have changed, the command refuses to remove them.

To disable **only this capture** in a new standalone OpenCode server, use `AI_OBSERVATORY_EVENTS_DISABLED=1 opencode --standalone`. `AI_OBSERVATORY_DISABLED=1 opencode --standalone` disables both this plugin and the global tracing adapter. An environment change to a client does not change an already-running shared server. Neither switch deletes recorded data or disables independent exporters.

## Machine-readable report

```sh
make agent-report

# Bounded UTC window; use explicit offsets in all timestamps.
python3 scripts/integrations/agent_report.py \
  --from-time 2026-11-01T00:00:00Z --to-time 2026-11-08T00:00:00Z

# Optional private identifiers and per-turn structural timelines.
python3 scripts/integrations/agent_report.py --include-identifiers

# Optional: scan one session only (child sessions are excluded).
python3 scripts/integrations/agent_report.py --session-id SESSION_ID

# Opt in to schema 2.0's disjoint reported/missing/excluded token counts.
python3 scripts/integrations/agent_report.py --schema-version 2.0
```

The default is the last seven days, at most 10,000 observations. Windows are from-inclusive/to-exclusive and limited to 90 days. `--max-observations` can be set between 1 and 100,000. Each request asks for at most 250 rows, allows at most 30 seconds (or the remaining report deadline), and the scan is capped at 1,000 pages and 64 MiB of total response data; each response is also capped at 4 MiB. Querying, computation and JSON encoding have a 120-second local wall-time budget and admit one report process per checkout; separate checkouts have independent locks. Stdout delivery is outside that deadline because a downstream consumer can stall or break the pipe. Tool/model breakdowns allow at most 2,000 distinct names each and at most 10,000 combined day/week model groups. Identifier details are limited to 10,000 observations, and complete JSON output is capped at 16 MiB. These are explicit resource limits, not measured universal performance targets. If any budget is exhausted, pagination is incomplete, or observations conflict, the command exits nonzero before writing report JSON; narrow the window or omit identifiers rather than interpreting the failure as an empty result. A broken or interrupted stdout stream can still contain partial transport and is not a valid report. A local timeout does not prove that Langfuse canceled server-side work. Data can arrive late or change while a report is being made. Re-run a bounded window when necessary.

The JSON contains a schema version, source, time window, computation time, structural coverage, root and all-turn counts/latency, linked generation/tool counts and errors, token sums with per-field reported/missing counts, tool/model breakdowns, separate optional runtime-event counts, and UTC day/ISO-week buckets. `linked_tools` includes only TOOL observations linked by observed ancestry to an OpenCode turn; the emitting integration is not established. Schema 1.0's existing `latency` fields retain their source-latency meaning. In opt-in schema 2.0, `linked_tools.elapsed_latency` and each tool-name entry's `elapsed_latency` use ordered source `endTime - startTime` intervals and report eligible, reported, missing and invalid counts with p50/p95; missing endpoints and invalid/reversed intervals are excluded from percentiles, not converted to zero. The finite-sample percentile method is nearest-rank (`ceil(p*n)-1`). Schema 2.0 adds per-field `excluded` counts and gives each bucket's `generation_token_usage` sum, reported, missing and excluded counts for input, output, total, reasoning and cache fields among **linked generation observations with a valid start time in that bucket**. `generation_token_usage_by_model` gives the same summaries within each bucket by model label, including an observation count; `(unknown)` groups observations without a usable model name. Model-group output is bounded by the explicit cardinality and output-byte budgets above, not by silently truncating top-N groups. Linked generations with missing or invalid start times remain in overall linked-generation usage but are not assigned to a day/week; `coverage.linked_generations_without_valid_start_time` counts them. The broader `coverage.missing_or_invalid_start_time` includes other row kinds too. Reported zero remains distinct from missing; unlinked generation candidates are excluded from these bucket denominators and counted separately. Legacy `reported_total_tokens` and `generations_with_total_tokens` fields remain available. The report uses nearest observed parent-turn ancestry within each trace, not a timestamp guess. Unmatched project tools and unmatched candidate generation/retry observations are counted separately; they can belong to other integrations, incomplete traces, or a parent outside the selected window. Generation usage is attributed once to its nearest turn, so child turns are not silently added again to their parent. Token `total`, input, output, reasoning and cache counters are reported separately; provider semantics may overlap, so do not sum them together.

The default schema 1.0 remains unchanged: its legacy `missing` count includes absent/null and invalid token values. Schema 2.0 is opt-in with `--schema-version 2.0`; there, a finite nonnegative number (including zero) is `reported`, an absent or null value is `missing`, and a present invalid value is `excluded`. Raw invalid values and malformed `usageDetails` containers are discarded in both versions. Schema 2.0 is a breaking semantic revision; consumers must inspect `schema_version`. No saved reports are migrated. These counts describe only the bounded report input and do not establish complete source capture. `coverage.exact_duplicates_ignored` counts repeated `(traceId, id)` identities whose compared report projection is unchanged, both within and across pages. The comparison covers fields selected for the report and keyed fingerprints of invalid selected token values; changes only to discarded source fields are not detected. A difference in the compared projection fails the report before JSON is emitted; it is not a counted C6 conflict. Invalid usage fingerprints are ephemeral, keyed and never emitted or retained after the scan. `coverage.ambiguous_child_turn_links` is limited to child-turn relationships and is not a generic ambiguous-record count. C6 invalid, conflicting and generic ambiguous record components are not emitted by this report.

The report also counts **candidates** where the same tool name appears three times consecutively, or two names alternate twice, ordered by recorded start time within a turn. It does not compare tool arguments or establish causality; valid searches, parallel calls and incomplete windows can match these patterns. Treat them as leads for human review, not proof of a cycle, waste or a wrong decision.

`--include-identifiers` additionally emits trace, observation and session IDs, structural per-turn timelines, evidenced child turns and session aggregates. Timeline order is by recorded start time, not a proof of causality; overlapping operations may be concurrent. Per-turn timing unions overlapping observed child generation, tool and child-turn intervals, clipped to the turn. Time not covered by those observed intervals is **not** proof of a wait or inefficiency: spans may be missing, outside the window, or not instrumented. Invalid child intervals make the uncovered figure unknown. Cyclic child-turn links are counted and omitted. Task completion, satisfaction, blockers, root cause, epic/subtask links and compaction quality remain **unknown** until separately evidenced. An explicit error is an observed error, not proof of a failed task.

The API field selection excludes `io` and `metadata`; the API's `basic` group may include a status message, which the report discards without emitting it. Tool/model names, counts, time ranges and optional IDs can still be sensitive. Reports print to stdout: do not paste them into a public issue, chat, or AI system without review. If saving one, set a restrictive `umask`, choose a private location, and protect and delete the file according to your data policy. The script never creates an output file or schedules itself. For a recurring job, explicitly configure your scheduler with a bounded time window, non-overlap, private output permissions and appropriate retention; do not schedule model evaluations merely by scheduling this report.

### Optional explicit task attestations

Task outcomes and epic/turn links are not available from the runtime events. To report **claims explicitly provided by an operator**, create a private, current-user-owned mode-600 JSON file, for example:

```json
{
  "schema_version": "1.0",
  "as_of": "2026-11-20T12:00:00Z",
  "tasks": [
    {"id": "example_epic", "kind": "epic", "parent_id": null, "status": "in_progress", "validation": "unknown", "attested_by": "human", "bindings": []},
    {"id": "example_task", "kind": "task", "parent_id": "example_epic", "status": "completed", "validation": "passed", "attested_by": "agent", "bindings": [{"trace_id": "example_trace", "turn_id": "example_turn"}]}
  ]
}
```

Replace example IDs with opaque IDs you explicitly intend to use; trace and turn IDs can be found in a private `--include-identifiers` report. Never put task text, paths, commands, personal information or secrets in IDs. `as_of` must be an ISO timestamp with seconds, up to six fractional digits and `Z` or a numeric `±HH:MM` offset; output is normalized to UTC. Every task has exactly the fields shown. `kind` is `epic`, `task` or `subtask`; `status` is `not_started`, `in_progress`, `completed`, `blocked`, `failed`, `interrupted`, `canceled` or `unknown`; `validation` is `passed`, `failed`, `not_run` or `unknown`; `attested_by` is `human`, `agent`, `automation` or `unknown` and applies to the whole record. Use `unknown` where there is no evidence. IDs contain 1–160 ASCII letters, digits, `_` or `-`. A snapshot can contain at most 1,000 records, 16 exact turn bindings per record and 1 MiB. Parent IDs outside the snapshot are allowed and counted; duplicate IDs, cyclic parents, duplicate bindings within a record, unknown fields and duplicate JSON keys are rejected.

Select the file only when desired:

```sh
python3 scripts/integrations/agent_report.py --task-evidence /private/path/tasks.json
```

The separate `task_attestations` section reports counts by kind, status, validation and attestor, plus binding coverage and snapshot age in seconds relative to report `computed_at`. A future-dated `as_of` produces a negative age and is flagged; no age threshold is used to label a snapshot stale. These are **attested claims, not independently verified outcomes**: a record marked completed and passed is not a verified success, and age does not verify claim freshness. Counts include epics, tasks and subtasks; use the by-kind breakdown rather than summing hierarchies. The snapshot is as of its own timestamp and is not filtered by the observation time window or `--session-id`; those filters apply only when looking for explicitly bound turns. A binding matches only an existing OpenCode turn with the same trace and observation IDs. An absent match may be outside the window or session; a turn claimed by multiple tasks is ambiguous, not attributed. `--include-identifiers` additionally includes the task records and binding match states; protect that output.

`conditional_generation_usage` summarizes recorded generation tokens **conditional on the supplied task-to-turn claims**. It includes only uniquely claimed observed turns with non-cyclic ancestry and the generations attached directly to each such turn. Unmatched, ambiguous and cyclic bindings are counted separately. Missing token fields remain unknown; schema 2.0 separately counts invalid token values as excluded, while schema 1.0 preserves its legacy `missing` meaning. A turn with no observed generation does not imply zero usage. A child turn is not attributed to its parent's task unless the child is itself explicitly bound, and task hierarchy is never rolled up: this prevents automatic parent/child double counting but can leave usage unassigned. The report gives an aggregate over eligible unique turns; per-task figures are included only with `--include-identifiers`. Even then, `all_declared_bindings_eligible` does not prove telemetry was complete, nor that the task link or outcome was correct. Compaction event usage is separate and is not assigned to tasks; provider token fields may overlap and should not be blindly added together.

The report never discovers such files, writes them, sends them to Langfuse or an LLM, or schedules processing. Selecting a file authorizes only that one read; the file and output remain the operator's responsibility. The existing unknown-outcome caveats remain valid for runtime observations even when attestations are supplied.

### Bounded searchable query pages

The internal `scripts/integrations/agent_query.py` adapter provides cursor-paginated searches over **observed** project `TOOL` rows by name and `GENERATION` rows by model, using the Langfuse v4.82.0 v2 observations API filters. It is not a configured-tool or available-model catalog. Synthetic tests verify request construction against that contract; no live API call was made and another deployed version remains unverified. Each page contains at most 100 rows, follows the source cursor order (not categorical or alphabetical), and does not report a total count. `query_complete` becomes true only when the source omits its cursor; an empty page with a cursor is still incomplete. The opaque cursor remains in the in-process query object and is not included in page JSON.

Each query is bounded to 1,000 pages, 100,000 rows, 64 MiB total response data (at most 4 MiB per response), 120 seconds total wall time (at most 30 seconds per request), one active report/query page request per checkout, and 1 MiB serialized output per page. Budget exhaustion is an explicit failure, not an empty result or a completed inventory; a failed query object cannot be retried. A local timeout does not prove Langfuse canceled server-side work. Each page is serialized atomically. IDs are omitted unless explicitly requested, and the query excludes input, output, metadata and status messages. Names, timestamps, usage and optional IDs can still be sensitive.

This adapter is a backend building block only: there is no HTTP endpoint, browser viewer, authorization layer, HTML renderer, or public-site integration. Do not expose it over a network or treat its local cursor state as a viewer security boundary. The current feasibility decision keeps supported source aggregates in Langfuse's project-authenticated UI and computed views in operator-controlled CLI JSON; it does not authorize a custom browser viewer. A future viewer needs a separate user-authentication, project-authorization and output-encoding review. Langfuse data can be delayed, in flight, dropped or unexported; reaching the current cursor end does not prove source completeness.

## Metric contract

The normative metric schema, identity/outcome rules, cohort/statistical rules and atomic component registry live in `docs/specs/harness-metric-contracts.md` and `docs/specs/harness-metric-contracts.json`. The registry preserves all 82 family IDs and owners as 268 single-unit components. `planned` components are not collected or validated merely because they appear in the registry; existing pre-v1.1 outputs are marked `implemented` with legacy conformance, not as v1.2-validated. Check each component's source readiness and delivery evidence before making dashboard claims.

## Optional local-model judgments

On macOS with LM Studio running on `127.0.0.1:1234` and LM Link offline, try three **synthetic** cases using the same default local model as the existing demo:

```sh
make agent-eval-demo
```

The output compares the model's labels with a tiny hand-labeled synthetic set. This is an illustration, **not** a reliability certification. A model can misunderstand a clear task, infer the wrong blocker, or claim no rework where the evidence is incomplete. Review each result; do not use it as proof of task success, a safety decision, or an automatic action. `unknown` is an allowed answer. Change the model only to another locally downloaded model advertised by your LM Studio API, using `--model`.

For a deliberately selected real case, create a **private mode-600 JSON file** with exactly these fields. Replace all sample text with the minimum evidence you are authorized to send, and do not put secrets in `case_id`:

```json
{
  "case_id": "example_1",
  "task": "Synthetic example: add a greeting function.",
  "result": "The function was added.",
  "checks": "The unit test was reported to pass.",
  "observations": "No rework was reported."
}
```

Then run:

```sh
python3 scripts/integrations/agent_evaluate.py --evidence /private/path/case.json --allow-sensitive
```

The explicit flag acknowledges that the selected file's contents are sent to LM Studio. The command does not retrieve historical trace content or write scores to Langfuse. It accepts one selected case, bounds field and response sizes, checks that the model is local and LM Link is offline, uses a fixed loopback endpoint without proxies or redirects, and prints only the case ID, enum labels, model/rubric version and caveats—not the supplied text. It checks routing again immediately before each call; settings can still change concurrently. LM Studio authentication, local users, host administrators and Docker workloads remain part of the privacy boundary. File paths, evidence and report output remain your responsibility. The model may follow malicious instructions embedded in evidence despite the prompt; strict output categories reduce exposure but do not make its conclusions trustworthy. No schedule or batch/backfill is enabled.

## Ownership and recovery

The dashboards and widgets have their own private, ignored `.ai-observatory.agent-dashboard-state.json`; the original span demo and its state are independent. Only IDs created by this command are managed. If a managed resource has changed in the UI, or a previous write has an uncertain outcome, the command stops instead of overwriting/adopting/deleting resources. Langfuse's unstable API does not offer atomic conditional writes, so do not edit managed resources concurrently with an apply. Inspect the named dashboard/widget in Langfuse before deciding how to recover an interrupted operation. This ownership file is not part of the existing manual backup archive; after a restore or loss, inspect existing dashboards before recreating them. No existing backup or volume behavior is changed.

## Full roadmap

The [execution plan](https://github.com/dmoliveira/coding-agent-observability/blob/main/docs/plan/doing/opencode-observability.md) maps the requested task outcomes, trajectories, bottlenecks, tools, tokens, context, compaction, subagents, services, recovery and local-model assessments to current evidence, missing instrumentation, privacy controls, validation and sequenced phases. The current dashboards and report are measured foundations, not a claim that all roadmap metrics or model judgments are implemented.
