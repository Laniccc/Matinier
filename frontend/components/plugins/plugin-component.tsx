"use client";

import type { PluginUIComponent, PluginUITone } from "@/types/plugin-ui";
import type { ReactNode } from "react";

function safeLinks(markdown: string): ReactNode[] {
  const nodes: ReactNode[] = []; let offset = 0;
  for (const match of markdown.matchAll(/\[([^\]\n]+)\]\((https:\/\/[^\s)]+)\)/g)) {
    try {
      const url = new URL(match[2]);
      if (url.protocol !== "https:" || !url.hostname || url.username || url.password) continue;
      nodes.push(markdown.slice(offset, match.index));
      nodes.push(<a key={match.index} href={url.href} target="_blank" rel="noopener noreferrer">{match[1]}</a>);
      offset = match.index! + match[0].length;
    } catch { /* Unsafe or malformed destinations remain escaped plain text. */ }
  }
  nodes.push(markdown.slice(offset)); return nodes;
}

interface PluginComponentProps {
  component: PluginUIComponent;
  values: Record<string, unknown>;
  onValueChange: (name: string, value: unknown) => void;
  onAction: (actionId: string) => void;
  disabled?: boolean;
}

function toneClass(tone: PluginUITone | "primary"): string {
  return `pluginTone pluginTone-${tone}`;
}

function formatTime(milliseconds: number): string {
  const total = Math.floor(milliseconds / 1_000);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

export function PluginComponent({
  component,
  values,
  onValueChange,
  onAction,
  disabled = false,
}: PluginComponentProps) {
  const children = (items: PluginUIComponent[]) =>
    items.map((item) => (
      <PluginComponent
        key={item.id}
        component={item}
        values={values}
        onValueChange={onValueChange}
        onAction={onAction}
        disabled={disabled}
      />
    ));

  switch (component.type) {
    case "text":
      return <p className="pluginText">{component.text}</p>;
    case "safe_markdown":
      return <p className="pluginMarkdown">{safeLinks(component.markdown)}</p>;
    case "card":
      return (
        <article className="pluginCard">
          {component.title ? <h4>{component.title}</h4> : null}
          {children(component.children)}
        </article>
      );
    case "section":
      return (
        <section className="pluginSection">
          <h4>{component.title}</h4>
          {children(component.children)}
        </section>
      );
    case "tabs":
      return (
        <div className="pluginTabs">
          {component.tabs.map((tab, index) => (
            <details key={tab.id} open={index === 0}>
              <summary>{tab.label}</summary>
              {children(tab.children)}
            </details>
          ))}
        </div>
      );
    case "list":
      return (
        <ul className="pluginList">
          {component.items.map((item) => (
            <li key={item.id}>
              <strong>{item.primary}</strong>
              {item.secondary ? <small>{item.secondary}</small> : null}
            </li>
          ))}
        </ul>
      );
    case "table":
      return (
        <div className="pluginTableWrap">
          <table className="pluginTable">
            <thead>
              <tr>{component.columns.map((column) => <th key={column.id}>{column.label}</th>)}</tr>
            </thead>
            <tbody>
              {component.rows.map((row, index) => (
                <tr key={index}>
                  {component.columns.map((column) => <td key={column.id}>{row[column.id] ?? ""}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    case "timeline":
      return (
        <ol className="pluginTimeline">
          {component.items.map((item) => (
            <li key={item.id}>
              <time>{formatTime(item.time_ms)}</time>
              <div><strong>{item.title}</strong>{item.body ? <p>{item.body}</p> : null}</div>
            </li>
          ))}
        </ol>
      );
    case "media_anchor":
      return <span className="pluginMediaAnchor">{formatTime(component.media_time_ms)} · {component.label}</span>;
    case "badge":
      return <span className={toneClass(component.tone)}>{component.text}</span>;
    case "metric":
      return (
        <div className="pluginMetric">
          <small>{component.label}</small><strong>{component.value}</strong>
          {component.detail ? <span>{component.detail}</span> : null}
        </div>
      );
    case "progress":
      return (
        <label className="pluginProgress">
          <span>{component.label} · {component.value}%</span>
          <progress value={component.value} max={100} />
        </label>
      );
    case "input":
      return (
        <label className="pluginField">
          <span>{component.label}</span>
          <input
            value={typeof values[component.name] === "string" ? String(values[component.name]) : ""}
            placeholder={component.placeholder}
            onChange={(event) => onValueChange(component.name, event.target.value)}
            disabled={disabled}
          />
        </label>
      );
    case "textarea":
      return (
        <label className="pluginField">
          <span>{component.label}</span>
          <textarea
            rows={component.rows}
            value={typeof values[component.name] === "string" ? String(values[component.name]) : ""}
            placeholder={component.placeholder}
            onChange={(event) => onValueChange(component.name, event.target.value)}
            disabled={disabled}
          />
        </label>
      );
    case "select":
      return (
        <label className="pluginField">
          <span>{component.label}</span>
          <select
            value={typeof values[component.name] === "string" ? String(values[component.name]) : ""}
            onChange={(event) => onValueChange(component.name, event.target.value)}
            disabled={disabled}
          >
            <option value="">请选择</option>
            {component.options.map((option) => <option key={option.id} value={option.value}>{option.label}</option>)}
          </select>
        </label>
      );
    case "checkbox":
      return (
        <label className="pluginCheckbox">
          <input
            type="checkbox"
            checked={typeof values[component.name] === "boolean" ? Boolean(values[component.name]) : component.checked}
            onChange={(event) => onValueChange(component.name, event.target.checked)}
            disabled={disabled}
          />
          <span>{component.label}</span>
        </label>
      );
    case "button":
      return (
        <button
          type="button"
          className={toneClass(component.tone)}
          onClick={() => onAction(component.action_id)}
          disabled={disabled}
        >
          {component.label}
        </button>
      );
    case "confirmation":
      return (
        <details className="pluginConfirmation">
          <summary>{component.title}</summary>
          <p>{component.body}</p>
          <button type="button" className="pluginTone pluginTone-danger" onClick={() => onAction(component.confirm_action_id)} disabled={disabled}>确认</button>
        </details>
      );
    case "empty_state":
      return <div className="pluginEmpty"><strong>{component.title}</strong>{component.message ? <p>{component.message}</p> : null}</div>;
    case "error_state":
      return <div className="pluginError" role="alert"><strong>{component.title}</strong><p>{component.message}</p></div>;
  }
}
