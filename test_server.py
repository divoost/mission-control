#!/usr/bin/env python3
"""Mission Control — test suite (stdlib unittest only, zero dependencies).

Run:   python3 test_server.py           (or: python3 -m unittest -v test_server)

Covers the pure parsing/scoring helpers and the real git-derived metrics
(DB tables, feature completion, risk detection, dev report) against a
throwaway git repo fixture — so the numbers the dashboard shows are checked,
not assumed.
"""
import os, sys, json, tempfile, subprocess, shutil, unittest

HERE = os.path.dirname(os.path.abspath(__file__))

# Point config at an empty one BEFORE importing server, so importing it does
# not scan the home directory, and give it an empty Claude-logs dir.
_cfg = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
json.dump({"scanDirs": [], "projects": []}, _cfg)
_cfg.close()
os.environ["MC_CONFIG"] = _cfg.name
_empty_logs = tempfile.mkdtemp(prefix="mc-logs-")
os.environ["MC_CLAUDE_PROJECTS"] = _empty_logs

sys.path.insert(0, HERE)
import server  # noqa: E402


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def make_repo(with_tests=False):
    d = tempfile.mkdtemp(prefix="mc-repo-")
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@example.com")
    _git(d, "config", "user.name", "Tester")
    with open(os.path.join(d, "schema.sql"), "w") as f:
        f.write("CREATE TABLE users (id int);\nCREATE TABLE orders (id int);\n")
    with open(os.path.join(d, "app.js"), "w") as f:
        f.write("// TODO: handle errors\nfunction x(){ return 1; }\n")
    if with_tests:
        os.makedirs(os.path.join(d, "tests"))
        with open(os.path.join(d, "tests", "app.test.js"), "w") as f:
            f.write("test('x', () => {});\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "init")
    # churn: touch app.js several times so it becomes a hotspot
    for i in range(5):
        with open(os.path.join(d, "app.js"), "a") as f:
            f.write("\n// change %d\n" % i)
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "change %d" % i)
    return d


SAMPLE_PROJ_EXTRA = {
    "features": [{"n": "A", "s": "done"}, {"n": "B", "s": "wip"}, {"n": "C", "s": "todo"}],
    "milestones": [{"name": "M1", "done": True}, {"name": "M2", "done": False}],
}


class TestPureHelpers(unittest.TestCase):
    def test_act_clean_strips_injected(self):
        raw = 'hi <system-reminder>secret</system-reminder> <pasted_content id="x">junk</pasted_content> there'
        self.assertEqual(server._act_clean(raw), "hi there")

    def test_act_is_prompt_rejects_injected(self):
        for bad in ("The command below was run directly in Claude Code",
                    "Your response above was cut off mid-stream",
                    "Called the Read tool", "a1b2c3d4e5f6 toolu_0123456789"):
            self.assertFalse(server._act_is_prompt(server._act_clean(bad)), bad)

    def test_act_is_prompt_accepts_real(self):
        for good in ("캠페인 분석 차트 추가", "Fix the login redirect loop"):
            self.assertTrue(server._act_is_prompt(good), good)

    def test_edit_delta_counts_lines(self):
        self.assertEqual(server._edit_delta("Write", {"content": "a\nb\nc"}), (3, 0))
        add, rem = server._edit_delta("Edit", {"old_string": "x", "new_string": "a\nb\nc\nd"})
        self.assertEqual((add, rem), (4, 1))

    def test_act_verb_maps(self):
        self.assertEqual(server._act_verb("Edit", {"file_path": "/a/b/app.ts"}), ("edit", "app.ts"))
        v, t = server._act_verb("Bash", {"description": "Run tests"})
        self.assertEqual((v, t), ("run", "Run tests"))

    def test_sum_tokens_classifies(self):
        acc = {"input": 0, "output": 0, "cache": 0}
        server._sum_tokens({"input_tokens": 10, "output_tokens": 5,
                            "cache_read_input_tokens": 7}, acc)
        self.assertEqual(acc, {"input": 10, "output": 5, "cache": 7})


class TestEmptyState(unittest.TestCase):
    def test_activity_no_logs_is_safe(self):
        a = server.claude_activity()
        self.assertEqual(a["sessions"], [])
        self.assertEqual(a["feed"], [])

    def test_errors_no_logs_zero(self):
        self.assertEqual(server.claude_errors(), 0)


class TestGitDerived(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = make_repo(with_tests=False)
        cls.proj = dict(repo=cls.repo, name="Fixture", **SAMPLE_PROJ_EXTRA)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.repo, ignore_errors=True)

    def test_db_tables_counted(self):
        self.assertEqual(server.overview_item(self.proj)["db"], 2)

    def test_feature_completion_is_done_over_total(self):
        it = server.overview_item(self.proj)
        self.assertEqual(it["feat"], {"done": 1, "wip": 1, "todo": 1})
        self.assertEqual(it["featProgress"], 33)   # 1 of 3 registered features

    def test_report_detects_todo(self):
        r = server.project_report(self.proj)
        self.assertGreaterEqual(r["todoCount"], 1)
        self.assertTrue(any("TODO" in t["txt"] or "handle errors" in t["txt"] for t in r["todos"]))

    def test_risks_churn_and_no_tests(self):
        kinds = {rk["kind"] for rk in server.project_report(self.proj)["risks"]}
        self.assertIn("churn", kinds)    # app.js changed many times
        self.assertIn("notest", kinds)   # no test/spec files

    def test_no_test_risk_absent_when_tests_exist(self):
        repo2 = make_repo(with_tests=True)
        try:
            proj2 = dict(repo=repo2, name="Fixture2", **SAMPLE_PROJ_EXTRA)
            kinds = {rk["kind"] for rk in server.project_report(proj2)["risks"]}
            self.assertNotIn("notest", kinds)
        finally:
            shutil.rmtree(repo2, ignore_errors=True)

    def test_next_actions_from_real_signals(self):
        nxt = server.project_report(self.proj)["next"]
        ks = {n["k"] for n in nxt}
        self.assertIn("wip", ks)     # feature B (wip)
        self.assertIn("todo", ks)    # feature C (todo)

    def test_find_repos_discovers_git_dir(self):
        parent = os.path.dirname(self.repo)
        found = server.find_repos(parent)
        self.assertIn(self.repo, found)


if __name__ == "__main__":
    unittest.main(verbosity=2)
