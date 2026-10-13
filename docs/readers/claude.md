# Claude subagent reader

The root is `AGENT_WATCH_CLAUDE_HOME`, or `~/.claude` by default.
There is no database. The reader only opens files for reading.
Transcripts live at
`projects/<project-slug>/<parent-session-id>/subagents/agent-<agent-id>.jsonl`.
Sidecars are named `agent-<agent-id>.meta.json`.
Sidecars can contain `agentType`. Their other fields are not used.

A project slug normally replaces slashes in the project path with hyphens.
The reader also supports slugs that replace all nonalphanumeric characters.
Observed subagents can have a cwd below their parent project.
The reader searches the cwd and its ancestors, including real path spellings.
It checks the first transcript record's `cwd` with `common.same_dir`.

Each transcript line is a JSON object.
The first record supplies `agentId`, `timestamp`, and `cwd`.
Timestamps can be ISO dates with a timezone or epoch milliseconds.
`sessionId` in the output is the agent ID, not the parent session ID.
A supplied agent ID holds its link outside the start window.
Claimed IDs and mismatched recorded cwds are rejected even for a held link.
Without an ID, the first timestamp must satisfy `common.in_link_window`.
The recorded `agentType`, if present, must match the run's `command`.
The newest eligible start wins, as required by this task's tool notes.
This differs from the contract's earliest eligible start rule.

Assistant records have `type: assistant` and a `message` object.
The reader uses `message.id`, `model`, `usage`, `content`, and `stop_reason`.
Content blocks use `type`, `text`, and tool use `name`.
Effort comes from the last assistant record's `effort` or `perTurnEffort`.
No effort is inferred from model names.

One response can span several lines with the same `message.id`.
The last usage for each ID counts once toward token totals and `steps`.
Input usage adds `input_tokens`, `cache_read_input_tokens`, and
`cache_creation_input_tokens`. Output usage uses `output_tokens`.
Missing usage components leave the affected total null.
`contextUsed` is the latest assistant response's combined input usage.
Transcripts do not state the window. The reader copies Claude Code
2.1.296's model table: 1000000 for Opus 4.7 and later, Sonnet 5 and later,
Haiku 5.5, Fable and Mythos; 200000 for older models, unless the name ends
in `[1m]`. `CLAUDE_CODE_DISABLE_1M_CONTEXT` forces 200000.
`compactAt` follows Claude Code's rule: the auto-compact window (the model's
window, unless `CLAUDE_CODE_AUTO_COMPACT_WINDOW` or `autoCompactWindow` in
`settings.json` sets a smaller one, at least 100000) less 20000 for the
reply and a 13000 buffer. Opus 5.5 compacts at 967000; a 200K model at
167000. `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` lowers it further.
`DISABLE_AUTO_COMPACT` or `DISABLE_COMPACT` leaves it null. Claude Code can
also tune the window from its servers, which no local file shows. Real
sessions on 2.1.294 auto-compacted at 210K and 312K, below this rule.

The last assistant's `end_turn` means `done`, unless a later user record
shows that work resumed. Other assistant stops mean `running`.
`endedAt` is the timestamp of the final assistant when done.
No assistant records means state `unknown` and no model or context usage.
Recent files had no explicit session failure or abort marker.
A tool result error does not prove the whole session failed.
The reader does not infer `failed` or `waiting` from message text.

`progress` comes only from assistant text, using `common.find_progress`.
`lastActivity` is the latest tool name or the latest text's first line.
Tool arguments are omitted because commands and patterns can hold secrets.
`common.one_line` limits the activity to 120 characters.
`lastActivityAt` is that assistant record's timestamp.
`children` stays null because a tool request does not confirm a child launch.
`error` stays null on success or a missing store.
Read failures return a short error without store paths or transcript content.

Discovery checks at most 128 recent parent directories per project and
256 recent transcript candidates. It has a 1.5 second discovery budget.
The first line read is limited to 262144 bytes. Sidecars use 65536 bytes.
The selected transcript uses `common.tail_lines`, capped at 2000000 bytes.
Partial JSON lines are ignored because Claude may still be writing them.
If a transcript exceeds the tail cap, token totals and `steps` stay null.
Progress and latest response metadata can still come from that tail.
Older sessions outside these bounds may not be found.

Tests build synthetic stores through the root override.
They include isolated entry point execution and split response usage.
The repository needs `tests/__init__.py` for discovery with `-t .` on Python 3.11.

## Quirks found live (2026-10-07)

- The mod's cwd can point at the wrong project folder once the shell moves.
  A known agent ID finds `projects/*/*/subagents/agent-<id>.jsonl` directly
  and skips the folder check.
