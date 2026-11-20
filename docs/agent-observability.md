# Agent observability dashboards and reports

The measured dashboards and report use existing Langfuse data. They **do not call an LLM, add evaluation rules, request observation input/output, or backfill scores**. They do not need LM Studio to run. The optional evaluator below is separate. The independent [span-classification demo](../README.md#example-classify-spans-on-a-dashboard) remains scoped to synthetic spans.

## Dashboards

After connecting the global OpenCode plugin and starting Langfuse, run:

```sh
make agent-dashboards
```

The command prints two URLs:

- **OpenCode operations:** recorded root prompt turns, explicit error levels, turn p50/p95, model observation counts, explicit generation errors and recorded retries.
- **Models and project tools:** OpenCode model observations, duration and reported tokens/cost by model, plus **project-wide** tool counts, errors and durations.

Use the dashboard's time range and supported Langfuse filters to explore. Click through to individual observations or traces for detailed evidence, which may contain sensitive content. Counts are of observations that reached Langfuse, grouped by their start time; dropped or in-flight observations are not visible. Charts and API access require the existing project key. The definitions are editable in `scripts/integrations/agent_dashboard_definition.py`; rerun the command after changing them.

**Interpretation matters:** a root turn is a prompt turn, not an epic or necessarily a completed task. A turn that lacks an `ERROR` level is not proven successful: the current plugin ends interrupted and successful executions similarly. An error can be recovered. Tool charts include all `TOOL` observations in the Langfuse project, including other integrations; their latency is elapsed time, not evidence the tool was inefficient. Model cost and usage depend on what the provider recorded. The separate report shows token coverage. A zero in a native chart does not prove that unavailable or unexported observations did not occur.

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
```

The default is the last seven days, at most 10,000 observations. Windows are from-inclusive/to-exclusive and limited to 90 days. `--max-observations` can be set between 1 and 100,000; if the scan is incomplete, pagination repeats, or observations conflict, the command fails rather than printing a deceptively complete report. Data can arrive late or change while a report is being made. Re-run a bounded window when necessary.

The JSON contains a schema version, source, time window, computation time, structural coverage, root and all-turn counts/latency, linked generation/tool counts and errors, token sums with per-field reported/missing counts, tool/model breakdowns, and UTC day/ISO-week buckets. The report uses nearest observed parent-turn ancestry within each trace, not a timestamp guess. Unmatched project tools and unmatched candidate generation/retry observations are counted separately; they can belong to other integrations, incomplete traces, or a parent outside the selected window. Generation usage is attributed once to its nearest turn, so child turns are not silently added again to their parent. Token `total`, input, output, reasoning and cache counters are reported separately; provider semantics may overlap, so do not sum them together.

The report also counts **candidates** where the same tool name appears three times consecutively, or two names alternate twice, ordered by recorded start time within a turn. It does not compare tool arguments or establish causality; valid searches, parallel calls and incomplete windows can match these patterns. Treat them as leads for human review, not proof of a cycle, waste or a wrong decision.

`--include-identifiers` additionally emits trace, observation and session IDs, structural per-turn timelines, evidenced child turns and session aggregates. Timeline order is by recorded start time, not a proof of causality; overlapping operations may be concurrent. Per-turn timing unions overlapping observed child generation, tool and child-turn intervals, clipped to the turn. Time not covered by those observed intervals is **not** proof of a wait or inefficiency: spans may be missing, outside the window, or not instrumented. Invalid child intervals make the uncovered figure unknown. Cyclic child-turn links are counted and omitted. Task completion, satisfaction, blockers, root cause, epic/subtask links and compaction quality remain **unknown** until separately evidenced. An explicit error is an observed error, not proof of a failed task.

The API field selection excludes `io` and `metadata`; the API's `basic` group may include a status message, which the report discards without emitting it. Tool/model names, counts, time ranges and optional IDs can still be sensitive. Reports print to stdout: do not paste them into a public issue, chat, or AI system without review. If saving one, set a restrictive `umask`, choose a private location, and protect and delete the file according to your data policy. The script never creates an output file or schedules itself. For a recurring job, explicitly configure your scheduler with a bounded time window, non-overlap, private output permissions and appropriate retention; do not schedule model evaluations merely by scheduling this report.

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
