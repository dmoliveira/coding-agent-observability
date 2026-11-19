# OpenCode integration

`make connect-opencode` sets up the official Langfuse OpenCode plugin for one project and verifies its API key against an explicitly local or HTTPS Tailscale endpoint. The credentials file is specific to the absolute project path and is outside the project. Moving a project changes that path; create a new configuration for its new location.

`make run-opencode PROJECT=/absolute/path` loads those credentials and starts `opencode --standalone`. When the plugin environment variable is unset, the generated entry is inactive. Restart OpenCode after changing a tracing destination; the official plugin's telemetry provider is process-wide.

## Existing project configuration

The automated command will not overwrite an existing `.opencode/opencode.json` or `.jsonc`. For OpenCode 2, manually add this entry to its existing `plugins` array while preserving the rest of the file:

```json
"{env:AI_OBSERVATORY_PLUGIN}"
```

Then run `make connect-opencode PROJECT=/absolute/path` to verify and save credentials without rewriting that configuration, followed by `make run-opencode PROJECT=/absolute/path`. Ensure the entry is actually in the `plugins` array, not just a comment.

If configuring the environment manually instead, set **both** `LANGFUSE_BASE_URL` and `LANGFUSE_BASEURL` explicitly to the intended local or Tailscale URL, alongside `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and `AI_OBSERVATORY_PLUGIN=@langfuse/opencode-observability-plugin@0.7.0`. Do not commit these values. The official plugin can default to Langfuse Cloud if credentials are provided without a base URL.

For a project configured by `make connect-opencode`, `make env-opencode PROJECT=/absolute/path` prints the protected credentials file path, without printing the credentials. If you source it yourself, remember that the environment remains active in that shell and its children; prefer the standalone launcher.

The integration deliberately does not modify `my_opencode`, global OpenCode settings, or an already running service. A standalone server can still load existing global plugins and use the normal OpenCode data directory; it is not a separate account or database. Do not assume an existing `my_opencode` gateway, redaction rule or guard protects traces in OpenCode 2. Review any other configured telemetry exporters and proxy environment variables separately. A future collaboration can make client installation and integration more convenient without weakening per-project opt-in.

Official references: [plugin v0.7.0](https://github.com/langfuse/opencode-observability-plugin/tree/v0.7.0) and [OpenCode plugins](https://opencode.ai/v2/docs/plugins).
