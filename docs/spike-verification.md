# MAP Hook Spikes

Run manually or observe during Cursor sessions. Logs under this directory.

| Spike | Script | Log | Validates |
|-------|--------|-----|-----------|
| stop hook | `spike_stop_hook.py` | `stop-hook.log` | followup_message + stop-check delegation |
| sessionStart | `spike_session_start.py` | `session-start.log` | additional_context from session-resume |
| subagentStart | `spike_subagent_start.py` | `subagent-start.log` | payload fields + set-role delegation |

## Usage

```bash
export OMC_ROOT=/path/to/oh-my-cursor
echo '{}' | python3 "$OMC_ROOT/hooks/spikes/spike_stop_hook.py"
echo '{"workspace_roots":["/path/to/repo"]}' | python3 "$OMC_ROOT/hooks/spikes/spike_session_start.py"
```

Replace hooks.json commands temporarily with spike wrappers to capture live payloads.

## Live Spike Progress

| Step | Status | Notes |
|------|--------|-------|
| A — spike hooks wired | **done** | |
| B — sessionStart | **done** | |
| C — subagentStart | **done** | |
| D — stop followup | **done** | Live UI followup confirmed |
| E — subagentStop observe | **done** | Path A (3.7.19 reliable) |
| F — restore + README results | **done** | `hooks.json` restored; spike `.review/` + markers cleaned |

**Live Spike complete:** 2026-06-08, Cursor **3.7.19**. Reload Cursor if hooks were cached during spike.

**After Step A:** Reload Cursor window (Cmd+Shift+P → "Developer: Reload Window") so hooks reload.

## Results (update after manual verification)

| Hook | Cursor | Date | Result | Notes |
|------|--------|------|--------|-------|
| stop | 3.7.19 | 2026-06-08 | **pass** | Live UI followup confirmed: fix-queue P0 message injected after stop |
| sessionStart | 3.7.19 | 2026-06-08 | **pass** | Live log keys: conversation_id, hook_event_name, workspace_roots; hint: draft spec → map-hyperplan |
| subagentStart | 3.7.19 | 2026-06-08 | **pass** | Fields: `subagent_type`, `subagent_id`; role → `.review/roles/tool_*.json` with `logical_role: coder` |
| subagentStop | 3.7.19 | 2026-06-08 | **reliable** | `.review-session/main/<HEAD>/reviewer-grok-*.json` with HMAC seal; DEF-06 → **Path A** |

### Stop followup loop note

If `phase=synthesis-complete` and fix-queue has pending P0/P1, **every stop** may re-inject the followup (loop_limit=5). Clear fix-queue or change phase after Step D verification.

### DEF-06 decision (Phase 4)

**Path A** — subagentStop reliable on Cursor 3.7.19; prefer marker-based critic tracking with subagentStop fallback documented only.

### Step B — sessionStart (action required)

`sessionStart` runs on a **new Agent conversation**, not when reopening Cursor on the same thread.

1. In `issue-to-pr`, click **New Chat** (new Agent session)
2. Optional first message: `hello` (any prompt)
3. Tell the agent to check: `$OMC_ROOT/hooks/spikes/session-start.log` last line
4. Look for routing hint in injected context (draft spec: `.specs/spike-test.md`)

## Results (legacy)

- stop followup_message: _pending live Cursor test_
- sessionStart additional_context: _pending live Cursor test_
- subagentStart dispatch: _pending live Cursor test_; subagentStop known unreliable on Cursor 3.7.12


## Appendix: Cursor 3.15.6 payload shapes (issue #22 / v1.3.1)

**Date:** 2026-08-10 · **Cursor:** 3.15.6 · **Source:** live `cursor.hooks.*.log` INPUT blocks (oh-my-cursor workspace).

### Field presence

| Field | subagentStart | subagentStop | preToolUse (parent) | preToolUse (subagent) |
|-------|---------------|--------------|---------------------|------------------------|
| `subagent_id` | present (`call-…`) | present | **absent** | **absent** |
| `subagent_type` | present | present | absent | absent |
| `conversation_id` | **parent** UUID | **parent** UUID | parent UUID | **agent** UUID (≠ parent) |
| `session_id` | parent | parent | parent | **same as agent cid** |
| `parent_conversation_id` | = parent | = parent | n/a | n/a |
| `transcript_path` | parent root jsonl | parent root jsonl | parent root jsonl | **null** |
| `agent_transcript_path` | null | null | absent | absent |
| `model` | present | present | present | present |
| `is_parallel_worker` | present (often false) | n/a | n/a | n/a |

### Join-key conclusion

- There is **no** payload field that joins `subagentStart.subagent_id` to subagent `preToolUse.conversation_id`.
- Parent vs subagent `preToolUse` **are** distinguishable: parent has root `transcript_path`; subagent has `transcript_path=null` and a distinct agent `conversation_id`.
- **Stable join for permissions:** keep `roles/{slug(subagent_id)}.json` with `active` + `model`; on preToolUse without `subagent_id`, scan active roles and resolve when **model uniquely matches** (or a single active role / single logical_role). Conflicts → fail-closed.
- **Forbidden:** single-value `by-conversation/{parent_cid}` sidecar (would alias Commander to last child).

### Parallel safety

Multiple active roles with the same model or conflicting write capabilities must fail-closed. MAP Large may spawn parallel reviewers (foreground ≠ exclusive).

