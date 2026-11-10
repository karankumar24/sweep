#!/usr/bin/env python3
"""Inspect stale Claude/Codex leftovers; change anything only with --apply."""
import argparse
import datetime as dt
import getpass
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
HOME = Path.home()
STATE = HOME / ".claude/sweep"  # outside the install folder, which a plugin update replaces
CACHE = STATE / "last.json"
KEEP_DIR = STATE / "keep"  # sessions declare needs here: lines "path /abs", "port 5266", "pid 123"
BROKER_ROOT = HOME / ".claude/plugins/data/codex-openai-codex/state"
PROJECTS = HOME / ".claude/projects"
CODEX_SESSIONS = HOME / ".codex/sessions"
APP = HOME / "Library/Application Support/Claude"
ARCHIVE_DIR = HOME / "Archives/claude-chats"
SCRATCH = Path("/private/tmp") / f"claude-{os.getuid()}"  # Claude Code session scratch: <project>/<session-id>/
# Where to look for git repos (to depth 3), besides the repos live sessions are in. SWEEP_SCAN_DIRS=dir1:dir2 replaces these.
SCAN_BASES = tuple(Path(os.path.expanduser(d)) for d in os.environ.get("SWEEP_SCAN_DIRS", "").split(":") if d) or tuple(
    HOME / d for d in ("dev", "code", "projects", "src", "Desktop", "Downloads"))
MIN_TRANSCRIPT_DAYS = 7
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
PS = ("ps", "-axwwo", "pid=,ppid=,pgid=,etime=,%cpu=,rss=,user=,lstart=,command=")
LSTART = re.compile(r"(\S+ \S+ +\d{1,2} \d\d:\d\d:\d\d \d{4}) +(.*)")  # ps lstart, e.g. "Thu Oct 29 16:14:18 2026"
# Local counterpart of the app's Storage panel (get_storage_usage). The Cowork VM (vm_bundles, claude-code-vm) is never scanned.
STORAGE = (("conversation history", [PROJECTS]),
           ("other Claude Code history", [HOME / ".claude" / n for n in ("file-history", "shell-snapshots", "paste-cache", "session-env",
                                                                  "todos", "debug", "telemetry", "history.jsonl", "backups")]),
           ("Codex sessions", [CODEX_SESSIONS]),
           ("Codex logs and DBs", [HOME / ".codex/log", *map(Path, glob.glob(str(HOME / ".codex/logs_*.sqlite*")))]),
           ("app scratch workspaces", [APP / "scratch-workspaces"]),
           (f"session scratch ({SCRATCH})", [SCRATCH]),
           ("Claude Code versions", [APP / "claude-code"]),
           ("app web cache", [APP / n for n in ("Cache", "Code Cache", "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache",
                                               "GPUPersistentCache", "GraphiteDawnCache")]
            + [HOME / "Library/Caches/com.anthropic.claudefordesktop", HOME / "Library/Caches/com.anthropic.claudefordesktop.ShipIt"]),
           ("app logs", [HOME / "Library/Logs/Claude"]))
BUILD_DIRS = {"node_modules", ".next", "dist", "build", ".turbo", ".vite", ".cache", "coverage", ".DS_Store", "out"}
EXCLUDED_EXE = ("/System/", "/usr/libexec/", "/usr/sbin/", "/Applications/", "/Library/")
AGENT = re.compile(r"app-server-broker\.mjs\s+serve\b|(?:\bcodex\s+app-server\b)|codex-code-mode-host\b|codex-companion\.mjs\s+task-worker\b")
DEV = re.compile(r"(?:\b(?:npm|pnpm|yarn|bun|npx)(?:\s+exec)?\s+(?:run\s+)?(?:dev|start|preview|vite)\b|\b(?:vite|next)\s+(?:dev|start|preview)\b|node_modules/\.bin/vite\b|\besbuild\b.*--service\b|\btsc\s+--watch\b|\bvitest\b|\bturbo\b|\bpython(?:3(?:\.\d+)?)?\s+-m\s+http\.server\b|\bplaywright\b|chrome-headless-shell\b|Google Chrome for Testing\b)")
# A live owner, matched on the executable (never on arguments): the Claude app or a Claude Code CLI.
CLAUDE_OWNER = re.compile(r"^(?:/Applications/Claude\.app/|/(?:[^ ]| (?!-))*?/MacOS/claude(?:\s|$)|(?:\S*/)?claude(?:\s|$))")
SNAPSHOT_SHELL = "/.claude/shell-snapshots/"  # the shell a Claude Code Bash call runs in
BLENDER_BG = re.compile(r"/[Bb]lender(?:\.app/Contents/MacOS/[Bb]lender)?\s(?:.*\s)?(?:-b|--background)(?:\s|$)")

def run(*args):
    try:
        return subprocess.run(args, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, check=False).stdout
    except (OSError, ValueError):
        return ""
def lsof(*args):
    """lsof's output and whether it ran: exit 1 with nothing on stderr only means nothing matched."""
    try:
        r = subprocess.run(("lsof", *args), text=True, capture_output=True, check=False)
    except (OSError, ValueError):
        return "", False
    return r.stdout, r.returncode == 0 or (r.returncode == 1 and not r.stderr.strip())

def etime_seconds(s):
    days, sep, rest = s.partition("-")
    parts = [int(x) for x in (rest if sep else s).split(":")]
    if not 2 <= len(parts) <= 3:
        raise ValueError(s)
    return (int(days) * 86400 if sep else 0) + sum(v * 60 ** i for i, v in enumerate(reversed(parts)))

def parse_ps(raw):
    out = {}
    for line in raw.splitlines():
        p = line.strip().split(None, 7)
        if len(p) != 8:
            continue
        m = LSTART.match(p[7])  # start time, when ps was asked for lstart: with the pid it identifies the process
        try:
            pid, ppid, pgid = map(int, p[:3])
            out[pid] = dict(pid=pid, ppid=ppid, pgid=pgid, age=etime_seconds(p[3]), cpu=float(p[4]), rss_kb=int(p[5]), user=p[6],
                            start=" ".join(m.group(1).split()) if m else None, command=m.group(2) if m else p[7])
        except ValueError:
            continue
    return out

def candidate(command):
    exe = command.split(None, 1)[0] if command.strip() else ""
    if BLENDER_BG.search(command):  # headless render; the interactive Blender (MCP) has no -b
        return True
    return bool(exe and not exe.startswith(EXCLUDED_EXE)
                and "Claude.app/" not in command and "Claude Helper" not in command
                and (AGENT.search(command) or DEV.search(command)))

def descendants(root, table):
    children = {}
    for p in table.values():
        children.setdefault(p["ppid"], []).append(p["pid"])
    result = []
    def visit(pid):
        for child in children.get(pid, []):
            visit(child)
        result.append(pid)
    visit(root)
    return result

def protected_pids(table, keep):
    protected = {0, 1, *keep}
    pid = os.getpid()
    while pid > 1 and pid not in protected:
        protected.add(pid)
        pid = table.get(pid, {}).get("ppid", 1)
    for pid, p in table.items():
        if "Claude.app/" in p["command"] or "Claude Helper" in p["command"]:
            protected.add(pid)
    return protected

def cwd_map():
    raw = run("lsof", "-a", "-d", "cwd", "-Fpn", "-u", getpass.getuser())  # -a: AND, or every open file is listed too
    found, pid = {}, None
    for line in raw.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            found[pid] = line[1:]
    return found

def listeners():
    """({pid: {ports}} of this user's listening TCP sockets, whether lsof could check)."""
    raw, ok = lsof("-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn")
    found, pid = {}, None
    for line in raw.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            found.setdefault(pid, set()).add(line.rsplit(":", 1)[-1])
    return found, ok
def ports(pids, listen):
    return sorted({port for pid in pids for port in listen.get(pid, ())}, key=int)

