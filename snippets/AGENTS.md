## Parallel agents

Other coding agents may be working in this repository at the same time. Follow the parallel-agents protocol:

1. At the start, pick a unique name such as `claude-7f2c` or `codex-a91e`, run `pa status`, and read `pa log`.
2. **Before editing any file**, claim it: `pa claim <files or folders> --as <name> --why "<what you are doing>"`. Claim `@migrations`, `@deps` or `@port:<n>` before running migrations, changing dependencies or starting a server.
3. If the claim is blocked (exit code 3), do not edit those paths. Work on something else, or ask the user. Exit code 2 means the command was wrong and nothing was claimed: fix it before editing.
4. Before each new batch of edits, run `pa claim` again for those paths. It is free for paths you hold and keeps your claims from expiring.
5. When you finish, leave a note with `pa note "<what changed, what is left>" --as <name>`, then release with `pa release --as <name>`.

If `pa` is not on the PATH, run `python3 <path to the parallel-agents skill>/scripts/pa.py` instead.
