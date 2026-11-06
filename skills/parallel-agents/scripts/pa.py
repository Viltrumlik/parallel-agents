#!/usr/bin/env python3
"""pa - keep parallel coding agents out of each other's way.

Before an agent edits files, it claims them. Other agents see the claim and
work elsewhere instead of overwriting each other. Claims expire on their own,
so a crashed agent never blocks anyone for long.

State lives in <git common dir>/parallel-agents (shared by every worktree of
the repo and never committed), or in .parallel-agents/ outside git.

Zero dependencies. Python 3.9+. macOS, Linux and Windows.
"""

import argparse
import contextlib
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid

try:
    import fcntl  # POSIX
except ImportError:  # pragma: no cover - Windows
    fcntl = None
    import msvcrt

VERSION = "0.1.0"
STATE_NAME = "parallel-agents"
DEFAULT_TTL = 30 * 60
EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_CONFLICT = 0, 1, 2, 3
GLOB_CHARS = "*?["
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {"com%d" % i for i in range(1, 10)} | {"lpt%d" % i for i in range(1, 10)}
CASE_INSENSITIVE_FS = sys.platform in ("darwin", "win32")


class UsageError(Exception):
    pass


# ---------------------------------------------------------------------------
# Locating the repository and its shared state
# ---------------------------------------------------------------------------

class Workspace:
    """Where we are: the worktree root (paths are relative to it) and the
    state directory shared by all agents."""

    def __init__(self, root, state):
        self.root = os.path.realpath(root)
        self.state = state

    @classmethod
    def discover(cls, cwd=None):
        cwd = os.path.realpath(cwd or os.getcwd())
        override = os.environ.get("PA_DIR")
        git, error = _git_dirs(cwd)
        if git:
            root, common = git
            return cls(root, override or os.path.join(common, STATE_NAME))
        # Never fall back to a private board inside a repo that git can't read:
        # that agent would see no one else's claims.
        probe = cwd
        while True:
            if os.path.exists(os.path.join(probe, ".git")):
                raise UsageError("this is a git repository but git failed (%s); fix git first" % (error or "unknown error"))
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        # Outside git: use the nearest .parallel-agents up the tree, or create one here.
        probe = cwd
        while True:
            candidate = os.path.join(probe, "." + STATE_NAME)
            if os.path.isdir(candidate):
                return cls(probe, override or candidate)
            parent = os.path.dirname(probe)
            if parent == probe:
                return cls(cwd, override or os.path.join(cwd, "." + STATE_NAME))
            probe = parent

    @property
    def claims_dir(self):
        return os.path.join(self.state, "claims")

    @property
    def log_path(self):
        return os.path.join(self.state, "log.jsonl")

    def ensure(self):
        os.makedirs(self.claims_dir, exist_ok=True)


