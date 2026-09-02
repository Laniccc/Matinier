# Matinier plugin SDK v1

This directory is the public compatibility boundary for locally installed, third-party assistants. A plugin can target meetings, live streams, ordinary audio, or video without changing the host: each legacy caption session is bridged to a generic `MediaSession`, and plugins receive scoped media events through a versioned JSON-RPC protocol.

## Security and process model

- Plugin code never runs in the API or browser process. One OCI container is supervised per enabled plugin version.
- Containers start with no network, a read-only root filesystem, all Linux capabilities dropped, `no-new-privileges`, bounded CPU/memory/PIDs, and a small temporary filesystem.
- A random `session_scope` is issued for every plugin/MediaSession binding. It is invalidated after a restart and cannot be used by another plugin.
- Plugins cannot render HTML, JavaScript, webviews, or permission prompts. They publish the closed `ui.publish` document model; the host validates and renders it.
- Network, state, media, UI, and future external effects are host-owned capabilities. Install-time permission acceptance and short-lived grants are enforced before adapters run.
- stdout is newline-delimited JSON-RPC only. Write logs to stderr and never put secrets in events, state, errors, or UI documents.

## Package layout

An installable package is a ZIP with the media type `application/vnd.matinier.plugin+zip`:

```text
plugin.json
image.tar
signature.json
assets/...        # optional, immutable signed assets
```

The host rejects absolute/traversal paths, links, duplicate names, ZIP bombs, oversized archives, image digest mismatches, invalid signatures, incompatible Host API ranges, and permission changes between inspection and confirmation. Installation is deliberately two-phase in `/plugins`: inspect the package, review every requested permission and publisher fingerprint, then confirm.

## Protocol lifecycle

1. The host requests `plugin.initialize`; return the exact plugin identity, protocol version, and host API version.
2. The host requests `session.open` with `media_session_id`, a fresh opaque `session_scope`, and the last acknowledged event sequence.
3. Event delivery uses `event.batch`; acknowledge only the highest sequence durably processed. After a crash, delivery resumes after the persisted acknowledgment.
4. Call the host with `capability.invoke`. Always include the issued scope for session-bound work.
5. Host UI actions arrive through `command.execute` with the expected view version. Reject stale or unknown commands.
6. Respond to `plugin.heartbeat`. Treat `session.close` and `plugin.shutdown` as notifications and finish promptly.

All messages use JSON-RPC 2.0, one UTF-8 JSON object per line. Public envelopes, manifests, media events, capability inputs/outputs, and UI views are published in [`schemas`](./schemas). `plugin-capabilities.schema.json` is the authoritative union of the bounded host capability contracts, including model invocation, Frozen Package delivery, and versioned document publication. These files are generated from the same Pydantic models enforced by the host; run the schema parity test after a contract change.

## Diagnostic example

[`examples/diagnostic`](./examples/diagnostic) is a dependency-free Python plugin that covers initialization, logical sessions, event acknowledgment, state calls, host-rendered UI, commands, heartbeat, and shutdown.

Create an unencrypted development Ed25519 key, then package it from the repository root:

```powershell
backend\.venv\Scripts\python.exe backend\scripts\package_diagnostic_plugin.py `
  --private-key C:\safe\diagnostic-development-key.pem `
  --output C:\safe\matinier-diagnostic-1.0.0.plugin.zip
```

Keep the private key outside the repository and plugin package. The script refuses to overwrite an output file or write the ZIP inside the diagnostic source directory.

## Course organizer example

[`examples/course-organizer`](./examples/course-organizer) implements
`com.matinier.course-organizer`. During playback it derives realtime knowledge
notes from Final transcript/translation events only. A user command publishes an
immutable interim document; a terminal session event publishes complete (or
partial-terminal after failure/cancellation) output from a Host-created Frozen
Package. Markdown and JSON exports are Host-owned and evidence-validated.

Its exact permissions are `state.get`, `state.put`, `ui.publish`, `model.invoke`,
`delivery.prepare`, `delivery.query`, and `document.publish`. It does not request
`network.fetch`; the container remains network-isolated and never receives model
credentials. Output defaults to the first observed translation language, falling
back to the source language, and each selected language has independent document
versions. Captured-tab coordinates are caption-audio offsets, not DOM/player seek
coordinates. See the example README for the signed packaging command.

## Meeting assistant example

[`examples/meeting-assistant`](./examples/meeting-assistant) implements
`com.matinier.meeting-assistant`. The network-isolated plugin owns only the
declarative panel, selection state, bounded polling, and Host capability calls.
The Host retains meeting data, model execution, intent issuance, confirmation,
Linear access, and read-only history. Installation and enablement never activate
analysis; activation is explicit and scoped to one media session.

Its manifest requests seven typed `meeting.*` capabilities plus `state.get`,
`state.put`, and `ui.publish`; it requests no direct network, model, database, or
generic external-write capability. Missing Linear configuration disables only
external execution. Host history remains readable after disable/uninstall, and
an old external action is never replayed merely because the plugin is enabled again.

## Dependency-free Python runtime

[`python/matinier_plugin`](./python/matinier_plugin) provides the reusable asyncio
transport used by Python plugins. It depends only on the Python standard library,
keeps stdout reserved for serialized one-line JSON-RPC, dispatches Host requests
concurrently, and correlates plugin-to-Host capability calls by unique request ID.

```python
import asyncio

from matinier_plugin import PluginRuntime


runtime = PluginRuntime()


async def heartbeat(_params):
    return {"ok": True}


async def event_batch(params):
    await runtime.capability(
        name="state.put",
        session_scope=params["session_scope"],
        input_value={
            "key": "cursor",
            "value": {"sequence": 42},
            "expected_version": 0,
        },
    )
    return {"acknowledged_sequence": 42}


runtime.register("plugin.heartbeat", heartbeat)
runtime.register("event.batch", event_batch)
asyncio.run(runtime.run())
```

Use `runtime.create_task()` for work that must continue after a short command
response. Owned tasks and pending Host calls are cancelled or failed when the
transport closes. Use `runtime.log()` for stderr diagnostics; never call `print()`
for plugin logs because stdout is the protocol channel.

To regenerate public schemas after an intentional host-contract update:

```powershell
cd backend
.venv\Scripts\python.exe -m app.plugins.sdk_schemas ..\plugin-sdk\schemas
```
