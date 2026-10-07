# agent-watch

A Claude Code mod that shows live status for the coding agents Claude launches:
Codex, OpenCode, Antigravity (`agy`), Hermes, and Claude's own subagents.

When Claude starts one, a pane opens with one block per agent:

```
◆ Codex  Codex worker for the reader
  running 4m12s · gpt-6.1-sol medium
  ctx ████░░░░░░ 38% 98k/258k   progress ███░░░░░░░ 3/7 ~5m35s left
  now: exec: python3 -m unittest (20s ago)
```

A short version sits in the status line: `◆ codex 4m12s 3/7 ctx 38%`.
A toast says when an agent finishes or fails. Finished agents stay five
minutes, then drop off. With nothing running, the pane closes and the status
line clears.

## What it shows

| | Codex | OpenCode | Antigravity | Hermes | Claude subagent |
|---|---|---|---|---|---|
| Running time | yes | yes | yes | yes | yes |
| Model and effort really used | yes | yes | yes | yes | yes |
| Context used / window | yes | yes | no | yes | yes (window is a fixed rule) |
| Progress and time left | with PROGRESS lines | with PROGRESS lines | with PROGRESS lines | with PROGRESS lines | with PROGRESS lines |
| What it is doing now | yes | yes | step type only | yes | yes |
| Native child count | yes | yes | no | yes | no |

Every value comes from the tool's own session files on disk. Anything the
files do not say shows as unknown. Nothing is guessed.

### Progress and time left

No agent tool tracks how far along a task is. So agent-watch reads a line the
agent writes itself: `PROGRESS 3/7`. Put this in each task you hand off:

> As you finish each step, print a line `PROGRESS n/total`, for example `PROGRESS 2/5`.

Time left is elapsed time scaled by the steps still to do. It is rough. It
assumes steps take about the same time.

## Install

Needs Claude Code 2.1.292 or newer and `python3` (3.11+) on the PATH.

From a folder on this machine:

```sh
claude plugin marketplace add /path/to/claude-agent-watch
claude plugin install agent-watch@agent-watch
```

From GitHub, typed at a Claude Code prompt:

```
/plugin install agent-watch --marketplace <owner>/claude-agent-watch
```

Answer `y` to add the marketplace, then pick the user scope. In a session that
was already running, type `/reload-plugins`.

## Use

Nothing to do. Launch an agent and the pane opens. The pane opens on its own
only when the terminal is at least 144 columns wide. Otherwise type
`/agent-watch` to open it.

- `/agent-watch`: open the pane.
- `/agent-watch poll`: check now instead of waiting up to 5 seconds.
- `/agent-watch clear`: drop finished agents from the list.

It detects these launch shapes in Bash commands (after `cd`, `VAR=x`, `env`,
`nohup` or `timeout`):

- `codex exec ...` (model from `-m`, effort from `-c model_reasoning_effort=`)
- `opencode run ...` (model and variant from `--model provider/model#variant`)
- `agy -p ...` (from `--model` and `--effort`)
- `hermes chat -q/--query/--query-file/--oneshot ...` (from `--model` and `--reasoning`)

## Change it

The mod has two halves:

- `hooks/`: the Claude Code side, in TypeScript. `register.tsx` holds the
  hooks, the 5-second poll, the pane and the status line. `detect.ts` spots
  launches. `format.ts` turns numbers into text.
- `probe/`: one Python reader per tool. Each reads that tool's session store
  read-only and answers in the shape `probe/CONTRACT.md` defines.
  `docs/readers/<tool>.md` explains each store, for when a tool changes its
  format.

Edit, then check:

```sh
claude plugin validate .
claude plugin test .                         # TypeScript tests
python3 -m unittest discover -s tests -t .   # Python reader tests
```

Installed from a local folder, the session reads the plugin from that folder.
Type `/reload-plugins` to pick up an edit. For live reloading while you edit,
start Claude Code with `claude --plugin-dir /path/to/claude-agent-watch`.

To try a reader by hand:

```sh
echo '{"now": 0, "runs": [{"id": "x", "tool": "codex", "startedAt": 1791397000000, "cwd": null, "sessionId": null, "claimed": []}]}' \
  | python3 -I probe/agent_probe.py
```

## Share it

Push this folder to a GitHub repository. The marketplace file is already in
`.claude-plugin/marketplace.json`. Others install it with the `/plugin install`
line above.

## Limits

- It watches agents Claude launches in this session, not ones you start by
  hand in another terminal.
- A run is matched to its session by start time (within 2 minutes) and folder.
  Two runs of one tool started in the same second could swap.
- Antigravity stores sessions in an undocumented binary format. Its reader
  gets the model, step count, state and output tokens. Context use stays
  unknown.
- Claude subagent context windows use a fixed rule (200k, or 1M for `[1m]`
  models). The transcripts do not state it.
- The pane refreshes every 5 seconds. Each check is a local Python run over session files: no tokens, about 50 to 100 ms of CPU, and nothing at all when no agent is tracked.
- Store formats change between tool versions. Readers were written against
  Codex 0.160.0, OpenCode 2.0.22, agy 1.3.0, Hermes 0.21.5 and Claude Code
  2.1.292.