def label(command, listening):
    if "app-server-broker.mjs" in command:
        return "codex broker"
    if "codex-companion.mjs" in command:
        return "codex task worker"
    if BLENDER_BG.search(command):
        return "blender -b"
    if not candidate(command):
        return "unknown kind" + (" :" + ",".join(listening) if listening else "")
    name = next((n for n in ("vite", "next", "esbuild", "tsc", "vitest", "turbo", "http.server",
                            "playwright", "chrome-headless-shell", "Google Chrome for Testing") if n in command), "dev server")
    return name + (" :" + ",".join(listening) if listening else "")

def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

def chain(pid, table):
    """pid and its ancestors, nearest first, up to (not including) launchd."""
    out = []
    while pid > 1 and pid in table and pid not in out:
        out.append(pid)
        pid = table[pid]["ppid"]
    return out
def env_owner(pid):
    """CLAUDE_PID from the process environment: the Claude Code session that started it (ps -E, own processes only)."""
    m = re.search(r"\bCLAUDE_PID=(\d+)", run("ps", "-E", "-ww", "-o", "command=", "-p", str(pid)))
    return int(m.group(1)) if m else None
def owner_map():
    """{pid: CLAUDE_PID} for every own process whose environment names the Claude Code session that started it."""
    found = {}
    for line in run("ps", "-axwwE", "-o", "pid=,command=", "-U", getpass.getuser()).splitlines():
        m = re.search(r"\bCLAUDE_PID=(\d+)", line)
        pid = line.split(None, 1)[0] if line.strip() else ""
        if m and pid.isdigit():
            found[int(pid)] = int(m.group(1))
    return found
def tcp_clients():
    """({port: {pids}} of local ESTABLISHED TCP connections by the remote (server) port, whether lsof could check)."""
    found, pid = {}, None
    raw, ok = lsof("-nP", "-iTCP", "-sTCP:ESTABLISHED", "-Fpn")
    for line in raw.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and "->" in line and pid is not None:
            found.setdefault(line.rsplit(":", 1)[-1], set()).add(pid)
    return found, ok
def orphan_verdict(live_owner, kept, owner, owner_alive, clients, session_cwd, parent=1, known=True, unchecked=""):
    """keep / ask / orphaned, with the evidence. Only 'orphaned' is ever killed without the owner's yes."""
    if live_owner:
        return "keep", [f"live owner in its parent chain: pid {live_owner}"]
    if kept:
        return "keep", [kept if isinstance(kept, str) else "declared in a keep file or --keep"]
    if owner and owner_alive:
        return "keep", [f"started by Claude session pid {owner}, still running"]
    if clients:
        return "keep", ["its port is in use by pid " + ", ".join(map(str, clients))]
    if parent != 1:
        return "ask", [f"parent pid {parent} is alive but is not a Claude session"]
    evidence = ["reparented to launchd (ppid 1): its parent exited"]
    if unchecked:  # a failed lsof proves nothing
        return "ask", evidence + [f"could not check {unchecked}"]
    evidence.append("no process outside its tree is connected to its ports")
    if session_cwd:
        return "ask", evidence + [f"cwd is inside live session folder {session_cwd}"]
    if not owner:
        return "ask", evidence + ["no CLAUDE_PID in its environment: owner unknown"]
    evidence.append(f"the Claude session that started it (pid {owner}) has exited")
    if not known:  # a kind of leftover sweep has no rule for: never killed without the owner's yes
        return "ask", evidence + ["unknown kind of process: no rule says it is safe to kill"]
    return "orphaned", evidence
def assess(pid, table, members=(), listening=(), conns=None, keep=None, keep_ports=(), sessions=None, cwd=None,
           owners=None, known=True, keep_paths=(), member_cwds=(), ports_ok=True, cwds_ok=True):
    keep = keep or set()
    ch = chain(pid, table)
    live = next((a for a in ch[1:] if CLAUDE_OWNER.search(table[a]["command"]) or a in keep), None)  # ancestors only
    owner = owners.get(pid) if owners is not None else env_owner(pid)
    owner_alive = owner in table and bool(CLAUDE_OWNER.search(table[owner]["command"]))
    members = set(members) or {pid}
    clients = sorted({c for port in listening for c in (conns or {}).get(port, ()) if c not in members})
    session_cwd = next((s for s in (sessions or {}).values() for c in (cwd, *member_cwds)  # any member, not just the root
                        if s and c and (inside(c, s) or inside(s, c))), None)
    kept_path = next((k for k in keep_paths for c in member_cwds if c and inside(c, k)), None)
    kept = ("declared in a keep file or --keep" if members & keep or set(listening) & set(map(str, keep_ports))
            else f"cwd inside kept path {kept_path}" if kept_path else "")
    unchecked = " and ".join(n for n, ok in (("ports/clients", ports_ok), ("process cwds", cwds_ok)) if not ok)
    verdict, evidence = orphan_verdict(live, kept, owner, owner_alive, clients, session_cwd, table[pid]["ppid"], known, unchecked)
    return dict(verdict=verdict, evidence=evidence, owner_pid=owner, clients=clients,
                chain=[f"{a} {table[a]['command'][:100]}" for a in ch] + ["1 launchd"])
def identity(pid):
    """(ppid, start time, command) of pid now, from a fresh ps; None once it has exited."""
    p = parse_ps(run("ps", "-wwo", PS[2], "-p", str(pid))).get(pid)
    return p and (p["ppid"], p["start"], p["command"])
def kill_tree(members, table, grace=3):
    """SIGTERM, then SIGKILL after `grace` s. Right before each signal a member's ppid, start time and command must still
    match the scan, or it is skipped: its pid may have been reused."""
    want = {n: (table[n]["ppid"], table[n].get("start"), table[n]["command"]) for n in members}
    if any(want[n][1] is None or identity(n) != want[n] for n in members):
        return "skipped (process changed)"
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for n in members:
            if identity(n) == want[n]:
                try:
                    os.kill(n, sig)
                except (ProcessLookupError, PermissionError):
                    pass
        if sig == signal.SIGTERM:
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline and any(alive(n) for n in members):
                time.sleep(.1)
    left = [n for n in members if (now := identity(n)) and now[1:] == want[n][1:]]  # same process, even if reparented
    return "failed" if left else "killed"
def process_sweep(table, hours, keep, apply, cwds, keep_ports=(), sessions=None, approved=(), keep_paths=(), listen=None,
                  cwds_ok=True):
    """Trees whose root was reparented to launchd and that run dev servers, browsers, Blender -b or Codex, or listen on TCP.
    --apply kills only proven orphans, plus 'ask' trees the owner approved by root pid (--kill)."""
    protected = protected_pids(table, keep)
    listen, listen_ok = listen or listeners()
    trees, used, conns, owners = [], set(), None, owner_map()
    for root, p in sorted(table.items()):
        if (root in used or root in protected or root <= 1 or p["ppid"] != 1
                or p["user"] != getpass.getuser()):
            continue
        members = descendants(root, table)
        known = candidate(p["command"]) or (SNAPSHOT_SHELL in p["command"] and any(candidate(table[m]["command"]) for m in members))
        owner = owners.get(root)
        listening = ports(members, listen)
        if not known and not (owner and not (owner in table and CLAUDE_OWNER.search(table[owner]["command"]))) and not (
                listening and not p["command"].startswith(EXCLUDED_EXE) and not CLAUDE_OWNER.search(p["command"])):
            continue  # unknown kinds are listed only when an exited Claude session started them or they listen on TCP
        used.update(members)
        if listening and conns is None:
            conns = tcp_clients()
        item = dict(pid=root, pids=members, label=label(p["command"], listening), command=p["command"][:160],
                    age_hours=round(p["age"] / 3600, 1), cwd=cwds.get(root), ports=listening,
                    cpu=round(sum(table[n]["cpu"] for n in members), 1),
                    rss_mb=round(sum(table[n]["rss_kb"] for n in members) / 1024, 1),
                    **assess(root, table, members, listening, conns and conns[0], protected, keep_ports, sessions, cwds.get(root),
                             owners, known, keep_paths, [cwds.get(m) for m in members],
                             listen_ok and (not listening or conns[1]), cwds_ok))
        if item["verdict"] == "orphaned" and p["age"] < hours * 3600:  # listed, but too young to kill without a yes
            item["verdict"] = "ask"
            item["evidence"] = item["evidence"] + [f"only {item['age_hours']}h old: proven orphans are killed after {hours:g}h"]
        go = item["verdict"] == "orphaned" or (item["verdict"] == "ask" and root in approved)
        item["status"] = {"orphaned": "would kill", "ask": "ask owner", "keep": "keep"}[item["verdict"]]
        if apply and go:
            print(f"killing {item['label']} pid {root} ({len(members)} procs): {item['command']}\n"
                  f"  chain: {' <- '.join(item['chain'])}\n  why: {'; '.join(item['evidence'])}"
                  + ("; approved by owner" if item["verdict"] == "ask" else ""), file=sys.stderr, flush=True)
            item["status"] = kill_tree(members, table)
        trees.append(item)
    return trees
