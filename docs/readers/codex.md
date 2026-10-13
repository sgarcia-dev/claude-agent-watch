# Codex reader

The root is `AGENT_WATCH_CODEX_HOME`, or `~/.codex` by default.
Rollouts live in `sessions/YYYY/MM/DD/rollout-<timestamp>-<id>.jsonl`.
There are no database tables in this reader.
Each JSON line has `type`, `payload`, and usually `timestamp`.
The inspected October 2026 store uses local date folders.
The reader covers both local and UTC dates touched by the link window.
It does not scan the entire history.

The first eight lines, within a 262144 byte read, supply `session_meta`.
Its payload supplies `id`, `timestamp`, `cwd`, and sometimes `agent_path`.
The timestamp must include a timezone.
Linking uses the common five second early and 120 second late window.
It excludes claimed IDs and compares real working directory paths.
The earliest eligible session wins.
A supplied session ID holds the link and bypasses time and cwd matching.
Claimed IDs remain excluded even for an existing link.
The original `startedAt` still selects folders for an existing link.
A missing store or missing match returns `found: false` without an error.

Only `common.tail_lines` reads session activity, capped at 2000000 bytes.
Malformed JSON lines, including an unfinished final line, are skipped.
`turn_context.payload.model` and `effort` fill model and effort.
Codex writes `turn_context` once per turn, so a long session's only one can
sit before the tail. Then the reader looks for the first one in the file's
first 2000000 bytes. Missing values stay null.

`event_msg.payload.type` identifies lifecycle and usage events.
`task_started` sets running and clears endedAt.
`task_complete` sets done. `turn_aborted` sets failed.
The latest lifecycle event wins.
Completion uses `completed_at`, then the record timestamp, for endedAt.
Aborts use the record timestamp. A running session has null endedAt.
Without a terminal or waiting event, state is running.

`token_count.payload.info.last_token_usage.input_tokens` fills contextUsed.
`info.model_context_window` fills contextWindow. It is 95% of the model's
full window (the share in `models_cache.json`).
`compactAt` is `model_auto_compact_token_limit` from the top level of
`config.toml`, else the model's `auto_compact_token_limit` in
`models_cache.json`, else 90% of the full window behind the session's own
report. Real GPT-6 Sol sessions (272K) compacted near 235K. The catalog's
windows change over time, so the session's report is the base.
`info.total_token_usage.input_tokens` and `output_tokens` fill totals.
Cached input and reasoning output are already included in these totals.
They are not added again. Missing usage fields stay null.

`response_item` function_call and custom_tool_call records count as steps.
Steps is null when the file exceeds the tail limit.
This avoids presenting a partial count as the lifetime total.
Tool names come from `payload.name`.
Shell commands come from JSON `arguments` or `input`, using `cmd` or `command`.
Activity uses the tool name and at most 60 command characters.
Other tools and unparsed arguments use only the tool name.
Obvious credential keywords suppress command or message text.
Activity always passes through `common.one_line`.

Assistant message text comes from `content` text blocks or a content string.
Message records must have `role: assistant`.
Agent_message records must have the current `agent_path` as `author`.
Root sessions without agent_path use `/root` as the native author name.
Older event_msg agent_message records use `payload.message`.
User messages, tool outputs, and messages from other agents do not supply progress.
The latest valid `common.find_progress` result fills progress.
The first message line fills lastActivity. Its timestamp fills lastActivityAt.
Missing messages, progress, activity, or timestamps leave those fields null.

A request_user_input or request_user_input_async tool call sets waiting.
The matching call_id output, a user message, or a later tool call clears waiting.
Lifecycle events also clear waiting.

Children use `session_meta.parent_thread_id` or
`session_meta.source.subagent.thread_spawn.parent_thread_id`.
They count distinct matching child IDs only in the scanned date folders.
Children is null when no parent schema is observed in those folders.
Children on later dates are outside this bounded scan.

The reader echoes id and returns found and sessionId for a linked rollout.
Error stays null on success. Exceptions become short class names without paths.
If Codex moves fields or changes record types, update these mappings and fixtures.

## Quirks found live (2026-10-07)

- In code mode, tool calls are `custom_tool_call` records named `exec` whose
  `input` is JavaScript, such as `tools.exec_command({cmd:"cat x"})`. The
  reader parses the last such call, skipping `write_stdin`.
- Spawned children carry `parent_thread_id` in `session_meta`. Runs never
  link to a child; children are counted for the parent.
