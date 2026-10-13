# Probe contract

The mod runs `python3 -I probe/agent_probe.py` every 5 seconds. It sends the
runs it tracks as JSON on stdin and reads one JSON status per run on stdout.
Each tool has one reader module: `probe/<tool>.py`.

Python 3.11 standard library only. No network. Never write to a tool's files.
Open SQLite stores read-only with `common.connect_ro(path)`.

## Input (stdin)

```json
{
  "now": 1791400000000,
  "runs": [
    {
      "id": "toolu_01ABC",
      "tool": "codex",
      "startedAt": 1791399000000,
      "cwd": "/Users/me/src/repo",
      "command": "codex exec -m gpt-6.1-sol ...",
      "sessionId": null,
      "claimed": ["ses_other_run_already_linked"]
    }
  ]
}
```

- `tool` is one of `codex`, `opencode`, `agy`, `hermes`, `claude`.
- `startedAt` and `now` are epoch milliseconds.
- `sessionId` is null until a reader links the run to a session. The mod sends
  back whatever the last answer said, so a link holds once made.
- `claimed` lists session IDs already linked to other runs of the same tool.
  Never link to one of those.
- `endedBy` (optional) is when the launching command ended, in epoch ms. A
  session that started more than 2 seconds after it belongs to another run.
- For `claude`, `sessionId` is the subagent's agent ID when the mod knows it,
  and `command` holds the subagent type.

## Linking a run to a session

When `sessionId` is null, pick the tool's session that:

1. started at or after `startedAt - 5000` and before `startedAt + 120000`,
   and no later than `endedBy + 2000` when `endedBy` is set,
2. is not in `claimed`,
3. started earliest among what is left, preferring sessions whose recorded
   folder matches `cwd` (compare real paths). When none match, ignore the
   folder: the mod's `cwd` is a best guess, since the Bash tool's folder moves.

If nothing matches yet, answer `found: false`. The session may not exist yet.

## Output (stdout)

```json
{ "runs": [ { "id": "toolu_01ABC", "found": true, "sessionId": "...", ... } ] }
```

One object per input run, same `id`. Every field below is present. Use `null`
for anything unknown. Never guess a value.

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Echo of the input run ID |
| `found` | bool | A session is linked |
| `sessionId` | string or null | The linked session or conversation ID |
| `state` | `running`, `done`, `failed`, `waiting` or `unknown` | `waiting` means blocked on a question or permission |
| `model` | string or null | Model the session actually used, as the tool names it |
| `effort` | string or null | Reasoning effort or variant actually used |
| `contextUsed` | int or null | Input tokens of the latest model request (context occupancy) |
| `contextWindow` | int or null | The most context the tool lets that model use, from local data only |
| `compactAt` | int or null | Context size at which the tool compacts (summarizes) the session on its own, when known |
| `inputTokens` | int or null | Total input tokens so far (cached included) |
| `outputTokens` | int or null | Total output tokens so far (reasoning included) |
| `progress` | `{"done": n, "total": m}` or null | Latest `PROGRESS n/m` line the agent wrote |
| `steps` | int or null | Count of model requests or tool calls so far |
| `lastActivity` | string or null | One line, at most 300 characters: the last tool call and its argument |
| `lastActivityAt` | int or null | Epoch ms of that activity |
| `lastMessage` | string or null | One line, at most 300 characters: the agent's latest message, skipping messages that are only a PROGRESS marker |
| `lastMessageAt` | int or null | Epoch ms of that message |
| `endedAt` | int or null | Epoch ms the session finished, when known |
| `children` | int or null | Native subagents this session started |
| `error` | string or null | Why the reader failed for this run (never raise) |

Build `lastActivity` with `common.tool_summary(name, args)` (the whole
command, a file's name, or a pattern) and `lastMessage` with
`common.message_line(text)`. Both replace credential-looking values with
`[hidden]` (`common.redact`). The pane cuts each line to its width.

## Process scan

When the request has `"scan": {"windows": [[start, end|null], ...], "known": [pid, ...]}`,
the answer also has `processes`: the agent CLI processes this Claude Code
session started (`probe/processes.py`). A process counts when:

1. the probe's nearest `claude` ancestor is also its ancestor, or
2. it is orphaned (no `claude` ancestor at all, as after `cmd &` in a shell
   that exited) and started inside one of `windows` (2 s before to 3 s
   after). The mod sends each foreground Bash call's span and each
   background call's first 5 seconds.

A task process counts: `codex [global options] exec|e`, `opencode run`,
`agy -p`, `hermes chat -q|--query*|--oneshot` or `hermes -z`, also behind an
interpreter (`bash shim`, `node codex.js`, Hermes's `python3 -c` launcher).
Servers (`codex app-server`, `opencode serve`) do not. A matching process
under another matching process (a wrapper's real binary) counts once.

Each entry: `pid`, `tool`, `words` (exact argv from `KERN_PROCARGS2`, the
tool name first; Codex with its subcommand second), `startedAt` (exact,
`KERN_PROC_PID`), `shellStartedAt` (start of the shell Claude Code ran the
Bash call in, or null for an orphan), `cwd` (from `lsof`, skipped for
`known` pids). If the scan fails, `processes` is null and `scanError` says
why.

## Reader module shape

```python
def probe(run: dict, now_ms: int) -> dict:
    out = common.empty_result(run)
    ...
    return out
```

A reader must finish in under 2 seconds on a large store. Read only the tail
of large files (`common.tail_lines`). Catch every exception and put a short
message in `error`.

## Progress lines

Task packets ask the agent to print `PROGRESS <done>/<total>` (for example
`PROGRESS 3/7`) as it finishes each step. `common.find_progress(text)` returns
the last such pair in a text. Readers scan the agent's own messages, newest
first, and stop at the first hit.

## Store locations and test overrides

Each reader takes its store root from an environment variable, so tests can
point it at a temporary folder of synthetic data. Real data never goes in
tests or fixtures.

| Tool | Variable | Default |
|---|---|---|
| codex | `AGENT_WATCH_CODEX_HOME` | `~/.codex` |
| opencode | `AGENT_WATCH_OPENCODE_DATA` | `~/.local/share/opencode` (models catalog: `AGENT_WATCH_OPENCODE_CACHE`, default `~/.cache/opencode`) |
| agy | `AGENT_WATCH_AGY_HOME` | `~/.gemini/antigravity-cli` |
| hermes | `AGENT_WATCH_HERMES_HOME` | `~/.hermes` |
| claude | `AGENT_WATCH_CLAUDE_HOME` | `~/.claude` |

Tests live in `tests/test_<tool>.py`, use `unittest`, build their fixtures in a
temporary folder, and run with `python3 -m unittest discover -s tests -t .`.