def owner_desc(pid, table):
    """Who keeps a process alive: the nearest Claude Code session, the Claude app (previews, terminal panel) or another parent."""
    ch = chain(pid, table)[1:]
    for a in ch:
        cmd = table[a]["command"]
        if re.search(r"/MacOS/claude(?:\s|$)|(?:^|/)claude\s", cmd) and "Claude.app/" not in cmd.split(None, 1)[0]:
            return f"Claude Code session pid {a}"
    for a in ch:
        if re.match(r"/bin/(?:ba|z)?sh -l\b", table[a]["command"]):
            return f"terminal shell pid {a} (Claude app terminal panel or Terminal)"
    for a in ch:
        if "Claude.app/" in table[a]["command"]:
            return f"Claude app pid {a} (preview server)"
    return f"parent pid {ch[0]} {table[ch[0]]['command'][:60]}" if ch else "launchd"
def kept_servers(table, trees, cwds, listen=None):
    """Dev servers that live owners keep (never reparented, so process_sweep skips them): listed, never killed without a yes."""
    seen, listen = {n for t in trees for n in t["pids"]}, (listen or listeners())[0]
    out = []
    for pid, p in sorted(table.items()):
        if (pid in seen or p["ppid"] == 1 or p["user"] != getpass.getuser() or SNAPSHOT_SHELL in p["command"]
                or not DEV.search(p["command"]) or not candidate(p["command"])):
            continue  # a Claude Bash call's wrapper shell is not the server; its child is
        parent = table.get(p["ppid"], {}).get("command", "")
        if DEV.search(parent) and candidate(parent) and SNAPSHOT_SHELL not in parent:
            continue  # list the top of a dev-server tree once (npm run dev, not its node vite)
        members = descendants(pid, table)
        seen.update(members)
        listening = ports(members, listen)
        owner = owner_desc(pid, table)
        out.append(dict(pid=pid, pids=members, label=label(p["command"], listening), command=p["command"][:160], ports=listening,
                        age_hours=round(p["age"] / 3600, 1), cwd=cwds.get(pid), owner=owner,
                        rss_mb=round(sum(table[n]["rss_kb"] for n in members) / 1024, 1),
                        status="kept (live owner)" if owner.startswith("Claude") else "kept (parent running)"))
    return out
def explain(pid, table, cwds, sessions, keep):
    members = descendants(pid, table)
    listen, listen_ok = listeners()
    listening = ports(members, listen)
    conns, conns_ok = tcp_clients() if listening else ({}, True)
    return dict(pid=pid, command=table[pid]["command"][:160], descendants=len(members) - 1, ports=listening, cwd=cwds.get(pid),
                **assess(pid, table, members, listening, conns, protected_pids(table, set(keep["pids"])), keep["ports"], sessions,
                         cwds.get(pid), known=candidate(table[pid]["command"]), keep_paths=keep["paths"],
                         member_cwds=[cwds.get(m) for m in members], ports_ok=listen_ok and conns_ok, cwds_ok=os.getpid() in cwds))
def broker_states(apply):
    result = []
    for path in BROKER_ROOT.glob("*/broker.json"):
        try:
            original = path.read_text()
            pid = int(json.loads(original).get("pid"))
            if pid > 0 and alive(pid):
                continue
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        item = dict(path=str(path), pid=pid, status="stale")
        if apply:
            try:
                current = path.read_text()
                if current != original or (pid > 0 and alive(pid)):
                    item["status"] = "skipped (changed)"
                else:
                    path.unlink()
                    item["status"] = "deleted"
            except OSError:
                item["status"] = "skipped (changed)" if not path.exists() else "failed"
        result.append(item)
    return result

def repos(cwds=(), bases=SCAN_BASES):
    """Main checkouts of git repos under bases (to depth 3) and of the repos holding the given cwds (live sessions')."""
    found = []
    for base in bases:
        if not base.is_dir():
            continue
        for root, dirs, _ in os.walk(base):
            depth = len(Path(root).relative_to(base).parts)
            if (Path(root) / ".git").is_dir():
                found.append(Path(root))
            dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", ".claude")]
            if depth >= 3:
                dirs[:] = []
    for cwd in cwds:
        common = git("-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir") if cwd and os.path.isdir(cwd) else ""
        if common.endswith("/.git"):
            found.append(Path(common).parent)
    return list(dict.fromkeys(found))

def worktree_records(raw):
    for block in raw.strip().split("\n\n"):
        fields = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            fields[key] = value
        if fields.get("worktree"):
            yield fields

def git(*args):
    return run("git", *map(str, args)).strip()
def activity(path):
    times = []
    try:
        dotgit = path / ".git"
        if dotgit.is_file():
            target = dotgit.read_text().strip().removeprefix("gitdir: ")
            gitdir = (path / target).resolve()
            times.extend(p.stat().st_mtime for p in (gitdir / "index", gitdir / "HEAD") if p.exists())
        times.extend(p.stat().st_mtime for p in path.iterdir() if p.name != ".git")
    except OSError:
        pass
    return max(times, default=0)
def inside(cwd, path):
    return cwd == str(path) or cwd.startswith(str(path) + os.sep)
def worktree_verdict(reasons, merged):
    """removable only when nothing else holds it and HEAD is merged into the base branch;
    pushed but unmerged is the owner's call (default keep)."""
    if reasons:
        return "keep"
    return "removable" if merged else "ask"
def base_ref(repo):
    """The remote base branch origin/HEAD points at, verified; '' without one (then nothing there is 'removable')."""
    ref = git("-C", repo, "rev-parse", "--abbrev-ref", "origin/HEAD")
    return ref if ref.startswith("origin/") and ref != "origin/HEAD" and git("-C", repo, "rev-parse", "--verify", "-q", ref) else ""
