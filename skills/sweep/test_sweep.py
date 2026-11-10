"""Fixture tests for sweep's classification logic. Run: python3 -I sweep.py --selftest"""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

import sweep

DAY = 86400
U = lambda n: f"{n:08x}-0000-4000-8000-000000000000"  # session ids are uuids


def touch(path, age_s=0, size=0):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    t = time.time() - age_s
    os.utime(path, (t, t))
    return path


def age_tree(path, age_s):
    t = time.time() - age_s
    for d, dirs, files in os.walk(path):
        for n in files + dirs:
            os.utime(os.path.join(d, n), (t, t))
    os.utime(path, (t, t))


def dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sweep-test-"))
        self.saved = {k: getattr(sweep, k) for k in ("BROKER_ROOT", "CODEX_SESSIONS", "PROJECTS", "HOME", "ARCHIVE_DIR", "trash",
                                                     "verify_archive", "identity", "job_activity", "live_sessions", "owner_map",
                                                     "alive", "cwd_map", "CACHE")}

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(sweep, k, v)
        subprocess.run(["chmod", "-R", "u+w", str(self.tmp)])
        for d, dirs, files in os.walk(self.tmp, topdown=False):
            for n in files:
                os.unlink(os.path.join(d, n))
            for n in dirs:
                p = os.path.join(d, n)
                os.unlink(p) if os.path.islink(p) else os.rmdir(p)
        os.rmdir(self.tmp)


