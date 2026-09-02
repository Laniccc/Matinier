import { describe, expect, it } from "vitest";

import { parsePluginUIView } from "./plugin-ui-schema";

function validView(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: 1,
    surface: "panel",
    view_id: "main",
    view_version: 2,
    root: {
      id: "section-1",
      type: "section",
      title: "Assistant",
      children: [
        { id: "text-1", type: "text", text: "Ready" },
        { id: "button-1", type: "button", label: "Run", action_id: "run" },
      ],
    },
    actions: [{ id: "run", kind: "command", command: "analyze" }],
    ...overrides,
  };
}

describe("parsePluginUIView", () => {
  it("parses unknown JSON into the closed discriminated union", () => {
    const result = parsePluginUIView(validView(), {
      allowedCommands: new Set(["analyze"]),
    });

    expect(result.ok).toBe(true);
    expect(result.view.root.type).toBe("section");
    if (result.view.root.type === "section") {
      expect(result.view.root.children[0]?.type).toBe("text");
    }
  });

  it.each([
    { id: "unknown-1", type: "webview", url: "https://evil.example" },
    { id: "prompt-1", type: "permission_prompt", permission: "network.fetch" },
    { id: "script-1", type: "script", source: "alert(1)" },
  ])("turns unknown and fake privileged components into a safe error view", (root) => {
    const result = parsePluginUIView(validView({ root, actions: [] }), {
      allowedCommands: new Set(),
    });

    expect(result.ok).toBe(false);
    expect(result.view.root).toEqual({
      id: "plugin-ui-error",
      type: "error_state",
      title: "Plugin view unavailable",
      message: "This plugin view could not be displayed safely.",
    });
    expect(JSON.stringify(result.view)).not.toContain("evil.example");
  });

  it.each([
    "<img src=x onerror=alert(1)>",
    "[click](javascript:alert(1))",
    "[file](file:///etc/passwd)",
  ])("rejects unsafe markdown links and raw HTML", (markdown) => {
    const result = parsePluginUIView(
      validView({
        root: { id: "markdown-1", type: "safe_markdown", markdown },
        actions: [],
      }),
      { allowedCommands: new Set() },
    );
    expect(result.ok).toBe(false);
  });

  it("rejects oversized, over-nested, duplicate, and unstable component trees", () => {
    let nested: Record<string, unknown> = {
      id: "leaf-1",
      type: "text",
      text: "leaf",
    };
    for (let depth = 0; depth < 9; depth += 1) {
      nested = {
        id: `section-${depth}`,
        type: "section",
        title: "Nested",
        children: [nested],
      };
    }
    expect(
      parsePluginUIView(validView({ root: nested, actions: [] }), {
        allowedCommands: new Set(),
      }).ok,
    ).toBe(false);

    const oversized = validView({
      root: { id: "text-1", type: "text", text: "x".repeat(40_000) },
      actions: [],
    });
    expect(
      parsePluginUIView(oversized, { allowedCommands: new Set() }).ok,
    ).toBe(false);

    const duplicate = validView({
      root: {
        id: "section-1",
        type: "section",
        title: "Duplicate",
        children: [
          { id: "same", type: "text", text: "one" },
          { id: "same", type: "text", text: "two" },
        ],
      },
      actions: [],
    });
    expect(
      parsePluginUIView(duplicate, { allowedCommands: new Set() }).ok,
    ).toBe(false);

    const missingId = validView({
      root: { type: "text", text: "no stable id" },
      actions: [],
    });
    expect(
      parsePluginUIView(missingId, { allowedCommands: new Set() }).ok,
    ).toBe(false);
  });

  it("rejects stale versions and undeclared action commands", () => {
    const previous = parsePluginUIView(validView(), {
      allowedCommands: new Set(["analyze"]),
    }).view;
    const stale = parsePluginUIView(validView(), {
      allowedCommands: new Set(["analyze"]),
      previous,
    });
    expect(stale.ok).toBe(false);
    if (stale.ok) throw new Error("expected stale plugin view to be rejected");
    expect(stale.errorCode).toBe("stale_version");

    const undeclared = parsePluginUIView(validView(), {
      allowedCommands: new Set(["summarize"]),
    });
    expect(undeclared.ok).toBe(false);
  });

  it("applies the smaller overlay allowlist", () => {
    const result = parsePluginUIView(
      validView({
        surface: "overlay",
        root: { id: "input-1", type: "input", name: "title", label: "Title" },
        actions: [],
      }),
      { allowedCommands: new Set() },
    );
    expect(result.ok).toBe(false);
  });

  it("keeps the safe fallback URL-free when a plugin injects download-like fields", () => {
    const result = parsePluginUIView(
      validView({
        root: {
          id: "download-1",
          type: "text",
          text: "Download",
          download_url: "javascript:alert(1)",
          markdown_path: "file:///etc/passwd",
        },
        actions: [],
      }),
      { allowedCommands: new Set() },
    );
    expect(result.ok).toBe(false);
    expect(JSON.stringify(result.view)).not.toContain("javascript:");
    expect(JSON.stringify(result.view)).not.toContain("file://");
  });
});
