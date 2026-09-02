# Persistent Caption Assistant Sidebar Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Open and operate the multi-plugin assistant sidebar without navigating away from RoomStudio or stopping captured audio.

**Architecture:** Keep RoomStudio as the media owner. Add controlled activity/sidebar UI with permanently mounted panels, embed the existing plugin workspace hook and renderer, and make auxiliary page links open separately. Use pure reducer tests plus React DOM interaction tests with fake LiveKit/API boundaries.

**Tech Stack:** Next.js 16, React 19, TypeScript, Vitest, jsdom, existing LiveKit client.

---

No valid Git repository is available; preserve current backend delivery fixes and use stage records instead of commits/worktrees. User approved this layout and previously requested automatic phased execution; implement in the current task, no new tasks/subagents. Do not restart backend or invoke real/private model tasks.

## Task 1: Test environment and sidebar state

**Files:** `frontend/package.json`, `frontend/pnpm-lock.yaml`, `frontend/vitest.config.ts`, `frontend/lib/studio-sidebar-state.ts`, `frontend/lib/studio-sidebar-state.test.ts`.

1. Add exact development-only jsdom dependency using pnpm; keep production dependencies unchanged. Configure alias resolution and automatic JSX for component tests; default tests remain node environment.
2. Write failing tests for `createStudioSidebarState()`, `reduceStudioSidebar(state, action)`, `clampSidebarWidth(width)`: rooms/default expanded, toggle active collapses, switch opens, explicit open is idempotent, widths clamped and invalid values safe.
3. Run `pnpm exec vitest run lib/studio-sidebar-state.test.ts --reporter=dot` and confirm missing implementation failure.
4. Implement minimal pure functions. Run again and expect pass.

## Task 2: Stable sidebar integration

**Files:** `frontend/components/assistants/studio-sidebar.tsx`, `frontend/components/assistants/room-assistant-sidebar.tsx`, `frontend/components/assistants/room-assistant-launcher.tsx`, `frontend/components/room-studio.tsx`, `frontend/app/globals.css`.

1. Implement activity buttons, permanently mounted hidden panels, pointer/keyboard resizing, accessible panel labels, collapse control, and scoped responsive styles.
2. Embed a generic assistant sidebar using `useMediaAssistantWorkspace`, `buildAssistantCatalog`, `AssistantCatalog` and `AssistantDetail`. Keep one data hook mounted even when the panel is hidden, follow only `activeRun.id`, and retain plugin/input state across UI toggles.
3. Replace RoomStudio's navigation anchor with an explicit open-sidebar button. Replace left Room aside with sidebar slots while leaving center/right media subtree and cleanup effect mounted. Remove the duplicated right-side assistant loader.
4. Add component tests proving state changes retain child node identity and hook instance.

## Task 3: Navigation safety and real media-lifecycle regression

**Files:** `frontend/components/assistants/assistant-catalog.tsx`, `frontend/components/assistants/assistant-detail.tsx`, `frontend/components/plugins/plugin-document-shelf.tsx`, `frontend/components/room-studio.test.tsx`.

1. Add optional separate-page link target for embedded catalog/detail; use `_blank` and `noopener noreferrer` for management/history/download links reachable from the sidebar.
2. Before integrating, write a DOM test that mounts actual RoomStudio with fake Room/audio tracks and API responses. Start tab audio, then click the assistant entry: old code must fail the no-navigation/side-panel assertion.
3. After integration, click all activity toggles, switch two synthetic plugins, resize and verify `stop`, `unpublishTrack`, `disconnect` remain uncalled; assert the caption DOM node stays the same. Unmount and assert original cleanup still runs.
4. Test pending/no Session, generic plugin errors, separate-tab links and per-plugin values. No tests contact local production API or external models.

## Task 4: Final verification and records

**Files:** `README.md`, `docs/plugin-operations.md`, `docs/stage-records.md`.

1. Run `pnpm exec vitest run --reporter=dot` and repeat media lifecycle/component tests five times.
2. Run `pnpm typecheck` then `pnpm build`; all routes including standalone `/assistants` remain available.
3. Review mounted ownership, focus/keyboard/resize behavior and CSS narrow-screen rules. Do not claim actual browser capture or visual checks without evidence.
4. Record exact counts and implementation scope. Explicitly retain pending backend restart/history replay authorization; no package version bump or backend changes required for this sidebar.
