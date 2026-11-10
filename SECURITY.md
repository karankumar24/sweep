# Security

## What sweep does on your machine

sweep runs locally and makes no network calls. It reads:

- the process list (`ps`, including each process's environment for `CLAUDE_PID`), open ports and working directories (`lsof`, `netstat`)
- git state of repos under the folders it scans and the repos live sessions are in
- sizes and modification times under `~/.claude`, `~/.codex`, the Claude app's support folder and Claude Code's session scratch

It writes:

- `~/.claude/sweep/last.json`, its last scan, only when the optional startup reminder is on (process commands and paths on your machine)
- `~/Archives/claude-chats/`, only when you approve archiving old transcripts

Its folder and the files it writes (scan cache, archives) are readable only by you.

It changes things only with `--apply`:

- sends SIGTERM, then SIGKILL, to process trees it proved orphaned, or that you said yes to
- marks a hung Codex job record as failed, and deletes stale Codex broker state files whose process is gone
- runs `git worktree remove` (never `--force`) on worktrees you approved
- moves files to the Trash with `/usr/bin/trash`. It never runs `rm` on your files.

## What it never touches

The Claude app's data folder and VM, installed Claude Code versions, your global Claude Code settings, Claude's memory folders, or live sessions' transcripts. Processes, worktrees, worktree bulk and session scratch covered by a keep declaration or `.sweep-keep` are always kept.

## Limits

The kill, job-record and worktree rules are enforced by the script, with a re-check right before each action. Asking you before trashing files is an instruction to Claude in SKILL.md, and Claude follows it the way it follows any prompt. Read each question before you answer it.

## Reporting a vulnerability

If you find a way to make sweep kill, trash or remove something its rules say it must keep, report it privately: open this repository's **Security** tab and choose **Report a vulnerability**. Please don't file a public issue first.

This is a single-maintainer project with no bug bounty. You'll get an honest answer about whether and when it will be fixed.
