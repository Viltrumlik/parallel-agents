"""End-to-end tests for the PreToolUse hook, driven the way Claude Code drives it."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, os.pardir, "skills", "parallel-agents", "scripts")
PA = os.path.join(SCRIPTS, "pa.py")
HOOK = os.path.join(SCRIPTS, "pa_hook.py")

SESSION = "7f2c91ab-4d5e-6789-0123-456789abcdef"
OTHER_SESSION = "a91e3307-1111-2222-3333-444455556666"


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def clean_env(extra=None):
    env = dict(os.environ)
    for key in ("PA_AS", "PA_DIR", "PA_HOOK_DISABLE", "PA_HOOK_MODE", "PA_HOOK_TTL"):
        env.pop(key, None)
    env.update(extra or {})
    return env


class HookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="pa-hook-test-"))
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "src", "api"))
        for rel in ("src/api/views.py", "src/models.py", "notes.ipynb"):
            with open(os.path.join(self.repo, *rel.split("/")), "w") as f:
                f.write("x\n")
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ----------------------------------------------------------

    def pa(self, *args, **kwargs):
        return subprocess.run([sys.executable, PA, *args], cwd=self.repo,
                              capture_output=True, text=True,
                              env=clean_env(kwargs.get("env")))

    def hook(self, payload, env=None, raw=None):
        """Run the hook the way Claude Code does: payload on stdin."""
        return subprocess.run([sys.executable, HOOK], cwd=self.repo,
                              input=json.dumps(payload) if raw is None else raw,
                              capture_output=True, text=True, env=clean_env(env))

    def edit(self, path="src/api/views.py", session=SESSION, tool="Edit"):
        return {"session_id": session, "cwd": self.repo, "hook_event_name": "PreToolUse",
                "tool_name": tool, "tool_input": {"file_path": path}}

    def assertAllowed(self, done):
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "", "hook blocked an edit it should have let through")

    def assertDenied(self, done):
        self.assertEqual(done.returncode, 0, done.stderr)
        out = json.loads(done.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertEqual(out["permissionDecision"], "deny")
        return out["permissionDecisionReason"]

    def board(self):
        return json.loads(self.pa("status", "--json").stdout)["agents"]

    # -- the protocol holds -----------------------------------------------

    def test_free_path_is_allowed_and_claimed(self):
        self.assertAllowed(self.hook(self.edit()))
        board = self.board()
        self.assertEqual(list(board), ["claude-7f2c91ab"], board)
        claim = board["claude-7f2c91ab"][0]
        self.assertEqual(claim["paths"], ["src/api/views.py"])
        self.assertEqual(claim["why"], "Edit via Claude Code")

    def test_path_held_by_another_agent_is_denied(self):
        self.pa("claim", "src/api/", "--as", "codex-a91e", "--why", "fix currency rounding")
        reason = self.assertDenied(self.hook(self.edit()))
        self.assertIn("codex-a91e", reason)
        self.assertIn("fix currency rounding", reason)
        self.assertIn("src/api/views.py", reason)

    def test_an_agent_is_never_blocked_by_its_own_claim(self):
        self.assertAllowed(self.hook(self.edit()))
        self.assertAllowed(self.hook(self.edit()))

    def test_two_sessions_are_two_agents(self):
        self.assertAllowed(self.hook(self.edit()))
        self.assertDenied(self.hook(self.edit(session=OTHER_SESSION)))

    def test_a_free_sibling_path_stays_free(self):
        self.assertAllowed(self.hook(self.edit()))
        self.assertAllowed(self.hook(self.edit(path="src/models.py", session=OTHER_SESSION)))

    def test_write_and_notebook_edits_are_covered(self):
        self.assertAllowed(self.hook(self.edit(tool="Write")))
        payload = self.edit(session=OTHER_SESSION, tool="NotebookEdit")
        payload["tool_input"] = {"notebook_path": "src/api/views.py"}
        self.assertDenied(self.hook(payload))

    # -- it stays out of the way ------------------------------------------

    def test_tools_that_do_not_write_are_ignored(self):
        payload = self.edit(tool="Bash")
        payload["tool_input"] = {"command": "ls"}
        self.assertAllowed(self.hook(payload))
        self.assertEqual(self.board(), {})

    def test_a_broken_payload_lets_the_edit_through(self):
        self.assertAllowed(self.hook(None, raw="not json at all"))
        self.assertAllowed(self.hook(None, raw=""))
        self.assertAllowed(self.hook(None, raw="[1, 2, 3]"))

    def test_a_payload_without_a_path_lets_the_edit_through(self):
        payload = self.edit()
        payload["tool_input"] = {}
        self.assertAllowed(self.hook(payload))
        self.assertEqual(self.board(), {})

    def test_disabling_the_hook_lets_a_held_path_through(self):
        self.pa("claim", "src/api/", "--as", "codex-a91e", "--why", "fix currency rounding")
        self.assertAllowed(self.hook(self.edit(), env={"PA_HOOK_DISABLE": "1"}))

    # -- configuration -----------------------------------------------------

    def test_check_mode_blocks_without_claiming(self):
        env = {"PA_HOOK_MODE": "check"}
        self.assertAllowed(self.hook(self.edit(), env=env))
        self.assertEqual(self.board(), {}, "check mode must not take the path")
        self.pa("claim", "src/api/", "--as", "codex-a91e", "--why", "fix currency rounding")
        self.assertDenied(self.hook(self.edit(), env=env))

    def test_pa_as_overrides_the_session_name(self):
        self.assertAllowed(self.hook(self.edit(), env={"PA_AS": "claude-mine"}))
        self.assertEqual(list(self.board()), ["claude-mine"])

    def test_ttl_is_configurable(self):
        self.assertAllowed(self.hook(self.edit(), env={"PA_HOOK_TTL": "1h"}))
        self.assertEqual(self.board()["claude-7f2c91ab"][0]["ttl"], 3600)

    def test_a_session_id_claude_code_did_not_send_still_names_an_agent(self):
        payload = self.edit()
        del payload["session_id"]
        self.assertAllowed(self.hook(payload))
        self.assertEqual(list(self.board()), ["claude"])


if __name__ == "__main__":
    unittest.main()
