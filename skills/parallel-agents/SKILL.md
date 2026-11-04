---
name: parallel-agents
description: Coordinates several coding agents (Claude Code, Codex, Cursor, Gemini CLI, or several copies of one) working in the same repository at the same time, so they never edit the same files or run clashing commands. Use before editing files whenever other agents may be active in this checkout, when a .git/parallel-agents folder exists, when the user mentions parallel or multiple agents, before shared operations such as migrations, dependency installs or dev servers, and when finishing or handing off work.
license: MIT
---

# Parallel agents

Several agents may be editing this repository at once. Before you edit anything, **claim** it. If another agent already holds it, leave it alone. When you are done, leave a note and **release** it.

Everything goes through one small script that ships with this skill:

```bash
python3 <this skill's folder>/scripts/pa.py <command> ...
```

Below, `pa` means that command. If `pa` is on the PATH, use it directly. The state lives in the repository's git directory (`.git/parallel-agents/`, shared by every worktree and never committed), or in `.parallel-agents/` outside git. Never edit those files by hand.

## 1. Pick a name once

At the start of the session, choose a name that is unique to you, such as your tool plus four random characters: `claude-7f2c`, `codex-a91e`, `cursor-03bd`. Pass the same `--as <name>` on every command for the rest of the session.

## 2. Look, then claim before you edit

```bash
pa status
pa claim src/billing/ tests/test_billing.py --as claude-7f2c --why "add refund endpoint"
```

- **Claim before the first edit**, not after. Reading files never needs a claim.
- **Claim narrowly.** Claim the files or the one folder you will change, not the whole project. Folders (`src/billing/`) and globs (`src/**/*.py`) work too.
- **Claim shared operations as resources:** `@migrations` before creating or running database migrations, `@deps` before changing lockfiles or installing packages, `@port:3000` before starting a dev server on that port, `@db` before resetting a database.
- **Always give `--why`.** Other agents read it to decide what to do.
- If a task grows, claim the new files **before** touching them.

## 3. If the claim is blocked

`pa claim` exits with code **3** and prints who holds what, and why. Nothing was claimed.

1. **Do not edit those paths.** Do not delete or rewrite another agent's claim files.
2. Work on another part of the task that is free, then retry later. Or read `pa log` to see whether the holder said when it will finish.
3. Do not retry in a tight loop. Do other work between attempts.
4. If you cannot make progress, tell the user who holds the files and why, and ask what to do.

Only the user can override with `pa evict <agent>`. Run it only when the user explicitly tells you to.

A warning (exit 0) about **another worktree** means a different checkout is editing the same files. You may continue, but mention the expected merge conflict to the user.

Exit code **2** means the command itself was wrong (bad path, path outside the repo, missing `--as`, git not working). Nothing was claimed: fix the command and claim again before editing.

## 4. Keep claims alive

Claims expire after 30 minutes without activity, so a crashed agent never blocks the repo for long. You cannot watch a clock, so tie renewal to your work instead:

- **Before each new batch of edits, run `pa claim` again for the paths you are about to change.** Claiming what you already hold is free, refreshes all your claims, and tells you immediately if something went wrong. If it exits 3, stop and re-plan.
- If you expect a long task, claim with `--ttl 2h` up front.
- `pa renew --as <name>` refreshes everything you hold. It exits 3 with `NOTHING HELD` if your claims already expired: claim again before editing.

## 5. Finish: note, then release

```bash
pa note "refund endpoint done; migration 0042 added; tests for partial refunds still missing" --as claude-7f2c --paths src/billing/
pa release --as claude-7f2c
```

- Release as soon as you stop editing, even if the task continues elsewhere. Holding files you are not changing blocks others for nothing.
- Write the note for the next agent: what changed, what is unfinished, and what to watch out for.
- Read `pa log` when you start, to pick up notes other agents left.

## Command reference

| Command | Purpose |
|---|---|
| `pa status [--json]` | Who holds what right now |
| `pa claim PATH... --as N --why TEXT [--ttl 30m]` | Claim files, folders, globs or `@resources` (exit 3 if blocked) |
| `pa check PATH... [--as N]` | Exit 0 if free (or yours), 3 if another agent holds it |
| `pa release [PATH...] --as N` | Release some paths, or all of yours |
| `pa renew --as N [--ttl 1h]` | Keep your claims alive (exit 3 if you hold nothing) |
| `pa note TEXT --as N [--paths ...]` | Leave a handoff note |
| `pa log [-n 10]` | Read recent notes |
| `pa gc` | Remove expired claims |
| `pa evict AGENT` | Human override: drop an agent's claims |

Every command accepts `--json`. Exit codes: `0` ok, `1` error (e.g. disk or lock), `2` wrong command, nothing claimed, `3` blocked by another agent, or nothing held.
