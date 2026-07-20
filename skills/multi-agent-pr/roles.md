# Model Reference

Available model slugs for subagent roles (as of 2026-07):

| Family | Slug | Typical Role |
|--------|------|--------------|
| Claude Opus 4.8 | `claude-opus-4-8-thinking-high` | Commander (parent) |
| Claude Opus 4.7 | `claude-opus-4-7-thinking-xhigh` | Heavy reasoning |
| Claude Opus 4.6 | `claude-opus-4-6-thinking-high` | Commander (alt) |
| Claude Sonnet 4.6 | `claude-4-6-sonnet-medium-thinking` | Lighter Commander |
| Composer 2.5 | `composer-2.5-fast` | Coder |
| GPT 5.5 | `gpt-5.5-medium` | Architect |
| GPT 5.3 Codex | `gpt-5.3-codex-high-fast` | Reviewer (quality) |
| Grok 4.5 | `grok-4.5` | Reviewer (correctness) |
| Gemini 3.1 Pro | `gemini-3.1-pro` | Reviewer (architecture) |
| Kimi K2.7 Code | `kimi-k2.7-code` | Tester-Writer |

## Subagent Types

| Type | Write Access | Use For |
|------|-------------|---------|
| `architect` | No | Spec/design review |
| `coder` | Yes | Implementation |
| `reviewer-grok` | No | Correctness review |
| `reviewer-codex` | No | Quality review |
| `reviewer-gemini` | No | Architecture review |
| `tester-writer` | Yes | Write tests |
| `generalPurpose` | Yes | Research, exploration |
| `explore` | No | Fast codebase search |

## Platform type vs MAP logical role

Cursor Task accepts **platform** subagent types only. MAP **logical roles** (merge-gate marker `type`, permission checks) may differ when reviewers are spawned via `generalPurpose`.

| MAP logical role | Platform `subagent_type` | `readonly` | Typical model |
|------------------|--------------------------|------------|---------------|
| `reviewer-grok` | `generalPurpose` | `true` | `grok-4.5` |
| `reviewer-codex` | `generalPurpose` | `true` | `gpt-5.3-codex-high-fast` |
| `reviewer-gemini` | `generalPurpose` | `true` | `gemini-3.1-pro` |
| `coder` | `coder` | — | `composer-2.5-fast` |
| `architect` | `architect` | — | `gpt-5.5-medium` |
| `tester-writer` | `tester-writer` | — | `kimi-k2.7-code` |
| `explore` | `explore` | — | (varies) |

Prompt must include `logical_role: reviewer-<engine>` (or `Reviewer-Grok` / etc.) so hooks infer the logical role at `subagentStart` and record `reviewer-*` markers at `subagentStop`.

## Prompt Templates

### Architect Prompt Skeleton

```
You are the Architect for PR N of project X.
Review the spec at `.specs/prN-xxx.md`.

Read these files: [list]

Score issues P0 (blocking) / P1 (severe) / P2 (advisory).

Focus on:
1. Runtime failures, data loss, security
2. Interface mismatches, backward compat
3. Race conditions, error handling
4. Missing validation

Return structured P0/P1/P2 list.
```

### Reviewer Prompt Skeleton

```
You are the [Focus] Reviewer for PR N.
The spec is at `.specs/prN-xxx.md`.

Read ALL of these files: [list]

Score issues P0/P1/P2 with:
- File + function
- Description
- Suggested fix

Focus on [correctness | quality | architecture].
```

### Tester Prompt Skeleton

```
You are the independent Tester for PR N on branch `feat/xxx`.
The spec is at `.specs/prN-xxx.md`.

Coder has already written these test files: [list]

Your job is to write COMPLEMENTARY tests that Coder would miss:

1. Integration tests (DO NOT mock the function under test's internal calls —
   let real code paths execute to verify cross-module contracts)
2. Boundary tests (empty input, None, missing config keys, type errors)
3. Failure paths (network errors, permission denied, timeouts, disk full)
4. Non-default config combinations

DO NOT duplicate Coder's existing tests.
DO NOT mock internal dependencies unless testing a specific mock scenario.

After writing tests, run: [test command]
All tests (existing + new) must pass. Commit with message:
  test(prN): add integration and boundary tests
```

### Coder Prompt Skeleton

```
You are the Coder for PR N on branch `feat/xxx`.
Implement all changes in `.specs/prN-xxx.md`.

Critical notes from Architect review:
- [list adjudicated fixes]

After implementing, run tests: `rtk python -m pytest tests/ -x -q`
All tests must pass. Commit with descriptive message.
```
