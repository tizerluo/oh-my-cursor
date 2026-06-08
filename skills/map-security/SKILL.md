---
name: map-security
description: >-
  安全审计 workflow。用于漏洞扫描、安全评估、渗透测试。
  触发词：security, audit, 安全, 漏洞, XSS, injection, 权限, map-security.
  Requires MAP .review/ infrastructure. Merge gate conditional on code fixes.
---

# MAP Security — Security Audit

## Workflow ID

`map-security`

## Subagent mapping

| Role | Task subagent_type | Focus |
|------|-------------------|--------|
| hunter-injection | `explore` | SQL/XSS/command injection |
| hunter-auth | `explore` | authZ/authN, privilege escalation |
| hunter-data | `explore` | data leak, serialization, crypto |
| poc-exploit | `coder` | PoC in `.review/poc/` only |

**Spawn convention:** set `config.roles.poc-exploit` to the actual Task `subagent_type` (usually `coder`).
The `subagentStart` hook resolves `logical_role` from `config.roles` and stores it in
`.review/roles/{subagent_id}.json` so Write/Delete and Shell gates apply the PoC sandbox.
| poc-fix | `coder` | fix + verify (may trigger merge gate) |

## Pipeline

```
config-confirmed → scope → hunt → triage → poc → report
```

1. **Scope** — validate `config.scope_paths` (inside repo, no traversal)
2. **Hunt** — 3 hunters in parallel
3. **Triage** — dedupe by fingerprint: `asset + vuln_class + sink + exploitability`
4. **PoC** — verify High+ findings; artifacts in `.review/poc/` (gitignored)
5. **Report** — `.review/reports/security-{date}.md`

## PoC sandbox

- Writes: `.review/poc/` only for poc-exploit
- Shell: deny dangerous patterns (rm -rf, curl, wget outside sandbox)
- Retention: session lifetime

## Merge gate

- Default: disabled (report-only)
- If poc-fix modifies code outside `.review/poc/` and `.review/reports/`:
  - `merge_gate_required` forced true
  - Require hotfix tier + ≥1 reviewer-* + verdict with `workflow: map-security`

## security-queue.json (optional second hunt)

Unverified High+ findings → `.review/security-queue.json` (schema: `hooks/schemas/security-queue.schema.json` under `$OMC_ROOT`).

Helper appends deduped findings:

```python
# review_gate.append_unverified_findings_to_security_queue(git_root, findings, session_id=...)
```

Fingerprint: `asset + vuln_class + sink + exploitability` (see `security_fingerprint()`).

Stop hook at `phase=report` emits followup with finding summaries.

## GitHub Issues (opt-in)

AskQuestion before creating issues. Templates:

- [templates/issue-body.md](templates/issue-body.md)
- [templates/issue-private-disclosure.md](templates/issue-private-disclosure.md)

Dedupe by triage fingerprint. Use private disclosure for sensitive findings.

## Config example

See multi-agent-pr SKILL §0.5 for `map-security` config shape.
