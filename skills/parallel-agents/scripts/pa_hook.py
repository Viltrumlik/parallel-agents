#!/usr/bin/env python3
"""pa-hook - enforce parallel-agents claims from inside the agent.

Claude Code runs this as a PreToolUse hook. Before an agent writes to a file
the hook claims that path; if another agent already holds it, the edit is
denied and the agent is told who holds it and why. The protocol then holds
even for an agent that never loaded the skill.

Reads the hook payload on stdin, writes a PreToolUse decision on stdout.

Anything unexpected lets the edit through. A coordination helper must never be
the reason a session stops working, so a broken payload, a missing pa.py or a
slow claim all fall back to allowing the edit.

Environment:
  PA_AS           this agent's name (default: claude-<session id>)
  PA_HOOK_DISABLE set to anything to turn the hook off
  PA_HOOK_MODE    "claim" (default) also takes the path; "check" only blocks
  PA_HOOK_TTL     how long an auto-claim lives without a further edit (15m)
"""

import json
import os
import subprocess
import sys

EDITING_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
PATH_KEYS = ("file_path", "notebook_path")
PA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pa.py")
DEFAULT_TTL = "15m"
EXIT_CONFLICT = 3
TIMEOUT = 10


def allow():
    """Say nothing: Claude Code then decides the call on its own rules."""
    sys.exit(0)


def deny(reason):
    json.dump({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}, sys.stdout)
    sys.exit(0)


def wanted_paths(tool_input):
    out = []
    for key in PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            out.append(value)
    return out


def agent_name(payload):
    """This agent's name: PA_AS, else one derived from the session id.

    Two Claude Code sessions on one repo get different session ids, which is
    exactly the identity the board needs. pa.py requires [a-z0-9][a-z0-9._-]*.
    """
    name = (os.environ.get("PA_AS") or "").strip()
    if name:
        return name
    session = "".join(c for c in str(payload.get("session_id") or "") if c.isalnum())
    return ("claude-" + session[:8].lower()) if session else "claude"


def explain(stdout, paths):
    holders = []
    try:
        for conflict in (json.loads(stdout).get("conflicts") or []):
            who = conflict.get("agent") or "another agent"
            held = ", ".join(conflict.get("paths") or []) or conflict.get("requested") or ""
            why = conflict.get("why") or ""
            holders.append("%s holds %s%s" % (who, held, ' "%s"' % why if why else ""))
    except (ValueError, AttributeError):
        pass
    lines = ["parallel-agents: %s is claimed by another agent." % ", ".join(paths)]
    lines.extend("  " + h for h in holders)
    lines.append("Do not edit these paths. Work on something else and come back to them, "
                 "or ask the user. `pa status` shows the whole board.")
    return "\n".join(lines)


def main():
    if os.environ.get("PA_HOOK_DISABLE"):
        allow()
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        allow()
    if not isinstance(payload, dict) or payload.get("tool_name") not in EDITING_TOOLS:
        allow()

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        allow()
    paths = wanted_paths(tool_input)
    if not paths or not os.path.exists(PA):
        allow()

    check_only = (os.environ.get("PA_HOOK_MODE") or "").strip().lower() == "check"
    argv = [sys.executable, PA, "check" if check_only else "claim"]
    argv += paths + ["--as", agent_name(payload), "--json"]
    if not check_only:
        argv += ["--why", "%s via Claude Code" % payload["tool_name"],
                 "--ttl", (os.environ.get("PA_HOOK_TTL") or DEFAULT_TTL).strip()]

    try:
        done = subprocess.run(argv, cwd=payload.get("cwd") or None,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        allow()

    # Only a conflict blocks. A usage or disk error means the board is not
    # answering, and a board that cannot answer must not hold the agent up.
    if done.returncode != EXIT_CONFLICT:
        allow()
    deny(explain(done.stdout, paths))


if __name__ == "__main__":
    main()
