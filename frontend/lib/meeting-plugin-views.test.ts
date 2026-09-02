import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { parsePluginUIView } from "./plugin-ui-schema";

const fixtures = JSON.parse(readFileSync(resolve(process.cwd(), "lib/__fixtures__/meeting-plugin-views.json"), "utf8")) as Record<string, unknown>;
const manifest = JSON.parse(readFileSync(resolve(process.cwd(), "../plugin-sdk/examples/meeting-assistant/plugin.json"), "utf8")) as { commands: string[] };

describe("meeting plugin shared Host/SDK fixtures", () => {
  it.each(Object.entries(fixtures))("parses the actual Python-generated %s view", (_name, raw) => {
    const result = parsePluginUIView(raw, { allowedCommands: new Set(manifest.commands) });
    expect(result.ok).toBe(true);
    expect(result.view.view_id).toBe("meeting-assistant");
    expect(result.view.actions.some(action => action.command === "apply_action")).toBe(false);
  });

  it("never promotes a plugin view to a trusted Host control", () => {
    const raw = { ...(fixtures.updated as object), source_kind: "host_history", trusted: true };
    expect(parsePluginUIView(raw, { allowedCommands: new Set(manifest.commands) }).ok).toBe(false);
  });
});
