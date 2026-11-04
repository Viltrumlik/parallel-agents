# Changelog

## 0.1.0 — 2026-11-04

First release.

- `pa` CLI: `claim`, `release`, `check`, `status`, `renew`, `note`, `log`, `gc`, `evict`, with `--json` output.
- Claims on files, folders, globs and named resources (`@migrations`, `@deps`, `@port:3000`).
- Atomic claiming under a file lock; all-or-nothing multi-path claims.
- Conservative overlap detection: case-insensitive paths, resolved symlinks, `**` globs that also match zero folders, and folders that cover files not created yet.
- Claims expire after 30 minutes without activity; any claim attempt refreshes all of an agent's claims.
- Exit codes: `0` ok, `1` error, `2` usage error (nothing claimed), `3` blocked or nothing held.
- State stored in the git common directory: never committed, shared across worktrees, with warnings for overlaps in other worktrees.
- Agent Skill (`SKILL.md`) for Claude Code, Codex, Cursor, Gemini CLI and other agents; Claude Code plugin marketplace manifest.
