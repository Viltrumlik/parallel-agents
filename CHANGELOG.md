# Changelog

## 0.1.0 — 2026-11-06

First release.

- `pa` CLI: `claim`, `release`, `check`, `status`, `renew`, `note`, `log`, `gc`, `evict`, with `--json` output.
- Claims on files, folders, globs and named resources (`@migrations`, `@deps`, `@port:3000`).
- Atomic claiming under a file lock; all-or-nothing multi-path claims.
- Conservative overlap detection: case-insensitive paths, resolved symlinks, `**` globs that also match zero folders, and folders that cover files not created yet.
- Claims expire after 30 minutes without activity; any claim attempt refreshes all of an agent's claims.
- Claiming a path already covered by one of your own claims records nothing new, so holding a folder and editing the files in it keeps the board to one line.
- Exit codes: `0` ok, `1` error, `2` usage error (nothing claimed), `3` blocked or nothing held.
- State stored in the git common directory: never committed, shared across worktrees, with warnings for overlaps in other worktrees.
- Agent Skill (`SKILL.md`) for Claude Code, Codex, Cursor, Gemini CLI and other agents; Claude Code plugin marketplace manifest.
- Claude Code `PreToolUse` hook (`pa_hook.py`): claims the path before `Edit`, `Write`, `MultiEdit` and `NotebookEdit`, and denies the edit when another agent holds it, so the protocol holds without the agent reading the skill. Each session is its own agent, named after its session id. Configurable with `PA_AS`, `PA_HOOK_TTL`, `PA_HOOK_MODE` and `PA_HOOK_DISABLE`; fails open on anything unexpected.
