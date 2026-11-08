"""End-to-end tests: every test drives the real CLI in a real git repository."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, os.pardir, "skills", "parallel-agents", "scripts", "pa.py")

spec = importlib.util.spec_from_file_location("pa", SCRIPT)
pa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pa)


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def run(cwd, *args, env=None):
    e = dict(os.environ)
    e.pop("PA_AS", None)
    e.pop("PA_DIR", None)
    e.update(env or {})
    return subprocess.run([sys.executable, SCRIPT, *args], cwd=cwd, capture_output=True, text=True, env=e)


class RepoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="pa-test-"))
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "src", "api"))
        for rel in ("src/api/views.py", "src/models.py", "README.md"):
            with open(os.path.join(self.repo, *rel.split("/")), "w") as f:
                f.write("x\n")
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def claim(self, agent, *paths, cwd=None, extra=()):
        return run(cwd or self.repo, "claim", *paths, "--as", agent, *extra)

    def state_dir(self):
        return os.path.join(self.repo, ".git", "parallel-agents")


class ClaimingTest(RepoTest):
    def test_second_agent_is_blocked(self):
        self.assertEqual(self.claim("claude", "src/api/views.py", extra=("--why", "refactor auth")).returncode, 0)

        blocked = self.claim("codex", "src/api/views.py")
        self.assertEqual(blocked.returncode, 3)
        self.assertIn("BLOCKED", blocked.stdout)
        self.assertIn("claude", blocked.stdout)
        self.assertIn("refactor auth", blocked.stdout)

        self.assertEqual(run(self.repo, "check", "src/api/views.py", "--as", "codex").returncode, 3)
        self.assertEqual(run(self.repo, "check", "src/api/views.py", "--as", "claude").returncode, 0)

    def test_claim_is_all_or_nothing(self):
        self.claim("claude", "src/models.py")
        self.assertEqual(self.claim("codex", "src/models.py", "README.md").returncode, 3)
        status = json.loads(run(self.repo, "status", "--json").stdout)
        self.assertNotIn("codex", status["agents"])

    def test_folder_covers_its_files(self):
        self.claim("claude", "src/api")
        self.assertEqual(self.claim("codex", "src/api/views.py").returncode, 3)
        self.assertEqual(self.claim("codex", "src/models.py").returncode, 0)

    def test_glob_covers_matching_files(self):
        self.claim("claude", "src/*.py")
        self.assertEqual(self.claim("codex", "src/models.py").returncode, 3)
        self.assertEqual(self.claim("codex", "README.md").returncode, 0)

    def test_resources_are_exact_and_case_insensitive(self):
        self.claim("claude", "@migrations")
        self.assertEqual(self.claim("codex", "@Migrations").returncode, 3)
        self.assertEqual(self.claim("codex", "@deps").returncode, 0)

    def test_paths_are_relative_to_the_repo_not_the_cwd(self):
        self.claim("claude", "src/models.py")
        self.assertEqual(self.claim("codex", "models.py", cwd=os.path.join(self.repo, "src")).returncode, 3)

    def test_reclaiming_your_own_files_is_fine(self):
        self.claim("claude", "src/models.py")
        self.assertEqual(self.claim("claude", "src/models.py", "README.md").returncode, 0)

    def test_a_file_inside_a_folder_you_hold_adds_no_second_claim(self):
        self.claim("claude", "src/api/", extra=("--why", "add refund endpoint"))
        self.assertEqual(self.claim("claude", "src/api/views.py").returncode, 0)
        board = json.loads(run(self.repo, "status", "--json").stdout)["agents"]
        self.assertEqual([c["paths"] for c in board["claude"]], [["src/api/"]])
        self.assertEqual(board["claude"][0]["why"], "add refund endpoint")

    def test_a_folder_is_still_recorded_when_you_only_held_a_file_in_it(self):
        self.claim("claude", "src/api/views.py")
        self.assertEqual(self.claim("claude", "src/api/").returncode, 0)
        board = json.loads(run(self.repo, "status", "--json").stdout)["agents"]
        self.assertEqual(sorted(p for c in board["claude"] for p in c["paths"]),
                         ["src/api/", "src/api/views.py"])

    def test_release_some_then_all(self):
        self.claim("claude", "src/models.py", "README.md")
        run(self.repo, "release", "README.md", "--as", "claude")
        self.assertEqual(self.claim("codex", "README.md").returncode, 0)
        self.assertEqual(self.claim("codex", "src/models.py").returncode, 3)
        run(self.repo, "release", "--as", "claude")
        self.assertEqual(self.claim("codex", "src/models.py").returncode, 0)

    def test_expired_claims_stop_blocking(self):
        self.claim("claude", "src/models.py", extra=("--ttl", "10s"))
        path = os.path.join(self.state_dir(), "claims", "claude.json")
        with open(path) as f:
            data = json.load(f)
        data["claims"][0]["heartbeat"] -= 60  # pretend claude crashed a minute ago
        with open(path, "w") as f:
            json.dump(data, f)
        self.assertEqual(self.claim("codex", "src/models.py").returncode, 0)

    def test_evict_is_a_human_override(self):
        self.claim("claude", "src/models.py")
        self.assertEqual(run(self.repo, "evict", "claude").returncode, 0)
        self.assertEqual(self.claim("codex", "src/models.py").returncode, 0)


class SafetyTest(RepoTest):
    def test_state_never_shows_up_in_git_status(self):
        self.claim("claude", "src/models.py")
        run(self.repo, "note", "hello", "--as", "claude")
        out = subprocess.run(["git", "status", "--porcelain"], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "")

    def test_paths_outside_the_repo_are_refused(self):
        self.assertEqual(self.claim("claude", "../elsewhere.py").returncode, 2)
        self.assertEqual(self.claim("claude", ".").returncode, 2)

    def test_an_agent_name_is_required(self):
        self.assertEqual(run(self.repo, "claim", "README.md").returncode, 2)
        self.assertEqual(run(self.repo, "claim", "README.md", env={"PA_AS": "gemini"}).returncode, 0)
        self.assertEqual(self.claim("bad name!", "README.md").returncode, 2)

    def test_concurrent_claims_have_exactly_one_winner(self):
        agents = ["agent-%02d" % i for i in range(16)]
        with ThreadPoolExecutor(max_workers=len(agents)) as pool:
            results = list(pool.map(lambda a: self.claim(a, "src/models.py").returncode, agents))
        self.assertEqual(results.count(0), 1, results)
        self.assertEqual(results.count(3), len(agents) - 1, results)


class RegressionTest(RepoTest):
    """Each test pins a bug found in review: two agents must never both hold the same file."""

    def assertBlocked(self, first, second):
        self.assertEqual(self.claim("claude", first).returncode, 0, first)
        result = self.claim("codex", second)
        self.assertEqual(result.returncode, 3, "%s vs %s\n%s%s" % (first, second, result.stdout, result.stderr))

    def test_double_star_glob_also_matches_the_top_folder(self):
        self.assertBlocked("src/**/*.py", "src/models.py")

    def test_trailing_slash_glob_covers_files_below(self):
        self.assertBlocked("src/*/", "src/api/views.py")

    def test_symlinked_glob_prefix_resolves_to_the_real_folder(self):
        try:
            os.symlink(os.path.join(self.repo, "src"), os.path.join(self.repo, "lnk"))
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not available")
        self.assertBlocked("lnk/*.py", "src/models.py")

    def test_folder_that_does_not_exist_yet_covers_future_files(self):
        self.assertBlocked("docs/api", "docs/api/x.md")

    def test_release_still_works_after_the_folder_is_created(self):
        self.claim("claude", "newdir")
        os.makedirs(os.path.join(self.repo, "newdir"))
        run(self.repo, "release", "newdir", "--as", "claude")
        self.assertEqual(json.loads(run(self.repo, "status", "--json").stdout)["agents"], {})

    def test_paths_compare_case_insensitively(self):
        self.assertBlocked("Src/Models.py", "src/models.py")

    def test_agent_names_are_case_insensitive(self):
        self.claim("Claude", "src/models.py")
        self.assertEqual(self.claim("claude", "src/models.py").returncode, 0)
        self.assertEqual(sorted(json.loads(run(self.repo, "status", "--json").stdout)["agents"]), ["claude"])

    def test_a_blocked_claim_still_refreshes_your_own_claims(self):
        self.claim("claude", "README.md")
        path = os.path.join(self.state_dir(), "claims", "claude.json")
        with open(path) as f:
            data = json.load(f)
        old = data["claims"][0]["heartbeat"] - 1000
        data["claims"][0]["heartbeat"] = old
        with open(path, "w") as f:
            json.dump(data, f)
        self.claim("codex", "src/models.py")
        self.assertEqual(self.claim("claude", "src/models.py").returncode, 3)
        with open(path) as f:
            self.assertGreater(json.load(f)["claims"][0]["heartbeat"], old + 900)

    def test_renew_with_nothing_held_says_so(self):
        result = run(self.repo, "renew", "--as", "claude")
        self.assertEqual(result.returncode, 3)
        self.assertIn("NOTHING HELD", result.stdout)

    def test_windows_reserved_names_are_refused(self):
        self.assertEqual(self.claim("con", "README.md").returncode, 2)

    def test_broken_git_never_falls_back_to_a_private_board(self):
        result = run(self.repo, "claim", "README.md", "--as", "claude", env={"GIT_DIR": os.path.join(self.tmp, "missing")})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("git failed", result.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".parallel-agents")))

    def test_a_corrupt_claims_file_does_not_break_the_board(self):
        self.claim("claude", "README.md")
        with open(os.path.join(self.state_dir(), "claims", "broken.json"), "w") as f:
            f.write("[1, 2, 3]")
        self.assertEqual(run(self.repo, "status").returncode, 0)
        self.assertEqual(self.claim("codex", "README.md").returncode, 3)

    def test_json_errors_are_json(self):
        result = run(self.repo, "claim", "README.md", "--as", "bad name!", "--json")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stdout)["ok"])


class WorktreeTest(RepoTest):
    def test_other_worktrees_get_a_warning_not_a_block(self):
        wt = os.path.join(self.tmp, "wt")
        git(self.repo, "worktree", "add", "-q", "-b", "feature", wt)
        self.claim("claude", "src/models.py")

        other = self.claim("codex", "src/models.py", cwd=wt)
        self.assertEqual(other.returncode, 0, other.stdout + other.stderr)
        self.assertIn("WARN", other.stdout)
        self.assertIn("merge conflict", other.stdout)


class NotesTest(RepoTest):
    def test_notes_round_trip(self):
        run(self.repo, "note", "auth refactor done, tests left", "--as", "claude", "--paths", "src/api")
        entries = json.loads(run(self.repo, "log", "--json").stdout)
        self.assertEqual(entries[-1]["agent"], "claude")
        self.assertEqual(entries[-1]["paths"], ["src/api/"])
        self.assertIn("tests left", run(self.repo, "log").stdout)


class OutsideGitTest(unittest.TestCase):
    def test_works_without_git(self):
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="pa-nogit-"))
        try:
            self.assertEqual(run(tmp, "claim", "notes.txt", "--as", "claude").returncode, 0)
            self.assertTrue(os.path.isdir(os.path.join(tmp, ".parallel-agents")))
            self.assertEqual(run(tmp, "claim", "notes.txt", "--as", "codex").returncode, 3)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_watch_without_git(self):
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="pa-nogit-"))
        try:
            self.assertEqual(run(tmp, "claim", "notes.txt", "--as", "claude").returncode, 0)
            r = run(tmp, "watch", "--once")
            self.assertEqual(r.returncode, 0)
            self.assertIn("claude", r.stdout)
            self.assertIn("notes.txt", r.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class WatchTest(RepoTest):
    def test_watch_once_empty(self):
        r = run(self.repo, "watch", "--once")
        self.assertEqual(r.returncode, 0)
        self.assertIn("No active claims. Everything is free.", r.stdout)

    def test_watch_once_with_claims(self):
        self.claim("alice", "src/models.py")
        r = run(self.repo, "watch", "--once")
        self.assertEqual(r.returncode, 0)
        self.assertIn("alice", r.stdout)
        self.assertIn("src/models.py", r.stdout)

    def test_watch_once_json(self):
        self.claim("bob", "README.md")
        r = run(self.repo, "watch", "--once", "--json")
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout)
        self.assertTrue(data.get("ok"))
        self.assertIn("bob", data["agents"])
        self.assertEqual(data["agents"]["bob"][0]["paths"], ["README.md"])


class UnitTest(unittest.TestCase):
    def test_overlaps(self):
        cases = [
            ("a.py", "a.py", True),
            ("a.py", "b.py", False),
            ("src/", "src/a.py", True),
            ("src/", "srcx/a.py", False),
            ("src/*.py", "src/a.py", True),
            ("src/*.py", "lib/a.py", False),
            ("src/*.py", "src/", True),
            ("*.py", "anything/at/all.py", True),
            ("src/a/*.py", "src/b/*.py", False),
            ("src/*", "src/a/*.py", True),
            ("@db", "@db", True),
            ("@db", "db", False),
            ("@DB", "@db", True),
            ("src/**/*.py", "src/a.py", True),
            ("src/**/*.py", "src/x/y/z.py", True),
            ("src/**/*.py", "lib/a.py", False),
            ("src/*", "src/x/y.py", True),
            ("src/api/*.py", "src", True),
            ("src/[ab].py", "src/a.py", True),
            ("src/[ab].py", "src/c.py", False),
            ("tests/*.py", "src/", False),
            ("Src/Models.py", "src/models.py", True),
            ("docs/api", "docs/api/x.md", True),
            ("docs/api/", "docs/api", True),
        ]
        for a, b, expected in cases:
            self.assertEqual(pa.overlaps(a, b), expected, (a, b))
            self.assertEqual(pa.overlaps(b, a), expected, (b, a))

    def test_covers(self):
        cases = [
            ("src/", "src/a.py", True),
            ("src/api", "src/api/views.py", True),
            ("src/api/", "src/api", True),
            ("Src/", "src/a.py", True),
            ("src/a.py", "src/", False),       # a file does not cover its folder
            ("src/", "srcx/a.py", False),
            ("src/**/*.py", "src/x/y.py", True),
            ("src/**/*.py", "lib/a.py", False),
            ("src/", "src/*.py", False),       # a glob is always recorded on its own
            ("@db", "@db", True),
            ("@db", "db", False),
            ("src/", "@migrations", False),
        ]
        for held, wanted, expected in cases:
            self.assertEqual(pa.covers(held, wanted), expected, (held, wanted))

    def test_parse_ttl(self):
        self.assertEqual(pa.parse_ttl("90s"), 90)
        self.assertEqual(pa.parse_ttl("30m"), 1800)
        self.assertEqual(pa.parse_ttl("2h"), 7200)
        self.assertEqual(pa.parse_ttl("45"), 2700)
        with self.assertRaises(pa.UsageError):
            pa.parse_ttl("soon")
        with self.assertRaises(pa.UsageError):
            pa.parse_ttl("2s")


if __name__ == "__main__":
    unittest.main()
