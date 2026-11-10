# Contributing

Thanks for looking. sweep kills processes and moves files to the Trash, so the bar for a change is that it can't make it remove something it shouldn't.

## Running the tests

You need macOS and Python 3.9 or newer.

```
python3 -I skills/sweep/sweep.py --selftest
```

The tests build fake process tables, worktrees and transcript folders in a temp directory. They never touch your real processes or files. CI runs the same command on macOS with Python 3.9 and 3.13.

To see what sweep would do on your own machine, run it without `--apply`. It changes nothing:

```
python3 -I skills/sweep/sweep.py --quick
```

## What fits

- A leftover sweep should recognize but doesn't, with the `ps` line and why it is safe to remove.
- Something sweep removed, or offered to remove, that it should have kept. These matter most. Say what it was and what the output said.
- A clearer question or summary in the skill's steps.

## What doesn't

- Anything that acts on a name, a pattern or an age alone. Every removal needs proof the code checks, and a re-check right before acting.
- New dependencies. sweep is one Python file that uses the standard library and macOS's own tools.
- Linux or Windows support bolted on. It would need its own process and trash rules; open an issue first.

## Style

Match what is there. Comments say why, in a line or two. Every rule that protects something gets a test named after what it protects. Keep user-facing text plain.

## Security

If you find a way to make sweep kill, trash or remove something its rules say it must keep, please report it privately. [SECURITY.md](SECURITY.md) says how.
