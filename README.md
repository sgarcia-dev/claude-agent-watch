# agent-watch

A Claude Code mod that shows live status for the coding agents Claude launches:
Codex, OpenCode, Antigravity (`agy`), Hermes, and Claude's own subagents.

When Claude starts one, a pane opens with one block per agent:

```
◆ Codex  Codex worker for the reader
  running 4m12s · gpt-6.1-sol medium
  ctx ████░░░░░░ 41% · 186k used · compacts at 450k of 475k   progress ███░░░░░░░ 3/7 ~5m35s left
  now 20s ago: exec_command: python3 -m unittest discover -s tests -t .
  said 45s ago: The reader tests pass; next I check the Hermes store.
```

The context bar fills toward the point where the agent's own tool compacts
(summarizes) its session, since that is when it starts losing detail. When
that point is unknown, it fills toward the model's whole window.

A short version sits in the status line: `◆ codex 4m12s 3/7 ctx 41%`.
A toast says when an agent finishes or fails. Finished agents stay one
minute, then drop off (`/agent-watch clear` drops them now). With nothing running, the pane closes and the status
line clears.

## What it shows

| | Codex | OpenCode | Antigravity | Hermes | Claude subagent |
|---|---|---|---|---|---|
| Running time | yes | yes | yes | yes | yes |
| Model and effort really used | yes | yes | yes | yes | yes |
| Context used / window | yes | yes | no | yes | yes |
| Where it compacts | yes | no | no | yes | yes |
| Progress and time left | with PROGRESS lines | with PROGRESS lines | with PROGRESS lines | with PROGRESS lines | with PROGRESS lines |
| What it is doing now (last tool call) | yes | yes | step type only | yes | yes |
| What it said last (its own words) | yes | yes | yes | yes | yes |
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

It finds agents two ways, so nothing about how you launch them has to change:

1. **The command.** A Bash command that plainly starts an agent shows at
   once, labeled with the command's description. It reads through `cd`,
   `VAR=x`, `env`, `nohup`, `timeout` and the `perl -e 'alarm shift; exec
   @ARGV' N` timeout wrapper, and skips heredoc bodies.
   - `codex exec ...` (model from `-m`, effort from `-c model_reasoning_effort=`)
   - `opencode run ...` (model and variant from `--model provider/model#variant`)
   - `agy -p ...` (from `--model` and `--effort`)
   - `hermes chat -q/--query/--query-file/--oneshot ...` (from `--model` and `--reasoning`)
2. **The process list.** Each check also looks for those agent processes
   started by this Claude Code session: anything under its process, or a
   process detached with `&` that started while one of its Bash commands ran.
   This catches launch scripts (`./run.sh`), loops that start several
   workers, and wrappers. A row ends when its process exits.

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
  hand in another terminal. One edge: an agent you start by hand, detached,
  in the same second one of this session's Bash commands runs, could be
  counted here.
- Exact process arguments and start times come from macOS `sysctl` calls.
  Elsewhere the scan falls back to `ps` output, which is coarser: labels
  for processes found this way may name the wrong Bash command. Only macOS
  has been tested.
- A run is matched to its session by start time (within 2 minutes) and folder.
  Two runs of one tool started in the same second could swap.
- Antigravity stores sessions in an undocumented binary format. Its reader
  gets the model, step count, state and output tokens. Context use stays
  unknown.
- Context windows and compaction points come from each tool's own rules,
  read from its code and checked against real sessions in October 2026
  (Claude Code 2.1.296, Codex, OpenCode 2.0.24, Hermes). Claude transcripts
  do not state the window, so the Claude reader copies Claude Code's model
  table: 1M for current models, 200K for older ones. Claude Code can also
  move its compaction point from its servers, which no local file shows.
  OpenCode's compaction point is not known, so its bar fills to the window.
  A tool update can change any of these.
- The pane refreshes every 5 seconds. Each check is a local Python run over
  session files and the process list: no tokens, about 50 to 100 ms of CPU.
  It runs only while an agent is tracked, a Bash command is running or ended
  in the last 15 seconds, or a background Bash task is open.
- Store formats change between tool versions. Readers were written against
  Codex 0.160.0, OpenCode 2.0.22, agy 1.3.0, Hermes 0.21.5 and Claude Code
  2.1.292.
