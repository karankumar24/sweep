# sweep guide

The [README](../README.md) covers what sweep does and how to install it. This page has the rest.

## What it looks for

- **Leftover processes:** dev servers (vite, next, npm/pnpm/yarn/bun dev, and the like), test browsers (Playwright, headless Chrome), background Blender renders (`blender -b`), and Codex helper processes.
- **Codex job records** stuck as "running" after the session that started them ended.
- **Worktrees** whose branch is merged and pushed, with nothing unsaved in them.
- **Build output inside worktrees** that git ignores: screenshots, caches, `node_modules`-sized folders.
- **Temporary session folders** Claude Code leaves in `/private/tmp`.
- **Chat logs** older than a week, from Claude Code and Codex.
- In the Claude desktop app, also the app's storage breakdown and sessions you haven't touched in two days (archiving is reversible).

## The rules it follows

A process is stopped without asking only when all of these are true:

1. Its parent process is gone (macOS handed it to `launchd`).
2. The Claude session that started it has ended.
3. No other process is connected to its ports.
4. No running session is working in its folder.
5. Nothing on your keep list covers it.
6. It has been running for more than 24 hours (`--hours` changes this).

If any of these can't be checked, it asks instead. Just before stopping anything, it checks again that it's the same process, because macOS reuses process numbers.

A worktree can be removed only when it has no unsaved changes, every commit is pushed, its branch is merged into the default branch, nothing is running in it, and no keep file covers it. sweep runs `git worktree remove`, never with `--force`.

Files only ever go to the Trash. Chat logs can be packed into one `.tgz` in `~/Archives/claude-chats/` first, and every file in it is compared with the original (size and SHA-256) before the originals move.

## Keep lists

To make sweep leave something alone:

- put an empty file named `.sweep-keep` in the folder, or
- create `~/.claude/sweep/keep/<any-name>.txt` with one item per line:

```text
port 5266
pid 1234
path ~/code/demo
```

sweep also keeps anything git tracks, folders changed in the last 24 hours, and folders a running process or session is using. It lists keep entries that no longer point at anything, but never edits your keep files.

## Running the script yourself

The script only looks unless you pass `--apply`:

```bash
python3 -I ~/.claude/skills/sweep/sweep.py --quick
```

- `--quick` skips the slow disk scans.
- `--explain 48211` explains why process 48211 would be kept or stopped.
- `--json` prints everything as JSON.
- `--help` lists every option.

With the plugin, the script is under `~/.claude/plugins/cache/sweep/`.

Here is what it prints (trimmed):

```text
Processes:
  would kill: vite :5173 pid 48211 (3 procs, 212.4 MB RSS, 0.0% CPU, 74.2h) cwd=~/code/shop
    evidence: reparented to launchd (ppid 1): its parent exited; no process outside its tree is
              connected to its ports; the Claude session that started it (pid 40112) has exited
  ask owner: next :3000 pid 51870 (5 procs, 388.0 MB RSS, 0.1% CPU, 6.2h) cwd=~/code/blog
    evidence: ...; only 6.2h old: proven orphans are killed after 24h
  keep: chrome-headless-shell pid 50644 (9 procs, 640.3 MB RSS, 0.0% CPU, 0.4h) cwd=~/code/shop
    evidence: started by Claude session pid 50120, still running
Worktrees:
  removable: ~/code/shop/.claude/worktrees/fix-cart [fix/cart; idle 6.0d; commit 6.1d]
Transcripts older than 7 days: 214 Claude sessions + 96 Codex files, 1.8 GB
```

## Where it looks for repos

`~/dev`, `~/code`, `~/projects`, `~/src`, `~/Desktop` and `~/Downloads` (three folders deep), plus every repo a running session is using. To use other folders:

```bash
export SWEEP_SCAN_DIRS=~/work:~/oss
```

## Startup reminder

sweep can show one line when a session starts, like `sweep: 2 orphaned process trees, 3 removable worktrees (as of last check)`. To keep it current, each session start runs a quick, read-only look in the background. It's off until you turn it on.

**Copy-in install:** add this to `~/.claude/settings.json`, inside your existing `"hooks"` if you have one:

```json
"hooks": {
  "SessionStart": [
    {
      "matcher": "startup",
      "hooks": [
        {
          "type": "command",
          "command": "python3 -I ~/.claude/skills/sweep/sweep.py --remind",
          "timeout": 5
        }
      ]
    }
  ]
}
```

**Plugin:** turn on the `remind` option when you install it, or later in `/plugin`.

## When your Mac is busy

If the 1-minute load average is 8 or more, sweep waits up to 30 seconds, then skips the slow disk scans and says so. Run it again later, or raise the limit with `--max-load 12`.

## Requirements

macOS with Python 3.9 or newer. sweep uses tools built into macOS (`ps`, `lsof`, `netstat`, and `trash`, which recent versions include). Without `trash`, sweep deletes nothing.