class Processes(unittest.TestCase):
    def test_new_candidates(self):
        for cmd in ("/Applications/Blender.app/Contents/MacOS/Blender -b scene.blend -P render.py",
                    "/opt/blender/blender --background x.blend",
                    "/Users/me/Library/Caches/ms-playwright/chromium-1200/chrome-mac/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing --headless"):
            self.assertTrue(sweep.candidate(cmd), cmd)
        for cmd in ("/Applications/Blender.app/Contents/MacOS/Blender", "/Applications/Blender.app/Contents/MacOS/Blender scene.blend",
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
            self.assertFalse(sweep.candidate(cmd), cmd)
        self.assertEqual(sweep.label("/Applications/Blender.app/Contents/MacOS/Blender -b a.blend", []), "blender -b")


class Ownership(unittest.TestCase):
    # a live session's check run: claude -> zsh -> node (in-process server) -> node check -> browser
    table = sweep.parse_ps("\n".join([
        "999100 1 999100 01:00:00 0 1 me /Users/me/Library/Application Support/Claude/claude-code/2.1.308/x/claude.app/Contents/MacOS/claude --output-format stream-json",
        "999101 999100 999101 02:00 0 1 me /bin/zsh -c source /Users/me/.claude/shell-snapshots/snapshot-zsh-1.sh && node run-checks.mjs",
        "999103 999101 999101 02:00 0 1 me node run-checks.mjs /x/some-worktree",
        "999104 999103 999101 02:00 0 1 me node /x/scripts/dev/checks/default-identity.mjs http://127.0.0.1:4191",
        "999105 999104 999101 02:00 0 1 me /x/ms-playwright/chromium/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing --headless",
        "999200 1 999200 3-00:00:00 0 1 me node /x/node_modules/.bin/vite preview --port 5300",
    ]))

    def test_chain(self):
        self.assertEqual(sweep.chain(999105, self.table), [999105, 999104, 999103, 999101, 999100])

    def test_live_session_run_is_kept(self):
        for pid in (999103, 999104, 999105):
            got = sweep.assess(pid, self.table, sweep.descendants(pid, self.table), ["4191"], {"4191": {999105}})
            self.assertEqual(got["verdict"], "keep", pid)
            self.assertIn("999100", got["evidence"][0])  # the live claude session up the chain

    def test_unknown_kind_started_by_exited_session_is_listed_as_ask(self):
        table = sweep.parse_ps("999300 1 999300 2-00:00:00 0 1 me /opt/some/new-tool --serve\n")
        got = sweep.assess(999300, table, [999300], [], {}, owners={999300: 999999}, known=False)
        self.assertEqual(got["verdict"], "ask")
        self.assertIn("unknown kind", got["evidence"][-1])
        known = sweep.assess(999300, table, [999300], [], {}, owners={999300: 999999}, known=True)
        self.assertEqual(known["verdict"], "orphaned")  # a known kind with the same proof is a proven orphan

    def test_reparented_server_without_owner_is_ask_not_kill(self):
        got = sweep.assess(999200, self.table, [999200], ["5300"], {})
        self.assertEqual(got["verdict"], "ask")
        self.assertIn("owner unknown", got["evidence"][-1])

    def test_owner_matches_executable_not_arguments(self):
        own = lambda c: bool(sweep.CLAUDE_OWNER.search(c))
        self.assertTrue(own("/Users/me/Library/Application Support/Claude/claude-code/2.1.308/x/claude.app/Contents/MacOS/claude --model x"))
        self.assertTrue(own("/Applications/Claude.app/Contents/MacOS/Claude"))
        self.assertTrue(own("claude --resume abc"))
        self.assertFalse(own("/bin/zsh -c source /Users/me/.claude/shell-snapshots/s.sh && claude --version"))
        self.assertFalse(own("node /x/vite.js --config /y/claude --x"))

    def test_child_in_live_session_folder_is_not_orphaned(self):
        table = sweep.parse_ps("999400 1 999400 2-00:00:00 0 1 me node /x/node_modules/.bin/vite\n"
                               "999401 999400 999400 2-00:00:00 0 1 me node worker.js\n")
        args = dict(owners={999400: 999999}, known=True, sessions={"s": "/work/app"})
        root_only = sweep.assess(999400, table, [999400, 999401], [], {}, member_cwds=["/tmp", "/tmp"], **args)
        self.assertEqual(root_only["verdict"], "orphaned")
        child_inside = sweep.assess(999400, table, [999400, 999401], [], {}, member_cwds=["/tmp", "/work/app/src"], **args)
        self.assertEqual(child_inside["verdict"], "ask")

    def test_orphan_verdict(self):
        V = sweep.orphan_verdict
        self.assertEqual(V(None, False, 42, False, [], None)[0], "orphaned")  # owner session exited, nothing else
        self.assertEqual(V(None, False, 42, True, [], None)[0], "keep")       # owner session alive
        self.assertEqual(V(None, False, 42, False, [7], None)[0], "keep")     # someone connected to its port
        self.assertEqual(V(None, True, 42, False, [], None)[0], "keep")       # keep file
        self.assertEqual(V(None, False, 42, False, [], "/w")[0], "ask")       # cwd in a live session's folder
        self.assertEqual(V(None, False, None, False, [], None)[0], "ask")     # owner unknown
        self.assertEqual(V(5, False, None, False, [], None)[0], "keep")       # live ancestor
        self.assertEqual(V(None, False, 42, False, [], None, parent=77)[0], "ask")  # parent alive, not a Claude session
        self.assertEqual(V(None, False, 42, False, [], None, known=False)[0], "ask")  # proven ownerless but an unknown kind


class KeptAndYoung(unittest.TestCase):
    table = sweep.parse_ps("\n".join(line.replace(" me ", f" {sweep.getpass.getuser()} ") for line in [
        "998100 1 998100 05:00:00 0 1 me /Users/me/Library/Application Support/Claude/claude-code/2.1.308/x/claude.app/Contents/MacOS/claude --output-format stream-json",
        "998101 998100 998101 04:00:00 0 1 me /bin/zsh -c source /Users/me/.claude/shell-snapshots/snapshot-zsh-1.sh && npm run dev",
        "998102 998101 998101 04:00:00 0 1 me npm run dev",
        "998103 998102 998101 04:00:00 0 1 me node /x/node_modules/.bin/vite --host 127.0.0.1",
        "998200 1 998200 05:00:00 0 1 me /Applications/Claude.app/Contents/MacOS/Claude",
        "998201 998200 998201 03:00:00 0 1 me /Applications/Claude.app/Contents/Helpers/disclaimer -- /x/node npm run dev",
        "998202 998201 998201 03:00:00 0 1 me npm run dev",
        "998203 998202 998201 03:00:00 0 1 me node /x/node_modules/.bin/vite --host 127.0.0.1",
        "998300 998200 998300 03:00:00 0 1 me /bin/zsh -l",
        "998301 998300 998300 02:00:00 0 1 me npm run dev",
        "998400 1 998400 03:00:00 0 1 me npm exec vite --host 127.0.0.1 --port 5199 --strictPort",
    ]))

    def test_kept_servers_listed_once_with_owner(self):
        got = {k["pid"]: k for k in sweep.kept_servers(self.table, [], {})}
        self.assertEqual(sorted(got), [998102, 998202, 998301])  # tops of trees; the reparented 998400 is process_sweep's
        self.assertIn("Claude Code session pid 998100", got[998102]["owner"])
        self.assertIn("preview server", got[998202]["owner"])
        self.assertIn("terminal shell pid 998300", got[998301]["owner"])
        self.assertEqual(got[998102]["status"], "kept (live owner)")
        self.assertEqual(got[998202]["status"], "kept (live owner)")
        self.assertEqual(got[998301]["status"], "kept (parent running)")  # a terminal is not a Claude owner

    def test_young_orphan_is_listed_as_ask(self):
        real = sweep.owner_map
        sweep.owner_map = lambda: {998400: 997777}  # started by a Claude session that has exited
        try:
            trees = sweep.process_sweep(self.table, 24, set(), False, {})
        finally:
            sweep.owner_map = real
        t = next(t for t in trees if t["pid"] == 998400)
        self.assertEqual(t["status"], "ask owner")
        self.assertIn("only 3.0h old", t["evidence"][-1])
        sweep.owner_map = lambda: {998400: 997777}
        try:
            trees = sweep.process_sweep(self.table, 2, set(), False, {})
        finally:
            sweep.owner_map = real
        self.assertEqual(next(t for t in trees if t["pid"] == 998400)["status"], "would kill")


class WorktreeRule(unittest.TestCase):
    def test_verdict(self):
        self.assertEqual(sweep.worktree_verdict([], True), "removable")
        self.assertEqual(sweep.worktree_verdict([], False), "ask")  # pushed, clean, unmerged: owner decides
        self.assertEqual(sweep.worktree_verdict(["dirty (1)"], True), "keep")
        self.assertEqual(sweep.worktree_verdict(["live session cwd inside"], True), "keep")


class CodexJobs(Base):
    def job(self, name, status, age_s, thread=None, sid=None):
        jobs = self.tmp / "state/ws-1/jobs"
        log = touch(jobs / f"{name}.log", age_s)
        data = dict(id=name, status=status, workspaceRoot="/x/ws", summary="research", logFile=str(log), threadId=thread, sessionId=sid)
        f = jobs / f"{name}.json"
        f.write_text(json.dumps(data))
        os.utime(f, (time.time() - age_s,) * 2)
        return data

    def test_verdict(self):
        self.assertIsNone(sweep.job_verdict("completed", 999, False))
        self.assertEqual(sweep.job_verdict("running", 10, False), "running")
        self.assertEqual(sweep.job_verdict("running", 31, False), "hung")
        self.assertEqual(sweep.job_verdict("queued", 7 * 60, False), "stale")
        self.assertEqual(sweep.job_verdict("queued", 7 * 60, True), "hung")  # its Claude session is alive: not proven, ask

    def test_session_file_activity_counts_and_apply_clears_stale_only(self):
        sweep.BROKER_ROOT, sweep.CODEX_SESSIONS = self.tmp / "state", self.tmp / "sessions"
        jobs = [self.job("active", "running", 3 * 3600, thread="t-active"),  # its Codex session file is fresh
                self.job("hung", "running", 3600), self.job("stale", "running", 8 * 3600, sid="gone-session"), self.job("done", "completed", 9 * 3600)]
        touch(self.tmp / "sessions/2026/10/27/rollout-2026-10-27T10-00-00-t-active.jsonl", 60)
        (self.tmp / "state/ws-1/state.json").write_text(json.dumps(dict(version=1, jobs=jobs)))
        dry = {j["id"]: j["verdict"] for j in sweep.codex_jobs(False, 6)}
        self.assertEqual(dry, {"active": "running", "hung": "hung", "stale": "stale"})
        done = {j["id"]: j["verdict"] for j in sweep.codex_jobs(True, 6)}
        self.assertEqual(done["stale"], "cleared")
        self.assertEqual(done["hung"], "hung")
        state = {j["id"]: j["status"] for j in json.loads((self.tmp / "state/ws-1/state.json").read_text())["jobs"]}
        self.assertEqual(state, {"active": "running", "hung": "running", "stale": "failed", "done": "completed"})
        self.assertEqual(json.loads((self.tmp / "state/ws-1/jobs/stale.json").read_text())["status"], "failed")


class Bulk(Base):
    def test_verdict(self):
        self.assertEqual(sweep.bulk_verdict("/w/shots/tmp/a", 48, [], [], False, False, 24), ("candidate", []))
        self.assertEqual(sweep.bulk_verdict("/w/shots/tmp/a", 48, ["/w/shots/tmp/a/keep.png"], [], False, False, 24)[0], "keep")
        self.assertEqual(sweep.bulk_verdict("/w/shots/tmp/a", 48, ["/w/shots"], [], False, False, 24)[0], "keep")
        self.assertEqual(sweep.bulk_verdict("/w/shots/tmp/a", 48, [], ["/w/shots/tmp/a/sub"], False, False, 24)[1], ["process cwd inside"])
        self.assertEqual(sweep.bulk_verdict("/w/shots/tmp/a", 2, [], [], False, False, 24)[1], ["changed 2h ago"])
        self.assertEqual(sweep.bulk_verdict("/w/shots/tmp/a", 48, [], [], True, False, 24)[0], "keep")
        self.assertEqual(sweep.bulk_verdict("/w/shots/tmp/a", 48, [], [], False, True, 24)[0], "keep")

    def test_lists_ignored_bulk_and_protects(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
        git("init", "-q")
        (repo / ".gitignore").write_text("shots/tmp/\nnode_modules/\n*.log\n")
        touch(repo / "shots/keep.png", size=10)
        git("add", ".")
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
        for name in ("old", "fresh", "declared", "marked", "busy", "small"):
            touch(repo / f"shots/tmp/{name}/frame.png", size=2_000_000 if name != "small" else 10)
        touch(repo / "shots/tmp/marked/.sweep-keep")
        touch(repo / "node_modules/big/x.js", size=3_000_000)
        touch(repo / "logs/a.log", size=2_000_000)
        for name in ("old", "declared", "marked", "busy", "small"):
            age_tree(repo / f"shots/tmp/{name}", 3 * DAY)
        age_tree(repo / "logs", 3 * DAY)
        units = sweep.worktree_bulk([str(repo)], [str(repo / "shots/tmp/declared")], {1: str(repo / "shots/tmp/busy")},
                                    {}, "", min_mb=1, recent_hours=24)
        got = {u["rel"]: u["verdict"] for u in units}
        self.assertEqual(got, {"shots/tmp/old": "candidate", "shots/tmp/fresh": "keep", "shots/tmp/declared": "keep",
                               "shots/tmp/marked": "keep", "shots/tmp/busy": "keep", "logs": "candidate"})
        self.assertTrue((repo / "shots/tmp/old/frame.png").exists())  # listing never deletes


class Transcripts(Base):
    def test_units_respect_floor_memory_and_live(self):
        projects, codex = self.tmp / "projects", self.tmp / "codex"
        touch(projects / f"p/{U(1)}.jsonl", 10 * DAY)
        touch(projects / f"p/{U(1)}/subagents/a.jsonl", 10 * DAY)
        touch(projects / f"p/{U(2)}.jsonl", 10 * DAY)
        touch(projects / f"p/{U(2)}/tool-results/r.txt", 1 * DAY)  # a fresh part keeps the whole session
        touch(projects / f"p/{U(3)}.jsonl", 5 * DAY)  # under the 7-day floor even with days=3
        touch(projects / f"p/{U(4)}.jsonl", 10 * DAY)
        touch(projects / "p/memory/MEMORY.md", 30 * DAY)
        touch(codex / "2026/10/01/rollout-a.jsonl", 20 * DAY)
        touch(codex / "2026/10/27/rollout-b.jsonl", 0)
        age_tree(projects / f"p/{U(1)}", 10 * DAY)
        units = sweep.transcript_units(3, {U(4)}, root=projects, codex_root=codex)
        self.assertEqual(sorted((u["kind"], u["sid"]) for u in units), [("claude", U(1)), ("codex", "rollout-a.jsonl")])
        self.assertEqual(sorted(units[0]["paths"]), sorted([str(projects / f"p/{U(1)}.jsonl"), str(projects / f"p/{U(1)}")]))

    def test_archive_verifies_then_trashes_unchanged(self):
        sweep.HOME, sweep.ARCHIVE_DIR = self.tmp, self.tmp / "Archives"
        trashed = []
        sweep.trash = lambda paths: trashed.extend(paths) or []
        projects = self.tmp / "projects"
        a, b = U(1), U(2)
        touch(projects / f"p/{a}.jsonl", 10 * DAY, size=100)
        touch(projects / f"p/{a}/subagents/s.jsonl", 10 * DAY, size=100)
        touch(projects / f"p/{b}.jsonl", 10 * DAY, size=100)
        age_tree(projects / f"p/{a}", 10 * DAY)
        units = sweep.transcript_units(7, set(), root=projects, codex_root=self.tmp / "none")
        touch(projects / f"p/{b}.jsonl", 0, size=200)  # written after the scan: must be kept
        result = sweep.archive_transcripts(units, "archive")
        self.assertTrue(result["status"].startswith("done"), result)
        with tarfile.open(result["archive"]) as t:
            names = set(t.getnames())
        self.assertTrue({f"projects/p/{a}.jsonl", f"projects/p/{a}/subagents/s.jsonl"} <= names)
        self.assertEqual(sorted(trashed), sorted([str(projects / f"p/{a}.jsonl"), str(projects / f"p/{a}")]))
        self.assertEqual(result["skipped_changed"], 1)

    def test_codex_only_rollout_jsonl_files(self):
        root = self.tmp / "codex"
        for name in ("rollout-a.jsonl", "notes.jsonl", "rollout-b.txt", "rollout-c.jsonl.bak"):
            touch(root / "2026/10/01" / name, 10 * DAY)
        (root / "2026/10/01/rollout-dir.jsonl").mkdir()
        units = sweep.transcript_units(7, set(), root=self.tmp / "none", codex_root=root)
        self.assertEqual([u["sid"] for u in units], ["rollout-a.jsonl"])

    def test_archive_name_avoids_archive_and_sidecar_collisions(self):
        sweep.HOME, sweep.ARCHIVE_DIR = self.tmp, self.tmp / "Archives"
        sweep.trash = lambda paths: []
        sid = U(1)
        touch(self.tmp / f"projects/p/{sid}.jsonl", 10 * DAY, size=100)
        units = sweep.transcript_units(7, set(), root=self.tmp / "projects", codex_root=self.tmp / "none")
        sweep.ARCHIVE_DIR.mkdir()
        stem = "claude-codex-chats-2026-10-01-120000"
        sidecar = sweep.ARCHIVE_DIR / f"{stem}.index.txt"
        sidecar.write_text("preserve")
        original = time.strftime
        with mock.patch.object(sweep.time, "strftime", side_effect=lambda fmt, *a: stem.removeprefix("claude-codex-chats-")
                               if fmt == "%Y-%m-%d-%H%M%S" else original(fmt, *a)):
            first = sweep.archive_transcripts(units, "archive")
            second = sweep.archive_transcripts(units, "archive")
        self.assertEqual(sidecar.read_text(), "preserve")
        self.assertEqual(Path(first["archive"]).name, f"{stem}-1.tgz")
        self.assertEqual(Path(second["archive"]).name, f"{stem}-2.tgz")
        self.assertTrue(Path(first["archive"]).exists())

    def test_newly_live_claude_sessions_are_not_trashed(self):
        sweep.HOME = self.tmp
        a, b = U(1), U(2)
        for sid in (a, b):
            touch(self.tmp / f"projects/p/{sid}.jsonl", 10 * DAY, size=100)
        units = sweep.transcript_units(7, set(), root=self.tmp / "projects", codex_root=self.tmp / "none")
        trashed = []
        sweep.trash = lambda paths: trashed.extend(paths) or []
        sweep.live_sessions = lambda: {a: str(self.tmp)}
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": b}):
            result = sweep.archive_transcripts(units, "trash")
        self.assertEqual(result["skipped_changed"], 2)
        self.assertEqual(trashed, [])


class Keep(Base):
    def test_read_keep(self):
        (self.tmp / "lead14.txt").write_text("# lead 14\nport 5266\nport 5279  # snap\npath ~/x/shots/tmp/lead14\npid 42\nbogus line\n")
        keep = sweep.read_keep(self.tmp)
        self.assertEqual(keep["ports"], [5266, 5279])
        self.assertEqual(keep["pids"], [42])
        self.assertEqual(keep["paths"], [os.path.realpath(os.path.expanduser("~/x/shots/tmp/lead14"))])

    def test_broker_rechecks_record_and_pid_before_unlink(self):
        sweep.BROKER_ROOT = self.tmp / "brokers"
        record = sweep.BROKER_ROOT / "one/broker.json"
        record.parent.mkdir(parents=True)
        record.write_text('{"pid": 999999}')
        calls = []
        def came_alive(pid):
            calls.append(pid)
            return len(calls) == 2
        sweep.alive = came_alive
        self.assertEqual(sweep.broker_states(True)[0]["status"], "skipped (changed)")
        self.assertTrue(record.exists())
        record.write_text('{"pid": 999999}')
        def changed(pid):
            record.write_text('{"pid": 999999, "new": true}')
            return False
        sweep.alive = changed
        self.assertEqual(sweep.broker_states(True)[0]["status"], "skipped (changed)")
        self.assertTrue(record.exists())

    def test_remind_opt_in_is_silent_until_enabled(self):
        sweep.CACHE = self.tmp / "last.json"
        sweep.CACHE.write_text('{"broker_states": [{}]}')
        with mock.patch.object(sweep.subprocess, "Popen") as spawn:
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_OPTION_REMIND": "off"}), contextlib.redirect_stdout(out):
                sweep.remind(opt_in=True)
            self.assertEqual(out.getvalue(), "")
            spawn.assert_not_called()
            with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_OPTION_REMIND": "YeS"}), contextlib.redirect_stdout(out):
                sweep.remind(opt_in=True)
            self.assertIn("sweep:", out.getvalue())
            self.assertEqual(spawn.call_count, 1)
            with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_OPTION_REMIND": "off"}), contextlib.redirect_stdout(io.StringIO()):
                sweep.remind()  # the existing mode still refreshes
            self.assertEqual(spawn.call_count, 2)

    def test_stale_keeps_are_listed_with_their_file(self):  # item 8
        f = self.tmp / "lead.txt"
        f.write_text(f"pid {dead_pid()}\npid {os.getpid()}\nport 5266\nport 5279\npath {self.tmp / 'gone'}\npath {self.tmp}\n")
        entries = sweep.read_keep(self.tmp)["entries"]
        got = {(s["kind"], s["why"]) for s in sweep.stale_keeps(entries, {"5279"})}
        self.assertEqual(got, {("pid", "process gone"), ("port", "nothing listening"), ("path", "missing")})
        self.assertTrue(all(s["file"] == str(f) for s in sweep.stale_keeps(entries, {"5279"})))
        self.assertEqual({s["kind"] for s in sweep.stale_keeps(entries, None)}, {"pid", "path"})  # ports unchecked: none listed
        self.assertTrue(f.exists())  # never edited or deleted


ME = sweep.getpass.getuser()


def table(*lines):
    return sweep.parse_ps("\n".join(line.replace(" me ", f" {ME} ") for line in lines))


class ProofGaps(unittest.TestCase):
    def sweep_(self, t, **kw):
        real, owners = sweep.owner_map, kw.pop("owners", {})
        sweep.owner_map = lambda: owners
        try:
            return {x["pid"]: x for x in sweep.process_sweep(t, 24, set(), False, kw.pop("cwds", {}), **kw)}
        finally:
            sweep.owner_map = real

    orphan = table("997100 1 997100 2-00:00:00 0 1 me node /x/node_modules/.bin/vite --port 5301")

    def test_failed_lsof_is_ask_not_kill(self):  # item 1
        V = sweep.orphan_verdict
        verdict, evidence = V(None, False, 42, False, [], None, unchecked="ports/clients")
        self.assertEqual((verdict, evidence[-1]), ("ask", "could not check ports/clients"))
        got = self.sweep_(self.orphan, owners={997100: 997777}, listen=({}, False))[997100]
        self.assertEqual(got["status"], "ask owner")
        self.assertIn("could not check ports/clients", got["evidence"])
        self.assertNotIn("no process outside its tree is connected to its ports", got["evidence"])
        got = self.sweep_(self.orphan, owners={997100: 997777}, listen=({}, True), cwds_ok=False)[997100]
        self.assertIn("could not check process cwds", got["evidence"])
        self.assertEqual(self.sweep_(self.orphan, owners={997100: 997777}, listen=({}, True))[997100]["status"], "would kill")

    def test_lsof_tells_failure_from_no_match(self):  # item 1: exit 1 with empty stderr = nothing matched
        p = subprocess.Popen(["sleep", "30"])
        try:
            self.assertEqual(sweep.lsof("-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-p", str(p.pid)), ("", True))
        finally:
            p.kill()
            p.wait()
        self.assertFalse(sweep.lsof("--no-such-option")[1])

    def test_kept_path_keeps_process_trees(self):  # item 2
        t = table("997200 1 997200 2-00:00:00 0 1 me npm run dev", "997201 997200 997200 2-00:00:00 0 1 me node /x/node_modules/.bin/vite")
        got = self.sweep_(t, owners={997200: 997777}, listen=({}, True), cwds={997201: "/w/app/sub"}, keep_paths=["/w/app"])[997200]
        self.assertEqual(got["status"], "keep")
        self.assertEqual(got["evidence"], ["cwd inside kept path /w/app"])
        got = self.sweep_(t, owners={997200: 997777}, listen=({}, True), cwds={997201: "/w/other"}, keep_paths=["/w/app"])[997200]
        self.assertEqual(got["status"], "would kill")

    def test_unknown_reparented_listener_is_ask(self):  # item 9
        t = table("997300 1 997300 2-00:00:00 0 1 me /opt/homebrew/bin/redis-server *:6379",
                  "997301 1 997301 2-00:00:00 0 1 me /usr/sbin/someservice",
                  "997302 1 997302 2-00:00:00 0 1 me /opt/x/quiet-tool")
        got = self.sweep_(t, listen=({997300: {"6379"}, 997301: {"7000"}}, True))
        self.assertEqual(sorted(got), [997300])  # system executables and non-listeners are not listed
        self.assertEqual((got[997300]["status"], got[997300]["label"]), ("ask owner", "unknown kind :6379"))


class KillIdentity(Base):
    def spawn(self, ignore_term=False):
        code = "import signal,sys,time\n" + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "") + \
               "print('ready', flush=True)\ntime.sleep(30)"
        p = subprocess.Popen([sys.executable, "-I", "-c", code], stdout=subprocess.PIPE, text=True)
        p.stdout.readline()
        self.addCleanup(lambda: (p.kill(), p.wait()))
        table = sweep.parse_ps(sweep.run("ps", "-wwo", sweep.PS[2], "-p", str(p.pid)))
        if p.pid not in table:
            self.skipTest("sandbox denied ps for the spawned process")
        return p, table

    def test_parse_ps_reads_start_time(self):  # item 3
        p, t = self.spawn()
        self.assertRegex(t[p.pid]["start"], r"^\S+ \S+ \d{1,2} \d\d:\d\d:\d\d \d{4}$")
        self.assertIn("-I -c import signal", t[p.pid]["command"])
        self.assertIsNone(Ownership.table[999100]["start"])  # fixtures without lstart still parse

    def test_kills_the_scanned_process(self):
        p, t = self.spawn()
        self.assertEqual(sweep.kill_tree([p.pid], t, grace=1), "killed")
        self.assertIsNotNone(p.wait(timeout=5))

    def test_changed_or_unknown_start_skips_the_tree(self):
        p, t = self.spawn()
        self.assertEqual(sweep.kill_tree([p.pid], {**t, p.pid: dict(t[p.pid], start="Mon Jan 1 00:00:00 2001")}),
                         "skipped (process changed)")
        self.assertEqual(sweep.kill_tree([p.pid], {**t, p.pid: dict(t[p.pid], start=None)}), "skipped (process changed)")
        self.assertIsNone(p.poll())

    def test_reused_pid_is_not_signalled_again(self):
        p, t = self.spawn(ignore_term=True)  # survives SIGTERM; its pid then "belongs to another process" before SIGKILL
        real, calls = sweep.identity, []
        def identity(pid):
            calls.append(pid)
            now = real(pid)
            return now if len(calls) <= 2 else now and (now[0], "Mon Jan 1 00:00:00 2001", now[2])
        sweep.identity = identity
        sweep.kill_tree([p.pid], t, grace=.3)
        time.sleep(.2)
        self.assertIsNone(p.poll())  # SIGKILL was skipped


class JobProof(Base):
    job = CodexJobs.job

    def test_verdict_needs_a_recorded_exited_session_and_no_completed_turn(self):  # item 4
        self.assertEqual(sweep.job_verdict("running", 7 * 60, None), "hung")  # no sessionId: owner unknown
        self.assertEqual(sweep.job_verdict("running", 7 * 60, False, finished=True), "hung")
        self.assertEqual(sweep.job_verdict("running", 7 * 60, False), "stale")

    def test_no_session_is_reported_only(self):
        sweep.BROKER_ROOT, sweep.CODEX_SESSIONS = self.tmp / "state", self.tmp / "sessions"
        self.job("orphan", "running", 9 * 3600)
        self.assertEqual([j["verdict"] for j in sweep.codex_jobs(True, 6)], ["hung"])
        self.assertEqual(json.loads((self.tmp / "state/ws-1/jobs/orphan.json").read_text())["status"], "running")

    def test_rechecks_right_before_writing(self):
        sweep.BROKER_ROOT, sweep.CODEX_SESSIONS = self.tmp / "state", self.tmp / "sessions"
        self.job("late", "running", 9 * 3600, sid="gone-session")
        real, calls = sweep.job_activity, []
        def activity(f):
            calls.append(f)
            if len(calls) == 2:
                touch(self.tmp / "state/ws-1/jobs/late.log", 0)  # Codex wrote between the scan and the write
            return real(f)
        sweep.job_activity = activity
        self.assertEqual([j["verdict"] for j in sweep.codex_jobs(True, 6)], ["skipped (changed)"])
        self.assertEqual(json.loads((self.tmp / "state/ws-1/jobs/late.json").read_text())["status"], "running")


class TranscriptProof(Base):
    def test_only_uuid_sessions(self):  # item 5
        projects = self.tmp / "projects"
        for rel in (f"p/{U(1)}.jsonl", f"p/{U(1)}/tool-results/r.txt", "p/notes.jsonl", "p/README.md", "p/stuff/x.txt",
                    f"p/{U(2)}/subagents/a.jsonl"):  # U(2): a folder without its session file
            touch(projects / rel, 10 * DAY)
        age_tree(projects / "p", 10 * DAY)
        units = sweep.transcript_units(7, set(), root=projects, codex_root=self.tmp / "none")
        self.assertEqual([(u["sid"], sorted(map(os.path.basename, u["paths"]))) for u in units], [(U(1), [U(1), f"{U(1)}.jsonl"])])

    def test_verify_archive_checks_content(self):  # item 6
        sweep.HOME = self.tmp
        for n in ("a", "b", "c"):
            touch(self.tmp / f"d/{n}.jsonl", size=100)
        tgz = self.tmp / "t.tgz"
        subprocess.run(["tar", "-czf", str(tgz), "d/a.jsonl", "d/b.jsonl"], cwd=self.tmp, check=True)
        names = {"d/a.jsonl", "d/b.jsonl", "d/c.jsonl"}
        st = os.stat(self.tmp / "d/a.jsonl")
        (self.tmp / "d/a.jsonl").write_bytes(b"y" * 100)  # same size and mtime, other content
        os.utime(self.tmp / "d/a.jsonl", ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(sorted(sweep.verify_archive(tgz, names)), ["d/a.jsonl", "d/c.jsonl"])  # c: not archived
        self.assertEqual(sweep.verify_archive(tgz, {"d/b.jsonl"}), [])

    def test_archive_mismatch_trashes_nothing(self):
        sweep.HOME, sweep.ARCHIVE_DIR = self.tmp, self.tmp / "Archives"
        trashed = []
        sweep.trash = lambda paths: trashed.extend(paths) or []
        touch(self.tmp / f"projects/p/{U(1)}.jsonl", 10 * DAY, size=100)
        units = sweep.transcript_units(7, set(), root=self.tmp / "projects", codex_root=self.tmp / "none")
        sweep.verify_archive = lambda tgz, names: sorted(names)  # archived copies don't match unchanged originals
        result = sweep.archive_transcripts(units, "archive")
        self.assertTrue(result["status"].startswith("stopped"), result)
        self.assertEqual(trashed, [])


class Scratch(Base):
    def test_session_scratch_rules(self):  # item 7
        root, sweep.PROJECTS = self.tmp / "claude-501", self.tmp / "projects"
        proj = root / "-Users-me-proj"
        names = dict(old=U(1), live=U(2), repo=U(3), fresh=U(4), busy=U(5), marked=U(6), declared=U(7), talked=U(8))
        for n, sid in names.items():
            touch(proj / sid / "scratchpad/out.png", size=1000)
        touch(proj / names["marked"] / "scratchpad/.sweep-keep")
        repo = proj / names["repo"] / "scratchpad/wt"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        touch(repo / "dirty.txt")
        touch(sweep.PROJECTS / f"-Users-me-proj/{names['talked']}.jsonl", 1 * DAY)  # its transcript was written yesterday
        touch(root / "bash-edit-diff/x/a.diff", size=10)
        touch(root / "loose.json", size=10)
        touch(proj / "not-a-session/x", size=10)
        for n, sid in names.items():
            if n != "fresh":
                age_tree(proj / sid, 5 * DAY)
        rows = sweep.session_scratch({names["live"]}, {1: str(proj / names["busy"] / "scratchpad")}, [str(proj / names["declared"])], root=root)
        got = {r.get("sid") or os.path.basename(r["path"].split(" ")[0]): r for r in rows}
        verdicts = {n: got[sid]["verdict"] for n, sid in names.items()}
        self.assertEqual(verdicts, dict(old="candidate", live="keep", repo="keep", fresh="keep", busy="keep", marked="keep",
                                        declared="keep", talked="keep"))
        self.assertIn("session live", got[names["live"]]["reasons"])
        self.assertTrue(any(r.startswith("git repo inside: scratchpad/wt (dirty 1)") for r in got[names["repo"]]["reasons"]))
        self.assertTrue(any(r.startswith("transcript written 1.0d") for r in got[names["talked"]]["reasons"]))
        self.assertEqual({got[k]["verdict"] for k in ("bash-edit-diff", "not-a-session", "*")}, {"list only"})
        self.assertTrue((proj / names["old"] / "scratchpad/out.png").exists())  # listing never deletes

    def test_storage_splits_scratch(self):  # item 15
        names = [n for n, _ in sweep.STORAGE]
        self.assertIn("app scratch workspaces", names)
        self.assertIn(f"session scratch ({sweep.SCRATCH})", names)
        self.assertNotIn("scratch", names)


class RepoScope(Base):
    def git(self, *a, cwd=None):
        return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main", *a],
                              cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    def repo(self, path):
        self.git("init", "-q", str(path))
        touch(path / "a.txt")
        self.git("add", ".", cwd=path)
        self.git("commit", "-qm", "init", cwd=path)
        return path

    def test_finds_plain_repos_and_live_session_repos(self):  # item 10
        base = self.tmp / "dev"
        plain = self.repo(base / "plain")
        self.repo(base / "a/b/c/too-deep")
        elsewhere = self.repo(self.tmp / "elsewhere/proj")
        self.git("worktree", "add", "-q", "--detach", str(self.tmp / "wt"), cwd=elsewhere)
        found = sweep.repos([str(self.tmp / "wt"), "", str(self.tmp)], bases=(base,))
        self.assertEqual([p.resolve() for p in found], [plain.resolve(), elsewhere.resolve()])

    def test_removable_needs_origin_head(self):  # item 12
        origin = self.repo(self.tmp / "origin")
        self.git("clone", "-q", str(origin), str(self.tmp / "clone"))
        clone = self.tmp / "clone"
        self.assertEqual(sweep.base_ref(clone), "origin/main")
        self.git("worktree", "add", "-q", "--detach", str(self.tmp / "wt"), cwd=clone)
        got = sweep.worktrees([clone], 0, False, False, False, {})[0]
        self.assertEqual(got[0]["verdict"], "removable")
        self.git("remote", "set-head", "origin", "-d", cwd=clone)  # no origin/HEAD: no fallback to a local main
        self.assertEqual(sweep.base_ref(clone), "")
        got = sweep.worktrees([clone], 0, False, False, False, {})[0]
        self.assertEqual((got[0]["verdict"], got[0]["reasons"][0][:16]), ("ask", "no origin/HEAD ("))
        self.assertEqual(sweep.base_ref(self.repo(self.tmp / "local")), "")  # local main only

    def test_worktree_keep_declarations_and_final_recheck(self):
        self.git("clone", "-q", str(self.repo(self.tmp / "origin")), str(self.tmp / "clone"))
        clone, wt = self.tmp / "clone", self.tmp / "wt"
        self.git("worktree", "add", "-q", "--detach", str(wt), cwd=clone)
        row = lambda **kw: sweep.worktrees([clone], 0, False, False, False, {}, **kw)[0][0]
        self.assertIn("keep declaration", row(keep_paths=[str(wt / "nested")])["reasons"])
        self.assertEqual(row(keep_paths=[str(self.tmp)])["verdict"], "keep")
        touch(wt / ".sweep-keep")
        self.assertEqual(row()["verdict"], "keep")
        (wt / ".sweep-keep").unlink()
        sweep.live_sessions = lambda: {}
        def cwd_after_scan():
            touch(wt / "new.txt")  # change after the first clean status
            return {os.getpid(): str(clone)}
        sweep.cwd_map = cwd_after_scan
        got = sweep.worktrees([clone], 0, False, True, True, {})[0][0]
        self.assertEqual(got["verdict"], "skipped (changed)")
        self.assertTrue(wt.is_dir())
        (wt / "new.txt").unlink()
        sweep.cwd_map = lambda: {os.getpid(): str(clone), 123: str(wt / "nested")}
        self.assertEqual(sweep.worktrees([clone], 0, False, True, True, {})[0][0]["verdict"], "skipped (changed)")
        sweep.cwd_map = lambda: {os.getpid(): str(clone)}
        sweep.live_sessions = lambda: {U(1): str(wt / "nested")}
        self.assertEqual(sweep.worktrees([clone], 0, False, True, True, {})[0][0]["verdict"], "skipped (changed)")

    def test_failed_cwd_lookup_blocks_removable_and_candidate(self):  # unmeasured cwds prove nothing
        self.git("clone", "-q", str(self.repo(self.tmp / "origin")), str(self.tmp / "clone"))
        self.git("worktree", "add", "-q", "--detach", str(self.tmp / "wt"), cwd=self.tmp / "clone")
        self.assertEqual(sweep.worktrees([self.tmp / "clone"], 0, False, False, False, {})[0][0]["verdict"], "removable")
        got = sweep.worktrees([self.tmp / "clone"], 0, False, False, False, {}, cwds_ok=False)[0][0]
        self.assertEqual((got["verdict"], got["reasons"]), ("keep", ["could not check process cwds"]))
        self.assertEqual(sweep.bulk_verdict("/w/shots/tmp/a", 48, [], [], False, False, 24, cwds_ok=False),
                         ("keep", ["could not check process cwds"]))
        sweep.PROJECTS = self.tmp / "projects"
        touch(self.tmp / f"scratch/-Users-me-p/{U(1)}/scratchpad/a.png", size=10)
        age_tree(self.tmp / "scratch", 5 * DAY)
        self.assertEqual(sweep.session_scratch(set(), {}, [], root=self.tmp / "scratch")[0]["verdict"], "candidate")
        got = sweep.session_scratch(set(), {}, [], root=self.tmp / "scratch", cwds_ok=False)[0]
        self.assertEqual((got["verdict"], got["reasons"]), ("keep", ["could not check process cwds"]))

    def test_newly_found_repos_are_not_pruned(self):  # item 10: wider discovery stays list only
        repo = self.repo(self.tmp / "plain")
        self.git("worktree", "add", "-q", "--detach", str(self.tmp / "gone"), cwd=repo)
        for d, dirs, files in os.walk(self.tmp / "gone", topdown=False):
            for n in files:
                os.unlink(os.path.join(d, n))
            for n in dirs:
                os.rmdir(os.path.join(d, n))
        os.rmdir(self.tmp / "gone")
        extras = sweep.worktrees([repo], 0, False, True, True, {})[1]
        self.assertEqual([e["kind"] for e in extras], ["prunable"])
        self.assertIn("prunable", self.git("worktree", "list", "--porcelain", cwd=repo))  # record kept


if __name__ == "__main__":
    unittest.main()
