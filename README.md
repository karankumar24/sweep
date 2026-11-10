<div align="center">

<img src="docs/banner.png" alt="sweep" width="720">

# Clean up after your agents

**Finds the dev servers, browsers, worktrees and old transcripts that Claude Code and Codex left running or lying around on your Mac, and removes only what it can prove nobody needs.**

<a href="https://github.com/karankumar24/sweep/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/karankumar24/sweep/ci.yml?branch=main&style=flat-square&label=ci" alt="ci"></a>
<a href="#install"><img src="https://img.shields.io/badge/Claude_Code-plugin-D97757?style=flat-square" alt="Claude Code plugin"></a>
<a href="#install"><img src="https://img.shields.io/badge/macOS-only-555555?style=flat-square" alt="macOS only"></a>
<a href="#install"><img src="https://img.shields.io/badge/python-3.9%2B-3776AB?style=flat-square" alt="Python 3.9+"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue?style=flat-square" alt="MIT license"></a>

</div>

---

Agents start things and forget them. A `vite` server from Tuesday still holds port 5173, three headless Chromes idle in the background, a dozen worktrees for branches that merged last week, and gigabytes of old transcripts sit in `~/.claude`. sweep finds them, kills what is provably orphaned, and asks you about everything else.

You type `/sweep:sweep`. The first thing you see is the dry run (trimmed):

```text
Processes:
  would kill: vite :5173 pid 48211 (3 procs, 212.4 MB RSS, 0.0% CPU, 74.2h) cwd=~/code/shop
    evidence: reparented to launchd (ppid 1): its parent exited; no process outside its tree is
              connected to its ports; the Claude session that started it (pid 40112) has exited
  ask owner: next :3000 pid 51870 (5 procs, 388.0 MB RSS, 0.1% CPU, 6.2h) cwd=~/code/blog
    evidence: ...; the Claude session that started it (pid 51002) has exited;
              only 6.2h old: proven orphans are killed after 24h
  keep: chrome-headless-shell pid 50644 (9 procs, 640.3 MB RSS, 0.0% CPU, 0.4h) cwd=~/code/shop
    evidence: started by Claude session pid 50120, still running
Worktrees:
  removable: ~/code/shop/.claude/worktrees/fix-cart [fix/cart; idle 6.0d; commit 6.1d]
  ask: ~/code/shop/.claude/worktrees/new-checkout [feat/checkout; idle 3.1d; commit 3.2d]
       (pushed but not merged into origin/main: owner decides)
Transcripts older than 7 days: 214 Claude sessions + 96 Codex files, 1.8 GB
```

Then Claude kills the one proven orphan and asks you about the rest, one question at a time, with keeping as the default.

## How it decides

- **A process is only killed when it is provably orphaned.** It was reparented to launchd, the Claude session that started it has exited, nothing outside its own tree is connected to its ports, no live session is working in its folder, and it is more than a day old. If any of that can't be checked, it asks.
- **Anything a live Claude session started stays.** sweep walks each process's parent chain, and a running Claude Code CLI or Claude app anywhere above it means keep.
- **Right before every kill it checks it's the same process.** If a pid's parent, start time or command changed since the scan, it is skipped.
- **Worktrees are removed only on your yes**, and only when clean, fully pushed, merged into `origin/HEAD` and idle.
- **Files go to the Trash, never `rm`.** That covers gitignored bulk inside worktrees, old session scratch and old transcripts, each only after you say yes. Transcripts can be archived first into one `.tgz` that is checked file by file (size and sha256) before the originals move.
- **You can tell it to leave something alone.** Put an empty `.sweep-keep` file in a folder, or write `~/.claude/sweep/keep/<name>.txt` with lines like `port 5266`, `pid 1234` or `path /abs/folder`.

It also recognizes Codex leftovers: broker processes, stale broker state and hung Codex job records. In the Claude desktop app it adds the app's storage breakdown and offers to archive old idle sessions.

## Install

Needs macOS and Python 3.9 or newer (`python3 --version`). In Claude Code:

```
/plugin marketplace add karankumar24/sweep
```

```
/plugin install sweep@sweep
```

Run it with `/sweep:sweep`. If the command doesn't show up, run `/reload-plugins`.

**Prefer plain `/sweep`?** Copy the skill into your personal skills folder instead. Install it one way or the other, not both, or you'll have two copies.

```bash
DIR=$(mktemp -d)
git clone --depth 1 https://github.com/karankumar24/sweep.git "$DIR"
mkdir -p ~/.claude/skills
cp -r "$DIR/skills/sweep" ~/.claude/skills/
rm -rf "$DIR"
```

The copy doesn't update itself. Pull and copy again for fixes.

Check it works:

```bash
python3 -I ~/.claude/skills/sweep/sweep.py --selftest
```

## Usage

```
/sweep:sweep
```

Or say "clean up", "my disk is full" or "something is hogging port 3000", and Claude reaches for it.

To look without Claude, run the script yourself. It changes nothing without `--apply`:

```bash
python3 -I ~/.claude/skills/sweep/sweep.py --quick
```

That's the copy-in path. The plugin keeps it under `~/.claude/plugins/cache/sweep/`, or run it from a clone of this repo as `skills/sweep/sweep.py`. `--explain <pid>` shows why a process would be kept or killed, and `--help` lists every flag.

sweep looks for git repos in `~/dev`, `~/code`, `~/projects`, `~/src`, `~/Desktop` and `~/Downloads`, plus every repo a live session is working in. To change that, set `SWEEP_SCAN_DIRS=~/work:~/oss`.

### Startup reminder (optional, off by default)

sweep can print a one-line note when a session starts, like `sweep: 2 orphaned process trees, 3 removable worktrees (as of last check)`. The note comes from the last scan, and each session start refreshes that scan in the background. That's a quick, read-only look at processes, sockets and git state, saved to `~/.claude/sweep/last.json`, but it runs every time, so it's off unless you turn it on.

- **Plugin:** turn on the plugin's `remind` option when you enable it, or later in `/plugin`.
- **Copy-in:** add this hook to `~/.claude/settings.json`, merging it into `"hooks"` if you already have some:

```json
"hooks": {
  "SessionStart": [
    { "matcher": "startup",
      "hooks": [{ "type": "command", "command": "python3 -I ~/.claude/skills/sweep/sweep.py --remind", "timeout": 5 }] }
  ]
}
```

## Your data

- Everything runs locally. There are no network calls and no telemetry.
- sweep reads process lists, open ports, git state and file sizes. It keeps its own state in `~/.claude/sweep/`: your keep files, and its last scan when the startup reminder is on.
- Archives go to `~/Archives/claude-chats/`. Deleted files go to the Trash, so you can get them back until you empty it.
- It never touches the Claude app's data folder or VM, installed Claude Code versions, your global settings, or Claude's memory folders. [SECURITY.md](SECURITY.md) has the details.

## Good to know

- macOS only. It relies on launchd, macOS `ps` and `lsof`, and `/usr/bin/trash`, which recent macOS versions ship. Without it, sweep deletes nothing.
- The script enforces the kill and worktree rules itself. Asking you before it trashes files is up to the skill's instructions, which Claude follows like any prompt. So read each question before you say yes.
- Heavy scans skip themselves while your machine is busy (1-minute load of 8 or more). Run it again later, or pass `--gentle`.
- If you use another agent that starts dev servers, sweep sees its processes but only acts on ones a Claude session started. Anything else is "ask".

## Contributing

Issues and pull requests are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) says what fits, and `python3 -I skills/sweep/sweep.py --selftest` runs the tests. MIT licensed.
