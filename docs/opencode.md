<a id="opencode-integration"></a>

# OpenCode tracing with self-hosted Langfuse

Run `make connect-opencode-global` once in a terminal, using an API key from your private Langfuse project's settings. The command verifies the key against an explicit loopback endpoint or the HTTPS Tailscale URL in `AI_OBSERVATORY_URL`, then installs a global OpenCode 2 plugin. It does not change `opencode.json(c)`, `my_opencode`, existing project files, or unrelated credentials. It refuses to overwrite an existing plugin entry or a modified managed installation.

After installation, ordinary OpenCode sessions across projects use the adapter by default. The global plugin directory is watched, so the running service may load it immediately. It can record prompts, assembled instructions and previous conversation context, reasoning, model output, code, and tool inputs and outputs. Previously stored context can appear in a new trace even if that context predates installation. Review the [security and privacy guidance](security.md) before use.

## Start without this integration

```sh
AI_OBSERVATORY_DISABLED=1 opencode --standalone
```

The variable must reach a **new standalone server**: passing it to a client of an already-running shared service does not change that service. The switch affects this adapter only, not other plugins, project-specific Langfuse integrations, or independent OpenTelemetry exporters. It does not remove data already exported. A standalone process still uses your normal OpenCode account and data directories.

## Configuration and coverage

The credentials file is `~/.config/ai-observatory/opencode-global.json` (or under `$XDG_CONFIG_HOME`). Its directory and file must be owned by you with modes `700` and `600`. The adapter ignores inherited `LANGFUSE_*` keys and destination settings, rejects invalid or unsafe credentials, and never falls back to Langfuse Cloud. API keys and trace data can still be read by sufficiently privileged local users or administrators.

The adapter uses the pinned `@langfuse/opencode-observability-plugin@0.7.0` with integrity-checked changes: credentials are passed directly rather than through the shared process environment; inherited OTLP headers, sampler and OTLP-specific TLS overrides are not applied to this exporter; HTTPS certificate verification remains enabled; media uploads are disabled; and unloading one OpenCode location does not clear another location's active trace state. An unexpected or unprepared upstream bundle disables this adapter rather than running with different defaults. Media uploads being disabled does **not** redact inline attachment data that may appear in a trace.

The integration covers the official plugin's session, generation, and tool instrumentation; it does not promise every experimental or non-session model call is traced. Project policies can disable plugins. Other tracing plugins can independently export the same session, including to another destination: review those configurations separately. If Langfuse is unavailable or the plugin cannot start, OpenCode continues but traces can be lost; this is not a durable offline queue. Enabling HTTP, network or TLS debugging through `NODE_DEBUG` disables the adapter to avoid exposing credentials or traces in logs. Changing credentials or destination requires a fresh OpenCode server process because the tracing client is cached for the process lifetime. Use `opencode plugin list` to check whether the adapter is loaded, and inspect Langfuse to confirm traces actually arrive.

Only use the HTTPS Tailscale hostname of a server you trust, with verified TLS, grants, and exposure. A `.ts.net` suffix by itself is not proof of private reachability. The local installer validates the key without using HTTP proxies or following redirects.

## Older project-scoped launcher

The original `make connect-opencode PROJECT=/absolute/path` and `make run-opencode PROJECT=/absolute/path` commands remain available. They configure and run the official plugin for one project with its own credentials. The launcher disables the global adapter in that standalone process to avoid duplicate instrumentation; it still enables its **project-specific** plugin. If a project already has `.opencode/opencode.json(c)`, the connect command preserves it and requires the following entry in the existing `plugins` array:

```json
"{env:AI_OBSERVATORY_PLUGIN}"
```

`make env-opencode PROJECT=/absolute/path` prints the protected project credential file path, not the credentials. Avoid sourcing it into a long-lived shell. If manually using the official plugin, set both `LANGFUSE_BASE_URL` and `LANGFUSE_BASEURL` to the intended endpoint: unlike the global adapter, the unmodified upstream plugin can fall back to Langfuse Cloud.

## Development checks

`make check` installs and prepares the integrity-pinned plugin without running npm lifecycle scripts, then runs the Python and Node tests. `make smoke-opencode-global` and `make smoke-opencode-shared` additionally need OpenCode 2 and use isolated configurations, a fake model and local collectors; they do not connect to the real Langfuse instance or invoke a paid model.

Official references: [plugin v0.7.0](https://github.com/langfuse/opencode-observability-plugin/tree/v0.7.0) and [OpenCode plugins](https://opencode.ai/v2/docs/plugins).