def worktrees(found, days, sizes, apply, remove, cwds, sessions=None, cwds_ok=True, keep_paths=()):
    entries, extras = [], []
    now = time.time()
    for repo in found:
        registered = set()
        base = base_ref(repo)
        for rec in worktree_records(run("git", "-C", str(repo), "worktree", "list", "--porcelain")):
            path = Path(rec["worktree"])
            if path.resolve() == repo.resolve():
                continue
            registered.add(str(path.resolve()))
            if "prunable" in rec:
                extras.append(dict(path=str(path), kind="prunable", detail=rec["prunable"]))
                continue
            dirty = len(git("-C", path, "status", "--porcelain", "--untracked-files=all").splitlines())
            unpushed = git("-C", path, "rev-list", "--count", "HEAD", "--not", "--remotes")
            pushed = unpushed == "0"
            merged = bool(base) and subprocess.run(("git", "-C", str(path), "merge-base", "--is-ancestor", "HEAD", base),
                                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
            stamp = git("-C", path, "show", "-s", "--format=%ct", "HEAD")
            commit_days = round((now - int(stamp)) / 86400, 1) if stamp.isdigit() else None
            idle_days = round((now - activity(path)) / 86400, 1)
            active = [pid for pid, cwd in cwds.items() if inside(cwd, path)]
            reasons = ([f"dirty ({dirty})"] if dirty else []) + ([] if pushed else [f"{unpushed or '?'} commits on no remote"])
            ignored = sorted({l[3:].split("/")[0] for l in git("-C", path, "status", "--porcelain", "--ignored").splitlines()
                              if l.startswith("!! ") and l[3:].split("/")[0] not in BUILD_DIRS})
            if ignored:
                reasons.append("ignored files: " + ", ".join(ignored[:5]))
            if active:
                reasons.append("process cwd inside")
            if not cwds_ok:  # a failed lsof proves nothing
                reasons.append("could not check process cwds")
            if any(c and inside(c, str(path)) for c in (sessions or {}).values()):
                reasons.append("live session cwd inside")
            if (path / ".sweep-keep").exists() or any(inside(os.path.realpath(path), os.path.realpath(k))
                                                      or inside(os.path.realpath(k), os.path.realpath(path)) for k in keep_paths):
                reasons.append("keep declaration")
            if idle_days < days:
                reasons.append("recent activity")
            item = dict(path=str(path), repo=str(repo), branch=rec.get("branch", "detached").removeprefix("refs/heads/"),
                        dirty=dirty, pushed=pushed, commit_days=commit_days, idle_days=idle_days,
                        merged=merged, base=base, active_pids=active, verdict=worktree_verdict(reasons, merged),
                        reasons=reasons or ([] if merged else [f"pushed but not merged into {base}: owner decides" if base else
                                                               "no origin/HEAD (git remote set-head origin -a): owner decides"]))
            if sizes:
                size = run("du", "-sk", str(path)).split()
                item["size_mb"] = round(int(size[0]) / 1024, 1) if size and size[0].isdigit() else None
            if apply and remove and item["verdict"] == "removable":
                fresh_cwds, fresh_sessions = cwd_map(), live_sessions()
                status = subprocess.run(("git", "-C", str(path), "status", "--porcelain", "--untracked-files=all"),
                                        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                if (status.returncode or status.stdout or os.getpid() not in fresh_cwds
                        or any(inside(os.path.realpath(c), os.path.realpath(path)) for c in fresh_cwds.values())
                        or any(c and inside(os.path.realpath(c), os.path.realpath(path)) for c in fresh_sessions.values())):
                    item["verdict"] = "skipped (changed)"
                else:
                    result = subprocess.run(("git", "-C", str(repo), "worktree", "remove", str(path)),
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
                    item["verdict"] = "removed" if result == 0 else "remove failed"
            entries.append(item)
        folder = repo / ".claude/worktrees"
        try:
            for path in folder.iterdir():
                if path.is_dir() and str(path.resolve()) not in registered:
                    extras.append(dict(path=str(path), kind="unregistered"))
        except OSError:
            pass
        if apply and remove and folder.is_dir():  # only where sweep always pruned; other repos' prunable records are listed only
            run("git", "-C", str(repo), "worktree", "prune")
    return entries, extras
def settings():
    path = HOME / ".claude/settings.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    keys = ("autoCompactEnabled", "autoCompactWindow", "bashOutputMaxChars",
            "cleanupPeriodDays", "desktopSessionCleanupPeriodDays")
    values = {key: data.get(key) for key in keys}
    values["env.BASH_MAX_OUTPUT_LENGTH"] = data.get("env", {}).get("BASH_MAX_OUTPUT_LENGTH")
    advice = []  # user keeps auto-compact off by choice; don't suggest it
    if not values["bashOutputMaxChars"] and not values["env.BASH_MAX_OUTPUT_LENGTH"]:
        advice.append("Optional: bashOutputMaxChars ~15000 caps noisy Bash output (default 30000).")
    if not values["desktopSessionCleanupPeriodDays"]:
        advice.append("Set desktopSessionCleanupPeriodDays ~14; desktop transcripts are otherwise kept forever.")
    return dict(values=values, recommendations=advice)
def gb(n):
    return f"{n / 1e9:.2f} GB"
def tree_size(path, found=None):
    """(bytes on disk, newest mtime, files) under path; never follows symlinks. found: a list to collect .git and .sweep-keep paths."""
    total = newest = files = 0
    stack = [str(path)]
    while stack:
        top = stack.pop()
        try:
            st = os.lstat(top)
        except OSError:
            continue
        total += st.st_blocks * 512
        newest = max(newest, st.st_mtime)
        if found is not None and os.path.basename(top) in (".git", ".sweep-keep"):
            found.append(top)
        if not stat.S_ISDIR(st.st_mode):
            files += 1
            continue
        try:
            stack.extend(e.path for e in os.scandir(top))
        except OSError:
            pass
    return total, newest, files
def read_keep(folder=KEEP_DIR):
    """Paths, ports and pids that sessions declared they need (folder/*.txt: "path /abs", "port 5266", "pid 123"),
    and each declaration with its file (entries)."""
    paths, kept_ports, pids, entries = [], set(), set(), []
    for f in sorted(Path(folder).glob("*.txt")):
        try:
            lines = f.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            kind, _, value = line.split("#", 1)[0].strip().partition(" ")
            value = value.strip()
            if kind == "path" and value:
                paths.append(os.path.realpath(os.path.expanduser(value)))
                entries.append((kind, paths[-1], str(f)))
            elif kind in ("port", "pid") and value.isdigit():
                (kept_ports if kind == "port" else pids).add(int(value))
                entries.append((kind, value, str(f)))
    return dict(paths=paths, ports=sorted(kept_ports), pids=sorted(pids), entries=entries)
def tcp_listening():
    """Every listening TCP port on this Mac, any user's (netstat); None when netstat failed."""
    try:
        r = subprocess.run(("netstat", "-anp", "tcp"), text=True, capture_output=True, check=False)
    except OSError:
        return None
    return set(re.findall(r"\.(\d+)\s+\S+\s+LISTEN\b", r.stdout)) if r.returncode == 0 else None
def stale_keeps(entries, listening):
    """Keep declarations that no longer resolve (pid gone, nothing listening on the port, path missing): listed for the
    owner to review; sweep never edits or deletes keep files. listening None = ports unchecked, so none listed."""
    out = []
    for k, v, f in entries:
        why = ("process gone" if k == "pid" and not alive(int(v)) else "nothing listening" if k == "port" and listening is not None
               and v not in listening else "missing" if k == "path" and not os.path.lexists(v) else "")
        if why:
            out.append(dict(kind=k, value=v, file=f, why=why))
    return out
def live_sessions():
    """{sessionId: cwd} of Claude Code sessions whose CLI process is alive."""
    found = {}
    for f in (HOME / ".claude/sessions").glob("*.json"):
        try:
            d = json.loads(f.read_text())
            if alive(int(d["pid"])):
                found[d["sessionId"]] = d.get("cwd", "")
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return found
def gentle():
    try:
        os.nice(19)
    except OSError:
        pass
    run("taskpolicy", "-b", "-p", str(os.getpid()))  # background CPU and IO; children inherit it
def busy(max_load, wait):
    """Why heavy scans should not run now (None = go): a high 1-minute load."""
    deadline = time.monotonic() + wait
    while True:
        load = os.getloadavg()[0]
        if load < max_load:
            return None
        if time.monotonic() >= deadline:
            return f"1-minute load {load:.1f} >= {max_load:g}"
        time.sleep(5)
def job_verdict(status, idle_min, session_live, stale_min=30, clear_hours=6, finished=False):
    """A Codex job record that says queued/running: running, hung (ask) or stale (proven: long silent, its turn never
    completed, and the Claude session recorded in it is gone, so the record can be cleared). session_live None: no
    session recorded, so nothing proves its owner exited."""
    if status not in ("queued", "running"):
        return None
    if idle_min <= stale_min:
        return "running"
    return "stale" if idle_min > clear_hours * 60 and session_live is False and not finished else "hung"
def mark_job_failed(job_file, job_id):
    patch = dict(status="failed", phase="failed", pid=None, completedAt=dt.datetime.now(dt.timezone.utc).isoformat(),
                 errorMessage="sweep: no Codex activity for hours; record cleared")
    for path in (job_file, job_file.parent.parent / "state.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for job in [data] if path == job_file else data.get("jobs", []):
            if job.get("id") == job_id:
                job.update(patch)
        tmp = path.with_name(path.name + ".sweep-tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, path)
def job_activity(job_file):
    """A job record, whether its log says the turn completed, its Codex session file, and minutes since any of them changed."""
    job = json.loads(job_file.read_text())
    log = Path(job.get("logFile") or job_file.with_suffix(".log"))
    try:
        finished = "Turn completed" in log.read_text(errors="ignore")
    except OSError:
        finished = False
    thread = job.get("threadId")
    session = next(iter(glob.glob(str(CODEX_SESSIONS / f"*/*/*/rollout-*-{thread}.jsonl"))), None) if thread else None
    stamps = [p.stat().st_mtime for p in map(Path, filter(None, (job_file, log, session))) if p.exists()]
    return job, finished, session, (time.time() - max(stamps)) / 60
def codex_jobs(apply, clear_hours, live=()):
    out = []
    for job_file in BROKER_ROOT.glob("*/jobs/*.json"):
        try:
            job, finished, session, idle = job_activity(job_file)
        except (OSError, ValueError):
            continue
        if job.get("status") not in ("queued", "running"):
            continue
        sid = job.get("sessionId")
        item = dict(id=job.get("id"), workspace=job.get("workspaceRoot"), summary=(job.get("summary") or "")[:70],
                    idle_min=round(idle), turn_completed=finished, session_file=session,
                    claude_session=sid, session_live=sid in live if sid else None,
                    verdict=job_verdict(job["status"], idle, sid in live if sid else None, clear_hours=clear_hours, finished=finished))
        if apply and item["verdict"] == "stale":
            try:  # re-check the record, its log and session files, and its Claude session right before writing
                job, finished, _, idle = job_activity(job_file)
                sid = job.get("sessionId")
                if job_verdict(job.get("status"), idle, sid in live_sessions() if sid else None, clear_hours=clear_hours,
                               finished=finished) != "stale":
                    item["verdict"] = "skipped (changed)"
                else:
                    mark_job_failed(job_file, item["id"])
                    item["verdict"] = "cleared"
            except (OSError, ValueError):
                item["verdict"] = "clear failed"
        out.append(item)
    return out
def has_marker(path, root):
    """A .sweep-keep file in path or any parent up to root."""
    p, root = Path(path), Path(root)
    while True:
        if (p / ".sweep-keep").exists():
            return True
        if p == root or p == p.parent:
            return False
        p = p.parent
def bulk_verdict(path, age_hours, keep_paths, busy_cwds, mentioned, tracked, recent_hours, marker=False, cwds_ok=True):
    reasons = (["has git-tracked files"] if tracked else []) + (["declared keep"] if marker or any(
        inside(path, k) or inside(k, path) for k in keep_paths) else [])
    if any(inside(c, path) for c in busy_cwds):
        reasons.append("process cwd inside")
    if not cwds_ok:
        reasons.append("could not check process cwds")
    if age_hours < recent_hours:
        reasons.append(f"changed {age_hours:.0f}h ago")
    if mentioned:
        reasons.append("named in a live session's transcript")
    return ("keep" if reasons else "candidate"), reasons
def live_transcripts(live, self_id):
    """Transcripts of every other live session, however long it has been idle. This session's is skipped: it holds
    sweep's own earlier output, which names every candidate."""
    files = []
    for sid in live:
        if sid != self_id:
            files += glob.glob(str(PROJECTS / f"*/{sid}.jsonl")) + glob.glob(str(PROJECTS / f"*/{sid}/subagents/*.jsonl"))
    return files
def worktree_bulk(checkouts, keep_paths, cwds, live, self_id, min_mb, recent_hours, cwds_ok=True):
    """Big gitignored, untracked folders inside checkouts (e.g. shots/tmp/*). Listed only; never deleted here."""
    units = []
    for wt in checkouts:
        raw = run("git", "-C", wt, "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory")
        groups, entries = {}, list(filter(None, raw.split("\0")))
        dirs = {e for e in entries if e.endswith("/")}
        for rel in entries:
            top = rel.split("/")[0]
            if (top in BUILD_DIRS or top.startswith(".") or "__pycache__" in rel
                    or any(rel.startswith(d) and rel != d for d in dirs)):  # git also lists *.log files inside a listed folder
                continue
            if rel.endswith("/"):
                folder = os.path.join(wt, rel)
                try:
                    kids = list(os.scandir(folder))
                except OSError:
                    continue
                if any(k.is_dir(follow_symlinks=False) for k in kids):  # shots/tmp/ -> one unit per lead folder
                    for k in kids:
                        groups[rel + k.name] = [rel + k.name]
                else:
                    groups[rel.rstrip("/")] = [rel.rstrip("/")]
            else:
                groups.setdefault(os.path.dirname(rel) + "/*", []).append(rel)  # loose ignored files per folder
        for key, rels in groups.items():
            sized = [tree_size(os.path.join(wt, r)) for r in rels]
            size = sum(s[0] for s in sized)
            if size < min_mb * 1e6:
                continue
            units.append(dict(checkout=wt, rel=key, path=os.path.join(wt, key.removesuffix("/*")), bytes=size,
                              files=sum(s[2] for s in sized), age_hours=round((time.time() - max(s[1] for s in sized)) / 3600, 1),
                              tracked=bool(run("git", "-C", wt, "ls-files", "--", *rels[:200]).strip())))
    names = {u["rel"]: "/".join(u["rel"].removesuffix("/*").split("/")[-2:]) for u in units}
    mentioned = set()
    for f in live_transcripts(live, self_id):
        try:
            data = Path(f).read_bytes()
        except OSError:
            continue
        mentioned.update(rel for rel, n in names.items() if n.encode() in data)
    busy_cwds = list(cwds.values())
    for u in units:
        u["verdict"], u["reasons"] = bulk_verdict(u["path"], u["age_hours"], keep_paths, busy_cwds, u["rel"] in mentioned,
                                                  u["tracked"], recent_hours, has_marker(u["path"], u["checkout"]), cwds_ok)
    return sorted(units, key=lambda u: -u["bytes"])
def transcript_units(days, skip_ids, root=PROJECTS, codex_root=CODEX_SESSIONS, now=None):
    """Sessions (Claude Code: <uuid>.jsonl + its <uuid>/; nothing else in a project folder is a session) and Codex rollout
    files untouched for `days` (never under 7); memory folders and skip_ids (live sessions, this one) are never included."""
    cutoff = (now or time.time()) - max(days, MIN_TRANSCRIPT_DAYS) * 86400
    units = []
    for proj in sorted(Path(root).iterdir()) if Path(root).is_dir() else []:
        if not proj.is_dir():
            continue
        sids = sorted(e.name[:-6] for e in proj.iterdir() if e.name.endswith(".jsonl") and UUID.fullmatch(e.name[:-6]) and e.is_file())
        for sid in sids:
            paths = [proj / f"{sid}.jsonl"] + ([proj / sid] if (proj / sid).is_dir() else [])
            sized = [tree_size(p) for p in paths]
            newest = max(s[1] for s in sized)
            if sid not in skip_ids and newest < cutoff:
                units.append(dict(kind="claude", project=proj.name, sid=sid, paths=[str(p) for p in paths],
                                  bytes=sum(s[0] for s in sized), newest=newest))
    for f in glob.glob(str(Path(codex_root) / "**/*"), recursive=True):
        try:
            st = os.lstat(f)
        except OSError:
            continue
        if (stat.S_ISREG(st.st_mode) and os.path.basename(f).startswith("rollout-") and f.endswith(".jsonl")
                and st.st_mtime < cutoff and not any(s in f for s in skip_ids)):
            units.append(dict(kind="codex", project="codex", sid=os.path.basename(f), paths=[f],
                              bytes=st.st_blocks * 512, newest=st.st_mtime))
    return units
def unit_files(unit):
    for p in unit["paths"]:
        if os.path.isdir(p) and not os.path.islink(p):
            for d, _, names in os.walk(p):
                yield from (os.path.join(d, n) for n in names)
        else:
            yield p
def trash(paths):
    """Move to the macOS Trash (the owner-approved way to delete outside a repo; the owner empties it)."""
    for i in range(0, len(paths), 200):
        subprocess.run(("/usr/bin/trash", *paths[i:i + 200]), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return [p for p in paths if os.path.lexists(p)]
def stamp(path):
    try:
        st = os.lstat(path)
        return st.st_size, st.st_mtime_ns
    except OSError:
        return None
def sha256(fh):
    h = hashlib.sha256()
    with fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
def verify_archive(tgz, names):
    """names (relative to HOME) that are missing from the archive or whose archived size or sha256 differs from the file on disk."""
    bad, seen = [], set()
    with tarfile.open(tgz, "r|gz") as t:
        for m in t:
            if m.name not in names or m.name in seen:
                continue
            seen.add(m.name)
            try:
                same = (m.isfile() and m.size == os.lstat(HOME / m.name).st_size
                        and sha256(t.extractfile(m)) == sha256(open(HOME / m.name, "rb")))
            except OSError:
                same = False
            if not same:
                bad.append(m.name)
    return bad + [n for n in names if n not in seen]
def archive_transcripts(units, mode):
    """mode archive: one .tgz in ~/Archives/claude-chats, every member verified (size, sha256) against its unchanged original,
    then the originals go to the Trash. mode trash: Trash only."""
    result = dict(mode=mode, sessions=len(units), bytes=sum(u["bytes"] for u in units), status="nothing to do")
    if not units:
        return result
    if not os.path.exists("/usr/bin/trash"):
        return dict(result, status="stopped: /usr/bin/trash missing; nothing changed")
    rels, moved = [[os.path.relpath(f, HOME) for f in unit_files(u)] for u in units], set()
    if mode == "archive":
        free = shutil.disk_usage(HOME).free
        if free < result["bytes"] * 0.7 + 2e9:
            return dict(result, status=f"stopped: only {gb(free)} free for the archive; nothing changed")
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        ARCHIVE_DIR.chmod(0o700)
        stem = f"claude-codex-chats-{time.strftime('%Y-%m-%d-%H%M%S')}"
        suffix = 0
        while True:
            tgz = ARCHIVE_DIR / f"{stem}{f'-{suffix}' if suffix else ''}.tgz"
            if not any(p.exists() for p in (tgz, tgz.with_suffix(".files.txt"), tgz.with_suffix(".index.txt"))):
                break
            suffix += 1
        files = [f for r in rels for f in r]
        snap = {f: stamp(HOME / f) for f in files}
        listing = tgz.with_suffix(".files.txt")
        listing.write_text("\n".join(files) + "\n")
        tgz.with_suffix(".index.txt").write_text("kind\tproject\tsession\tlast_modified\tsize_mb\n" + "".join(
            f"{u['kind']}\t{u['project']}\t{u['sid']}\t{time.strftime('%Y-%m-%d %H:%M', time.localtime(u['newest']))}\t"
            f"{u['bytes'] / 1e6:.1f}\n" for u in units))
        rc = subprocess.run(("nice", "-n", "19", "taskpolicy", "-b", "tar", "-czf", str(tgz), "-T", str(listing)),
                            cwd=HOME, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
        if rc == 70:  # taskpolicy cannot set background priority in some sandboxes
            rc = subprocess.run(("nice", "-n", "19", "tar", "-czf", str(tgz), "-T", str(listing)),
                                cwd=HOME, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
        try:
            bad = verify_archive(tgz, set(files)) if rc == 0 else files
        except (OSError, tarfile.TarError, EOFError):
            bad = files
        moved = {f for f in files if stamp(HOME / f) != snap[f]}  # written while archiving: its session is kept below
        wrong = [f for f in bad if f not in moved]
        if rc or wrong:
            return dict(result, archive=str(tgz), status=f"stopped: tar exit {rc}, {len(wrong)} files missing from the archive "
                                                         "or not matching it (size, sha256); originals untouched")
        result.update(archive=str(tgz), archive_bytes=tgz.stat().st_size)
    paths, changed = [], 0
    live_now = set(live_sessions()) | {os.environ.get("CLAUDE_CODE_SESSION_ID", "")}
    for u, r in zip(units, rels):
        if ((u["kind"] == "claude" and u["sid"] in live_now)
                or max(tree_size(p)[1] for p in u["paths"]) > u["newest"] or moved.intersection(r)):
            changed += 1  # written since the scan: keep it
        else:
            paths += u["paths"]
    left = trash(paths)
    return dict(result, skipped_changed=changed, trashed=len(paths) - len(left), failed=left[:5],
                status="done; space returns when the Trash is emptied")
def scratch_row(d, live, cwds, keep_paths, days, now, cwds_ok=True):
    """One session's scratch folder (its name is the session id), with every reason to keep it."""
    found = []
    size, newest, files = tree_size(d, found)
    sid = d.name
    transcript = max((os.path.getmtime(f) for f in glob.glob(str(PROJECTS / f"*/{sid}.jsonl"))), default=0)
    reasons = ["session live"] if sid in live else []
    if now - newest < days * 86400:
        reasons.append(f"changed {(now - newest) / 86400:.1f}d ago")
    if now - transcript < days * 86400:
        reasons.append(f"transcript written {(now - transcript) / 86400:.1f}d ago")
    for g in (f for f in found if os.path.basename(f) == ".git"):
        repo = os.path.dirname(g)
        dirty = len(git("-C", repo, "status", "--porcelain", "--untracked-files=all").splitlines())
        reasons.append(f"git {'worktree' if os.path.isfile(g) else 'repo'} inside: {os.path.relpath(repo, d)}"
                       + (f" (dirty {dirty})" if dirty else ""))
    if any(c and inside(c, str(d)) for c in cwds.values()):
        reasons.append("process cwd inside")
    if not cwds_ok:
        reasons.append("could not check process cwds")
    if any(os.path.basename(f) == ".sweep-keep" for f in found) or any(inside(str(d), k) or inside(k, str(d)) for k in keep_paths):
        reasons.append("declared keep")
    return dict(kind="session", project=d.parent.name, sid=sid, path=str(d), bytes=size, files=files,
                age_days=round((now - newest) / 86400, 1), live=sid in live, verdict="keep" if reasons else "candidate", reasons=reasons)
def session_scratch(live, cwds, keep_paths, root=SCRATCH, days=3, now=None, cwds_ok=True):
    """Session scratch (/private/tmp/claude-<uid>/<project>/<session-id>/), one row per session folder; anything else there
    (bash-edit-diff/, other folders, loose files) is a 'list only' row. Listed only: the owner trashes candidates by hand."""
    now, rows, loose = now or time.time(), [], []
    try:
        top = sorted(Path(root).iterdir())
    except OSError:
        return rows
    for entry in top:
        if not entry.is_dir() or entry.is_symlink():
            loose.append(entry)
            continue
        try:
            kids = sorted(entry.iterdir()) if entry.name.startswith("-") else [entry]  # -Users-...: a project's sessions
        except OSError:
            kids = [entry]
        for d in kids:
            if d is not entry and UUID.fullmatch(d.name) and d.is_dir() and not d.is_symlink():
                rows.append(scratch_row(d, live, cwds, keep_paths, days, now, cwds_ok))
            else:
                size, newest, _ = tree_size(d)
                rows.append(dict(kind="other", path=str(d), bytes=size, age_days=round((now - newest) / 86400, 1),
                                 verdict="list only", reasons=[]))
    if loose:
        sized = [tree_size(p) for p in loose]
        rows.append(dict(kind="other", path=f"{root}/* ({len(loose)} loose files)", bytes=sum(s[0] for s in sized),
                         age_days=round((now - max(s[1] for s in sized)) / 86400, 1), verdict="list only", reasons=[]))
    return sorted(rows, key=lambda r: -r["bytes"])
def storage(sizes_of_worktrees=None):
    rows = [dict(name=name, bytes=sum(tree_size(p)[0] for p in paths if os.path.lexists(p))) for name, paths in STORAGE]
    if sizes_of_worktrees is not None:
        rows.append(dict(name="worktrees (app + agent + manual)", bytes=sizes_of_worktrees))
    return rows
def selftest():
    assert [etime_seconds(x) for x in ("02:03", "01:02:03", "2-03:04:05")] == [123, 3723, 183845]
    positives = ["node /x/scripts/app-server-broker.mjs serve --endpoint unix:/tmp/cxc-XXXX/broker.sock",
                 "node /x/bin/codex app-server", "codex app-server", "codex-code-mode-host",
                 "node /x/codex-companion.mjs task-worker",
                 "npm exec vite --host 127.0.0.1 --port 5242", "node /x/node_modules/.bin/vite",
                 "python -m http.server 8000"]
    negatives = ["/Applications/Claude.app/Contents/MacOS/Claude Helper", "/usr/libexec/containermanagerd",
                 "uv run blender-mcp"]
    assert all(candidate(s) for s in positives), [s for s in positives if not candidate(s)]
    assert not any(candidate(s) for s in negatives)
    fake = parse_ps("10 1 10 2-00:00:00 1.0 1024 me node /x/scripts/app-server-broker.mjs serve\n"
                    "11 10 10 01:00:00 0.5 512 me codex app-server\n"
                    "12 11 10 00:30 0.0 256 me codex-code-mode-host\n")
    assert descendants(10, fake) == [12, 11, 10]
    print("selftest passed: etime, command matching, process tree")
    import unittest  # fixture tests for the classification logic
    sys.path.insert(0, str(Path(__file__).parent))
    sys.dont_write_bytecode = True
    result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromName("test_sweep"))
    sys.exit(0 if result.wasSuccessful() else 1)
def remind(opt_in=False):
    if opt_in and os.environ.get("CLAUDE_PLUGIN_OPTION_REMIND", "").lower() not in ("1", "true", "yes", "on"):
        return
    try:
        data = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        data = {}
    subprocess.Popen((sys.executable, "-I", __file__, "--write-cache"), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    counts = [(sum(t.get("verdict", "orphaned") == "orphaned" for t in data.get("processes", [])), "orphaned process trees"),
              (len(data.get("broker_states", [])), "dead Codex records"),
              (sum(w.get("verdict") == "removable" for w in data.get("worktrees", [])), "removable worktrees"),
              (sum(t.get("verdict") == "ask" for t in data.get("processes", [])), "process trees to ask about"),
              (sum(j.get("verdict") in ("hung", "stale") for j in data.get("codex_jobs", [])), "hung Codex jobs")]
    parts = [f"{n} {what}" for n, what in counts if n]
    if parts:
        cmd = "/sweep:sweep" if os.environ.get("CLAUDE_PLUGIN_ROOT") else "/sweep"  # plugin skills are namespaced
        line = "sweep: " + ", ".join(parts) + f" (as of last check). Run {cmd} to clean up."
        print(json.dumps({"systemMessage": line, "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": line + f" Mention this to the user once; do not run {cmd} yourself."}}))
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="kill orphaned trees, clear stale broker/lock/job records")
    ap.add_argument("--hours", type=float, default=24, help="orphaned process age")
    ap.add_argument("--wt-days", type=float, default=2)
    ap.add_argument("--remove-worktrees", action="store_true")
    ap.add_argument("--sizes", action="store_true", help="also size each worktree (du)")
    ap.add_argument("--keep", type=int, nargs="+", action="append", default=[])
    ap.add_argument("--kill", type=int, nargs="+", default=[], help="with --apply: also kill these 'ask owner' root pids (owner said yes)")
    ap.add_argument("--explain", type=int, nargs="+", help="show parent chain, owner and verdict for these pids; changes nothing")
    ap.add_argument("--days", type=float, default=MIN_TRANSCRIPT_DAYS, help=f"transcript age (never under {MIN_TRANSCRIPT_DAYS})")
    ap.add_argument("--transcripts", choices=("archive", "trash"),
                    help="with --apply and the owner's yes: archive (verified .tgz) or trash old transcripts")
    ap.add_argument("--min-mb", type=float, default=100, help="smallest worktree bulk folder to list")
    ap.add_argument("--recent-hours", type=float, default=24, help="bulk changed this recently is kept")
    ap.add_argument("--job-hours", type=float, default=6, help="clear a hung Codex job record after this much silence")
    ap.add_argument("--gentle", action="store_true", help="run at lowest CPU/IO priority")
    ap.add_argument("--max-load", type=float, default=8, help="skip heavy scans at or above this 1-minute load")
    ap.add_argument("--wait", type=float, default=30, help="seconds to wait for the load to drop")
    ap.add_argument("--quick", action="store_true", help="skip heavy scans (storage, bulk, transcripts)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--remind", action="store_true", help="SessionStart hook: print last cached result, refresh cache in background")
    ap.add_argument("--opt-in", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--write-cache", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    os.umask(0o077)  # its cache holds process command lines and its archives hold whole chats: owner-only
    if args.selftest:
        selftest()
        return
    if args.remind:
        remind(args.opt_in)
        return
    if args.explain:
        table, cwds, live, keep = parse_ps(run(*PS)), cwd_map(), live_sessions(), read_keep()
        for pid in args.explain:
            print(json.dumps(explain(pid, table, cwds, live, keep) if pid in table else dict(pid=pid, error="no such process"), indent=2))
        return
    if args.write_cache:
        args.apply = args.remove_worktrees = args.sizes = False
        args.transcripts, args.quick, args.gentle = None, True, True
    if args.gentle:
        gentle()
    keep = read_keep()
    live = live_sessions()
    self_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    table = parse_ps(run(*PS))
    cwds, listen = cwd_map(), listeners()
    cwds_ok = os.getpid() in cwds  # no own cwd = lsof failed
    trees = process_sweep(table, args.hours, set(sum(args.keep, [])) | set(keep["pids"]), args.apply, cwds, keep["ports"],
                          live, set(args.kill), keep["paths"], listen, cwds_ok)
    kept = kept_servers(table, trees, cwds, listen)
    for k in kept:
        if args.apply and k["pid"] in args.kill:  # the owner said yes to stopping it
            print(f"stopping {k['label']} pid {k['pid']} kept by {k['owner']}: {k['command']}", file=sys.stderr, flush=True)
            k["status"] = kill_tree(k["pids"], table)
    brokers = broker_states(args.apply)
    jobs = codex_jobs(args.apply, args.job_hours, live)
    skipped = "quick mode" if args.quick else busy(args.max_load, args.wait)
    found = repos(live.values())
    wts, extras = worktrees(found, args.wt_days, args.sizes and not skipped, args.apply, args.remove_worktrees, cwds, live,
                            cwds_ok, keep["paths"])
    stale = stale_keeps(keep["entries"], tcp_listening() if keep["ports"] else None)
    bulk, units, store, archived, scratch = [], [], [], None, []
    if not skipped:
        checkouts = sorted({w["path"] for w in wts} | {w["repo"] for w in wts})
        bulk = worktree_bulk(checkouts, keep["paths"], cwds, live, self_id, args.min_mb, args.recent_hours, cwds_ok)
        scratch = session_scratch(set(live) | {self_id}, cwds, keep["paths"], cwds_ok=cwds_ok)
        units = transcript_units(args.days, set(live) | {self_id})
        store = storage(sum(w.get("size_mb") or 0 for w in wts) * 1048576 if args.sizes else None)
        if args.apply and args.transcripts:
            archived = archive_transcripts(units, args.transcripts)
    old = sum(u["bytes"] for u in units)
    doomed = [t for t in trees if t["status"] in (("killed",) if args.apply else ("would kill",))]
    summary = (f"{'killed' if args.apply else 'would kill'} {len(doomed)} proven orphan trees "
               f"({sum(t['rss_mb'] for t in doomed):.1f} MB RSS), {sum(t['status'] == 'ask owner' for t in trees)} trees to ask about, "
               f"{len(kept)} dev servers kept because their parent is running, "
               f"{len(brokers)} stale state files, "
               f"{sum(j['verdict'] in ('hung', 'stale') for j in jobs)} hung Codex jobs"
               f"{f' ({n} records cleared)' if (n := sum(j['verdict'] == 'cleared' for j in jobs)) else ''}, "
               f"{sum(w['verdict'] in ('removable', 'removed') for w in wts)} removable worktrees" +
               ("" if skipped else f", {gb(sum(b['bytes'] for b in bulk if b['verdict'] == 'candidate'))} worktree bulk to review, "
                                   f"{gb(sum(s['bytes'] for s in scratch if s['verdict'] == 'candidate'))} session scratch to review, "
                                   f"{gb(old)} transcripts older than {max(args.days, MIN_TRANSCRIPT_DAYS):g}d"))
    data = dict(processes=trees, kept_servers=kept, broker_states=brokers, codex_jobs=jobs, worktrees=wts,
                worktree_extras=extras, worktree_repos=[str(r) for r in found], worktree_bulk=bulk, session_scratch=scratch,
                storage=store, keep=keep, stale_keeps=stale, heavy_skipped=skipped,
                old_transcripts=dict(sessions=sum(u["kind"] == "claude" for u in units),
                                     codex_files=sum(u["kind"] == "codex" for u in units), bytes=old),
                transcript_action=archived, settings=settings(), load=round(os.getloadavg()[0], 1), summary=summary)
    if args.write_cache:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.parent.chmod(0o700)
        tmp = CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, CACHE)
        return
    if args.json:
        print(json.dumps(data, indent=2))
        return
    print("Processes:")
    for t in trees:
        print(f"  {t['status']}: {t['label']} pid {t['pid']} ({len(t['pids'])} procs, {t['rss_mb']} MB RSS, {t['cpu']}% CPU, {t['age_hours']}h) cwd={t['cwd'] or '?'}\n"
              f"    command: {t['command']}\n    chain: {' <- '.join(t['chain'])}\n    evidence: {'; '.join(t['evidence'])}")
    print("Dev servers kept because their parent is running (stop one only on the owner's yes: --apply --kill <pid>):")
    for k in kept:
        print(f"  {k['status']}: {k['label']} pid {k['pid']} ({len(k['pids'])} procs, {k['rss_mb']} MB RSS, {k['age_hours']}h)"
              f" cwd={k['cwd'] or '?'}\n    owner: {k['owner']}\n    command: {k['command']}")
    print("Broker states:")
    for b in brokers:
        print(f"  {b['status']}: {b['path']} (pid {b['pid']})")
    print("Codex jobs (queued/running records):")
    for j in jobs:
        print(f"  {j['verdict']}: {j['id']} idle {j['idle_min']} min{'; turn completed' if j['turn_completed'] else ''}"
              f"{'; no Claude session recorded' if not j['claude_session'] else ''}"
              f" [{os.path.basename(j['workspace'] or '?')}] {j['summary']}")
    bases = ", ".join(str(b).replace(str(HOME), "~", 1) for b in SCAN_BASES if b.is_dir()) or "no scan folders found"
    print(f"Worktrees (scope: {len(found)} git repos in {bases} to depth 3 or holding a live session's cwd;"
          " each repo's `git worktree list`):")
    for w in wts:
        print(f"  {w['verdict']}: {w['path']} [{w['branch']}; idle {w['idle_days']}d; commit {w['commit_days']}d]" +
              (f" {w['size_mb']} MB" if w.get("size_mb") else "") + (f" ({', '.join(w['reasons'])})" if w['reasons'] else ""))
    for e in extras:
        print(f"  {e['kind']}: {e['path']}")
    if skipped:
        print(f"Heavy scans skipped ({skipped}): storage, worktree bulk, session scratch, old transcripts. Rerun later or with --gentle.")
    else:
        print("Storage (local scan; the app's own figures come from get_storage_usage):")
        for r in store:
            print(f"  {gb(r['bytes']):>9}  {r['name']}")
        print("  Not scanned: the Cowork VM (never touched); kept-ready worktrees are the app's (get_storage_usage).")
        print(f"Worktree bulk (gitignored, untracked, >= {args.min_mb:g} MB; list only):")
        for b in bulk:
            print(f"  {b['verdict']}: {gb(b['bytes'])} {b['path']} ({b['files']} files, changed {b['age_hours']}h ago)" +
                  (f" ({', '.join(b['reasons'])})" if b['reasons'] else ""))
        print(f"Session scratch ({SCRATCH}; list only, >= 10 MB):")
        for s in (s for s in scratch if s["bytes"] >= 1e7):
            print(f"  {s['verdict']}: {gb(s['bytes'])} {s['path']} (" + "; ".join(
                [f"changed {s['age_days']}d ago"] + [r for r in s["reasons"] if not r.startswith("changed ")]) + ")")
        small = [s for s in scratch if s["bytes"] < 1e7]
        if small:
            kinds = sorted({s["verdict"] for s in small})
            print(f"  {len(small)} smaller: {gb(sum(s['bytes'] for s in small))} ("
                  + ", ".join(f"{sum(s['verdict'] == v for s in small)} {v}" for v in kinds) + ")")
        projects = {}
        for u in units:
            projects[u["project"]] = projects.get(u["project"], 0) + u["bytes"]
        print(f"Transcripts older than {max(args.days, MIN_TRANSCRIPT_DAYS):g} days (live sessions, this one and memory/ excluded): "
              f"{data['old_transcripts']['sessions']} Claude sessions + {data['old_transcripts']['codex_files']} Codex files, {gb(old)}")
        for name, size in sorted(projects.items(), key=lambda kv: -kv[1])[:6]:
            print(f"  {gb(size):>9}  {name}")
        if archived:
            print("Transcript action:", json.dumps(archived))
    print(f"Keep declarations ({KEEP_DIR}): {len(keep['paths'])} paths, ports {keep['ports'] or '-'}, pids {keep['pids'] or '-'}"
          f"; {len(stale)} no longer resolve (owner reviews; sweep never edits keep files)")
    for s in stale:
        print(f"  stale keep: {s['kind']} {s['value']} ({s['why']}) in {s['file']}")
    print("Settings:", json.dumps(data["settings"]["values"], separators=(",", ":")))
    for rec in data["settings"]["recommendations"]:
        print("  Suggest:", rec)
    print(f"Load (1 min): {data['load']}")
    print(summary)
if __name__ == "__main__":
    main()