def _git_dirs(cwd):
    """((worktree root, common dir), None) or (None, error text)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel", "--git-common-dir"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)
    if out.returncode != 0:
        return None, out.stderr.strip().splitlines()[-1] if out.stderr.strip() else "exit %d" % out.returncode
    lines = out.stdout.strip().splitlines()
    if len(lines) < 2:
        return None, "unexpected git output"
    root, common = lines[0], lines[1]
    if not os.path.isabs(common):
        common = os.path.join(cwd, common)
    return (os.path.realpath(root), os.path.realpath(common)), None


def same_path(a, b):
    if a is None or b is None:
        return a == b
    a, b = os.path.normpath(a), os.path.normpath(b)
    return a.casefold() == b.casefold() if CASE_INSENSITIVE_FS else a == b


# ---------------------------------------------------------------------------
# Locking and storage
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def locked(ws):
    """Exclusive lock around every read and write of the board, so two agents
    can never claim the same file at the same moment."""
    ws.ensure()
    fd = os.open(os.path.join(ws.state, ".lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:  # pragma: no cover - Windows
            deadline = time.time() + 30
            while True:
                try:
                    os.lseek(fd, 0, 0)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.time() > deadline:
                        raise OSError("could not lock %s after 30s" % ws.state)
                    time.sleep(0.02)
        yield
    finally:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_UN)
        else:  # pragma: no cover - Windows
            try:
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(fd)


def _atomic_write(path, data):
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _agent_file(ws, agent):
    return os.path.join(ws.claims_dir, agent + ".json")


def _valid_claim(c):
    return (isinstance(c, dict) and isinstance(c.get("paths"), list) and c["paths"]
            and all(isinstance(p, str) for p in c["paths"])
            and isinstance(c.get("heartbeat"), (int, float)) and isinstance(c.get("ttl"), (int, float))
            and isinstance(c.get("created"), (int, float)))


def load_all(ws):
    """{agent: [claim, ...]} for every agent with a claims file. Call under the lock."""
    result = {}
    if not os.path.isdir(ws.claims_dir):
        return result
    for name in sorted(os.listdir(ws.claims_dir)):
        if not name.endswith(".json") or name.startswith("."):
            continue
        try:
            with open(os.path.join(ws.claims_dir, name), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue  # a corrupt file never takes the whole board down
        if not isinstance(data, dict) or not isinstance(data.get("claims"), list):
            continue
        agent = str(data.get("agent") or name[:-5]).lower()
        result[agent] = [c for c in data["claims"] if _valid_claim(c)]
    return result


def save_agent(ws, agent, claims):
    path = _agent_file(ws, agent)
    if claims:
        _atomic_write(path, {"agent": agent, "claims": claims})
    else:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)


def is_active(claim, now):
    return now - claim["heartbeat"] <= claim["ttl"]


def drop_expired(ws, board, now):
    """Remove expired claims from disk. Returns the cleaned board and how many went."""
    removed = 0
    for agent in list(board):
        alive = [c for c in board[agent] if is_active(c, now)]
        if len(alive) != len(board[agent]):
            removed += len(board[agent]) - len(alive)
            save_agent(ws, agent, alive)
            board[agent] = alive
        if not board[agent]:
            del board[agent]
    return board, removed


# ---------------------------------------------------------------------------
# Paths, resources and overlap
# ---------------------------------------------------------------------------

def _is_glob(p):
    return any(ch in p for ch in GLOB_CHARS)


def normalize(ws, raw, cwd=None):
    """Turn a user path into a repo-relative POSIX path.

    - '@name' is a named resource (migrations, deps, a port) and is kept as is.
    - An existing directory gets a trailing '/' (for display; any path covers
      everything below it, existing or not).
    - Globs are kept as patterns relative to the repo root, with the folder
      part before the first wildcard resolved like any other path.
    """
    raw = raw.strip()
    if not raw:
        raise UsageError("empty path")
    if raw.startswith("@"):
        name = raw[1:].strip().lower()
        if not name:
            raise UsageError("empty resource name after '@'")
        return "@" + name
    cwd = os.path.realpath(cwd or os.getcwd())
    unified = raw.replace("\\", "/") if os.sep == "\\" else raw
    if _is_glob(unified):
        parts = unified.split("/")
        first = next(i for i, part in enumerate(parts) if _is_glob(part))
        static = "/".join(parts[:first]) or "."
        tail = "/".join(parts[first:]).rstrip("/")
        base = os.path.realpath(os.path.join(cwd, static))
        absolute = os.path.normpath(os.path.join(base, tail))
    else:
        absolute = os.path.realpath(os.path.join(cwd, raw))
    try:
        rel = os.path.relpath(absolute, ws.root)
    except ValueError:  # another drive on Windows
        raise UsageError("%s is outside the repository (%s)" % (raw, ws.root))
    if rel == os.curdir:
        raise UsageError("refusing to claim the whole repository; claim the files or folders you will edit")
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        raise UsageError("%s is outside the repository (%s)" % (raw, ws.root))
    rel = rel.replace(os.sep, "/").rstrip("/")
    if not _is_glob(rel) and os.path.isdir(os.path.join(ws.root, rel)):
        rel += "/"
    return rel


def _static_prefix(pattern):
    """'src/api/*.py' -> 'src/api'; '*.py' -> '' (could match anything)."""
    cut = min((pattern.index(ch) for ch in GLOB_CHARS if ch in pattern), default=len(pattern))
    head = pattern[:cut]
    return head[: head.rfind("/")] if "/" in head else ""


def _variants(glob):
    """'**/' may also match zero folders: 'src/**/*.py' also means 'src/*.py'."""
    seen, todo = set(), [glob]
    while todo:
        g = todo.pop()
        if g in seen:
            continue
        seen.add(g)
        i = g.find("**/")
        while i != -1:
            todo.append(g[:i] + g[i + 3:])
            i = g.find("**/", i + 1)
    return seen


def _matches(path, glob):
    return any(fnmatch.fnmatchcase(path, g) for g in _variants(glob))


def _witness(segment):
    """A concrete file name the last glob segment would match."""
    s = re.sub(r"\[![^\]]*\]", "~", segment)
    s = re.sub(r"\[([^\]])[^\]]*\]", r"\1", s)
    s = s.replace("*", "x").replace("?", "x")
    return s or "x"


def _under(path, folder):
    return path == folder or path.startswith(folder + "/")


def _glob_touches(glob, concrete):
    """Could a glob claim and a concrete claim (which covers itself and
    everything below it) touch the same file? Errs on the side of yes."""
    prefix = _static_prefix(glob)
    if prefix and _under(prefix, concrete):  # the concrete folder contains the glob's whole area
        return True
    parts = concrete.split("/")
    for i in range(len(parts), 0, -1):  # the concrete path, or a folder above it, is matched
        if _matches("/".join(parts[:i]), glob):
            return True
    leaf = _witness(glob.rsplit("/", 1)[-1])
    for depth in range(5):  # something below the concrete folder is matched
        if _matches(concrete + "/" + "x/" * depth + leaf, glob):
            return True
    return False


def overlaps(a, b):
    """Could these two claims touch the same file? Errs on the side of yes:
    every comparison is case-insensitive (macOS and Windows file systems are),
    and every path also covers anything that might later appear below it."""
    a, b = a.casefold().rstrip("/"), b.casefold().rstrip("/")
    if a.startswith("@") or b.startswith("@"):
        return a == b
    ga, gb = _is_glob(a), _is_glob(b)
    if not ga and not gb:
        return _under(a, b) or _under(b, a)
    if ga and gb:
        pa, pb = _static_prefix(a), _static_prefix(b)
        return pa == "" or pb == "" or _under(pa, pb) or _under(pb, pa)
    return _glob_touches(a, b) if ga else _glob_touches(b, a)


def same_claim_path(a, b):
    return a.casefold().rstrip("/") == b.casefold().rstrip("/")


def covers(held, wanted):
    """Does a path I already hold also cover this one? A folder covers what is
    under it, so claiming a file inside a folder of mine records nothing new.
    Keeps the board readable when an agent claims a folder and then edits
    thirty files in it."""
    h, w = held.casefold().rstrip("/"), wanted.casefold().rstrip("/")
    if h == w:
        return True
    if h.startswith("@") or w.startswith("@") or _is_glob(w):
        return False
    return _matches(w, h) if _is_glob(h) else _under(w, h)


def find_overlaps(ws, board, agent, paths, now):
    """Split other agents' overlapping claims into blocking (same worktree)
    and heads-up warnings (another worktree of the same repo)."""
    conflicts, warnings = [], []
    for other, claims in board.items():
        if other == agent:
            continue
        for claim in claims:
            if not is_active(claim, now):
                continue
            hits = sorted({p for p in paths for q in claim["paths"] if overlaps(p, q)})
            if not hits:
                continue
            entry = {"agent": other, "claim": claim, "requested": hits}
            (conflicts if same_path(claim.get("worktree"), ws.root) else warnings).append(entry)
    return conflicts, warnings


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def parse_ttl(text):
    match = re.fullmatch(r"\s*(\d+)\s*([smh]?)\s*", text or "")
    if not match:
        raise UsageError("bad --ttl %r (use e.g. 90s, 30m, 2h)" % text)
    value, unit = int(match.group(1)), match.group(2) or "m"
    seconds = value * {"s": 1, "m": 60, "h": 3600}[unit]
    if not 10 <= seconds <= 24 * 3600:
        raise UsageError("--ttl must be between 10s and 24h")
    return seconds


def ago(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    return "%dh%02dm" % (seconds // 3600, (seconds % 3600) // 60)


def describe(entry, now, ws):
    c = entry["claim"]
    where = "" if same_path(c.get("worktree"), ws.root) else "  [worktree %s]" % c.get("worktree")
    why = (' "%s"' % c["why"]) if c.get("why") else ""
    return "%s holds %s%s (since %s ago, expires in %s)%s" % (
        entry["agent"], ", ".join(c["paths"]), why,
        ago(now - c["created"]), ago(c["heartbeat"] + c["ttl"] - now), where)


def brief(entry):
    c = entry["claim"]
    return {"agent": entry["agent"], "paths": c["paths"], "why": c.get("why", ""),
            "requested": entry["requested"], "worktree": c.get("worktree")}


def emit_json(obj):
    print(json.dumps(obj, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def need_agent(args):
    agent = (args.as_ or os.environ.get("PA_AS") or "").strip().lower()
    if not agent:
        raise UsageError("say who you are: --as <name> (e.g. --as claude-7f2c), or set PA_AS")
    if not NAME_RE.match(agent):
        raise UsageError("agent name %r: use letters, digits, '.', '_' or '-' (max 64)" % agent)
    if agent.split(".")[0] in WINDOWS_RESERVED:
        raise UsageError("agent name %r is reserved on Windows; pick another" % agent)
    return agent


def optional_agent(args):
    return need_agent(args) if (args.as_ or os.environ.get("PA_AS")) else None


def cmd_init(ws, args):
    ws.ensure()
    if args.json:
        emit_json({"ok": True, "state": ws.state, "root": ws.root})
    else:
        print("parallel-agents state: %s" % ws.state)
        print("repository root:       %s" % ws.root)
    return EXIT_OK


def cmd_claim(ws, args):
    agent = need_agent(args)
    ttl = parse_ttl(args.ttl) if args.ttl else DEFAULT_TTL
    paths = sorted({normalize(ws, p) for p in args.paths})
    now = time.time()
    with locked(ws):
        board, _ = drop_expired(ws, load_all(ws), now)
        mine = board.get(agent, [])
        for c in mine:  # any claim attempt, even a blocked one, refreshes all of mine
            c["heartbeat"] = now
        conflicts, warnings = find_overlaps(ws, board, agent, paths, now)
        if conflicts:
            save_agent(ws, agent, mine)
            if args.json:
                emit_json({"ok": False, "agent": agent, "conflicts": [brief(c) for c in conflicts]})
            else:
                print("BLOCKED: nothing was claimed.")
                for c in conflicts:
                    print("  " + describe(c, now, ws))
                print("Do not edit these paths. Work on something else and retry later, or ask the user.")
            return EXIT_CONFLICT

        held = [p for c in mine for p in c["paths"]]
        new_paths = [p for p in paths if not any(covers(h, p) for h in held)]
        for c in mine:
            c["ttl"] = max(c["ttl"], ttl)
        if new_paths:
            mine.append({
                "id": uuid.uuid4().hex[:8],
                "paths": new_paths,
                "why": args.why or "",
                "worktree": ws.root,
                "created": now,
                "heartbeat": now,
                "ttl": ttl,
            })
        save_agent(ws, agent, mine)

    if args.json:
        emit_json({"ok": True, "agent": agent, "claimed": paths, "expires_in": ttl,
                   "warnings": [brief(w) for w in warnings]})
    else:
        print("OK: %s now holds %s (expires in %s unless renewed)." % (agent, ", ".join(paths), ago(ttl)))
        for w in warnings:
            print("WARN: same paths in another worktree, expect a merge conflict: " + describe(w, now, ws))
    return EXIT_OK


def cmd_release(ws, args):
    agent = need_agent(args)
    targets = [normalize(ws, p) for p in args.paths] if args.paths else None
    released, keep = [], []
    with locked(ws):
        mine = load_all(ws).get(agent, [])
        for c in mine:
            if targets is None:
                released.extend(c["paths"])
                continue
            gone = [p for p in c["paths"] if any(same_claim_path(p, t) for t in targets)]
            released.extend(gone)
            remaining = [p for p in c["paths"] if p not in gone]
            if remaining:
                c["paths"] = remaining
                keep.append(c)
        save_agent(ws, agent, keep)
        held = [p for c in keep for p in c["paths"]]
    missing = [t for t in (targets or []) if not any(same_claim_path(t, r) for r in released)]
    if args.json:
        emit_json({"ok": True, "agent": agent, "released": sorted(released), "not_held": missing})
        return EXIT_OK
    if released:
        print("OK: %s released %s." % (agent, ", ".join(sorted(released))))
    elif targets is None:
        print("Nothing to release for %s." % agent)
    for t in missing:
        print("NOT HELD: %s. You hold: %s" % (t, ", ".join(held) or "nothing"))
    return EXIT_OK


def cmd_check(ws, args):
    agent = optional_agent(args)
    paths = sorted({normalize(ws, p) for p in args.paths})
    now = time.time()
    with locked(ws):
        board = load_all(ws)
    conflicts, warnings = find_overlaps(ws, board, agent, paths, now)
    if args.json:
        emit_json({"ok": not conflicts, "free": not conflicts,
                   "conflicts": [brief(c) for c in conflicts], "warnings": [brief(w) for w in warnings]})
    elif conflicts:
        for c in conflicts:
            print("TAKEN: " + describe(c, now, ws))
    else:
        print("FREE: %s" % ", ".join(paths))
        for w in warnings:
            print("WARN: " + describe(w, now, ws))
    return EXIT_CONFLICT if conflicts else EXIT_OK


def cmd_status(ws, args):
    now = time.time()
    with locked(ws):
        board = load_all(ws)
    board = {a: [c for c in cs if is_active(c, now)] for a, cs in board.items()}
    board = {a: cs for a, cs in board.items() if cs}
    if args.json:
        emit_json({"ok": True, "state": ws.state, "agents": {
            a: [dict(c, expires_in=int(c["heartbeat"] + c["ttl"] - now)) for c in cs]
            for a, cs in board.items()}})
        return EXIT_OK
    if not board:
        print("No active claims. Everything is free.")
        return EXIT_OK
    for agent, claims in sorted(board.items()):
        print(agent)
        for c in claims:
            where = "" if same_path(c.get("worktree"), ws.root) else "  [worktree %s]" % c.get("worktree")
            why = ('  "%s"' % c["why"]) if c.get("why") else ""
            print("  %s%s  (since %s ago, expires in %s)%s" % (
                ", ".join(c["paths"]), why, ago(now - c["created"]),
                ago(c["heartbeat"] + c["ttl"] - now), where))
    return EXIT_OK


def cmd_renew(ws, args):
    agent = need_agent(args)
    ttl = parse_ttl(args.ttl) if args.ttl else None
    now = time.time()
    with locked(ws):
        board, _ = drop_expired(ws, load_all(ws), now)
        mine = board.get(agent, [])
        for c in mine:
            c["heartbeat"] = now
            if ttl:
                c["ttl"] = ttl
        save_agent(ws, agent, mine)
    if args.json:
        emit_json({"ok": bool(mine), "agent": agent, "renewed": len(mine)})
    elif mine:
        print("OK: renewed %d claim(s) for %s." % (len(mine), agent))
    else:
        print("NOTHING HELD: %s has no live claims. They may have expired; claim again before editing." % agent)
    return EXIT_OK if mine else EXIT_CONFLICT


def cmd_note(ws, args):
    agent = need_agent(args)
    text = " ".join(args.text).strip()
    if not text:
        raise UsageError("note text is empty")
    entry = {"ts": time.time(), "agent": agent, "text": text,
             "paths": sorted({normalize(ws, p) for p in (args.paths or [])})}
    with locked(ws):
        with open(ws.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if args.json:
        emit_json({"ok": True, "note": entry})
    else:
        print("OK: note saved.")
    return EXIT_OK


def cmd_log(ws, args):
    entries = []
    with locked(ws):
        if os.path.exists(ws.log_path):
            with open(ws.log_path, encoding="utf-8") as f:
                for line in f:
                    with contextlib.suppress(ValueError):
                        e = json.loads(line)
                        if isinstance(e, dict) and isinstance(e.get("ts"), (int, float)) and e.get("text"):
                            entries.append(e)
    entries = entries[-max(1, args.n):]
    if args.json:
        emit_json(entries)
        return EXIT_OK
    if not entries:
        print("No notes yet.")
    now = time.time()
    for e in entries:
        paths = (" [%s]" % ", ".join(e["paths"])) if e.get("paths") else ""
        print("%s ago  %s: %s%s" % (ago(now - e["ts"]), e.get("agent", "?"), e["text"], paths))
    return EXIT_OK


def cmd_gc(ws, args):
    with locked(ws):
        _, removed = drop_expired(ws, load_all(ws), time.time())
        for name in os.listdir(ws.claims_dir):  # leftovers of a process killed mid-write
            path = os.path.join(ws.claims_dir, name)
            if name.startswith(".tmp-") and time.time() - os.path.getmtime(path) > 60:
                with contextlib.suppress(OSError):
                    os.unlink(path)
    if args.json:
        emit_json({"ok": True, "removed": removed})
    else:
        print("Removed %d expired claim(s)." % removed)
    return EXIT_OK


def cmd_evict(ws, args):
    """Human override: drop another agent's claims (e.g. it crashed and you
    don't want to wait for the TTL)."""
    target = args.agent.strip().lower()
    with locked(ws):
        count = sum(len(c["paths"]) for c in load_all(ws).get(target, []))
        save_agent(ws, target, [])
    if args.json:
        emit_json({"ok": True, "evicted": target, "paths": count})
    else:
        print("Evicted %s (%d path(s))." % (target, count))
    return EXIT_OK


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="pa",
        description="Keep parallel coding agents out of each other's way: claim files before editing them.",
        epilog="Exit codes: 0 ok, 1 error, 2 usage error (nothing was claimed), 3 blocked or nothing held.",
    )
    p.add_argument("--version", action="version", version="parallel-agents " + VERSION)
    sub = p.add_subparsers(dest="command", metavar="<command>")

    def add(name, help_text, func):
        sp = sub.add_parser(name, help=help_text, description=help_text)
        sp.set_defaults(func=func)
        sp.add_argument("--json", action="store_true", help="machine-readable output")
        return sp

    def who(sp, required=True):
        sp.add_argument("--as", dest="as_", metavar="NAME",
                        help="your agent name, e.g. claude-7f2c (or set PA_AS)" if required
                        else "your agent name; your own claims never count as taken")

    add("init", "Create the shared state directory and show where it is.", cmd_init)

    sp = add("claim", "Claim files, folders, globs or @resources before editing them.", cmd_claim)
    sp.add_argument("paths", nargs="+", metavar="PATH")
    who(sp)
    sp.add_argument("--why", help="what you are about to do (shown to other agents)")
    sp.add_argument("--ttl", help="how long the claim lives without renewal (default 30m)")

    sp = add("release", "Release some or all of your claims.", cmd_release)
    sp.add_argument("paths", nargs="*", metavar="PATH")
    who(sp)

    sp = add("check", "Exit 0 if free (or yours with --as), 3 if another agent holds it.", cmd_check)
    sp.add_argument("paths", nargs="+", metavar="PATH")
    who(sp, required=False)

    add("status", "Show who holds what right now.", cmd_status)

    sp = add("renew", "Keep your claims alive during long tasks (exit 3 if you hold nothing).", cmd_renew)
    who(sp)
    sp.add_argument("--ttl", help="new lifetime, e.g. 1h")

    sp = add("note", "Leave a handoff note for the next agent.", cmd_note)
    sp.add_argument("text", nargs="+")
    who(sp)
    sp.add_argument("--paths", nargs="*", metavar="PATH", help="files the note is about")

    sp = add("log", "Read recent handoff notes.", cmd_log)
    sp.add_argument("-n", type=int, default=10, help="how many (default 10)")

    add("gc", "Delete expired claims and leftover temp files.", cmd_gc)

    sp = add("evict", "Human override: drop all claims of another agent.", cmd_evict)
    sp.add_argument("agent")

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    try:
        return args.func(Workspace.discover(), args)
    except UsageError as e:
        if getattr(args, "json", False):
            emit_json({"ok": False, "error": str(e)})
        print("error: %s" % e, file=sys.stderr)
        return EXIT_USAGE
    except OSError as e:
        if getattr(args, "json", False):
            emit_json({"ok": False, "error": str(e)})
        print("error: %s" % e, file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
