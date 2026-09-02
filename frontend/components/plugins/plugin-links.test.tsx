// @vitest-environment jsdom
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { PluginComponent } from "./plugin-component";
it("renders HTTPS result links without enabling HTML, scripts or credential URLs", () => {
  const html = renderToStaticMarkup(<PluginComponent component={{ type: "safe_markdown", id: "link", markdown: '[Linear](https://linear.app/team/issue/ABC-1) [bad](javascript:alert) [credentials](https://user:secret@example.com/) <img src=x onerror=alert(1)>' }}
    values={{}} onValueChange={() => {}} onAction={() => {}} />);
  expect(html).toContain('href="https://linear.app/team/issue/ABC-1"');
  expect(html).toContain('rel="noopener noreferrer"');
  expect(html).not.toContain('href="javascript:'); expect(html).not.toContain('href="https://user:');
  expect(html).not.toContain('<img');
});
