# Diagnostic plugin

This deliberately small plugin exercises the stable Host API without third-party runtime dependencies. It negotiates protocol identity, opens multiple logical media sessions, acknowledges event batches, persists a small cursor/count state, publishes a host-rendered diagnostic panel, responds to `refresh`, and shuts down cleanly.

`plugin.json` contains a placeholder image digest. Do not ZIP this directory directly. Use `backend/scripts/package_diagnostic_plugin.py`; it builds the image, writes the actual digest into a copied manifest, signs the immutable package material, and creates the installable `.plugin.zip` outside this source directory.

The plugin communicates through newline-delimited JSON-RPC 2.0 on stdin/stdout. Diagnostic messages must go to stderr so stdout remains protocol-only.
