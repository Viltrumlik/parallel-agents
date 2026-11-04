# Contributing

Thanks for helping make parallel agents less chaotic.

## Run the tests

```bash
python3 -m unittest discover -s tests -v
```

The tests drive the real CLI in throwaway git repositories, including a race where 16 agents claim the same file at once. They need Python 3.8+ and git, nothing else.

## Principles

1. **Zero dependencies, one file.** `pa.py` must run with a stock Python on macOS, Linux and Windows. Agents copy it around; it can't need `pip install`.
2. **Never block forever.** Every claim expires. Anything that could leave a repo stuck is a bug.
3. **Refuse loudly, explain clearly.** A blocked claim says who holds what and why, so the agent can decide what to do next.
4. **The skill is the product.** `SKILL.md` is what agents actually read. Keep it short, concrete and imperative. If you change behavior, update it in the same PR.
5. **State stays out of the working tree.** It lives in the git directory, never in files the user might commit.

## Good first contributions

- Hooks that enforce claims in a specific agent (Claude Code `PreToolUse`, Cursor hooks, Codex)
- Reports of how a particular agent follows (or ignores) the skill, with a transcript
- `pa watch`: a live view of claims in the terminal

## Regenerating the demo GIF

```bash
pip install pillow
python3 demo/make_gif.py
```

Every line in the GIF is real output from `pa.py`.
