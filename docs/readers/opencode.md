# OpenCode reader

The database is `~/.local/share/opencode/opencode.db`.
`AGENT_WATCH_OPENCODE_DATA` overrides its directory.
The reader opens it with `common.connect_ro()`. It never writes to the store.

`session_v2` supplies `id`, `parent_id`, `directory`, and `time_created`.
Only sessions with null `parent_id` can link.
A known session ID holds its link unless it is claimed by another run.
Otherwise `common.pick_session()` chooses the earliest eligible start.
The window includes 5 seconds before launch and excludes 120 seconds after.
Claimed IDs are excluded. Recorded directories must match real paths.
`children` counts rows whose `parent_id` is the linked ID.

The session's `model` is JSON with `providerID`, `id`, and `variant`.
`model` reports `providerID/id`. `effort` reports `variant`.
OpenCode 2 keeps the models.dev catalog in `opencode.db`, table `kv`, key
`models-dev:catalog` (JSON with the catalog as a string in `body`). The
reader uses it first, then `~/.cache/opencode/models.json` (older versions;
`AGENT_WATCH_OPENCODE_CACHE` overrides its directory), which lacks newer
models.
`contextWindow` is the model's `limit.input` when set, else `limit.context`.
OpenCode 2.0.24 caps OpenAI GPT 5.5 and later at 272000 input (400000
context) when the OpenAI login is a ChatGPT account. The reader checks that
by reading only the `type` field of the active `openai` row in the
`credential` table (`oauth`). Real GPT-6 Sol sessions compacted near 243000.
OpenCode's compaction rule could not be read from its compiled binary, so
`compactAt` stays null.
A missing model, catalog, or context limit leaves that field null.

`session_pending` is checked by `session_id` and `type`.
A pending `question` or `permission` sets `state` to `waiting`.
The inspected store had no pending rows. Check type names if this changes.
Without pending work, null `time_idle` means `running`.
With `time_idle`, `idle_outcome` maps `succeeded` to `done`.
`failed` and `interrupted` map to `failed`.
Other idle outcomes mean `unknown`.
`endedAt` is `time_idle` only for recognized terminal states.

`session_message` uses `session_id`, `type`, `seq`, `time_created`, and `data`.
Only `assistant` rows supply progress, usage, or activity.
`steps` counts those rows as model requests, including retries.
The reader loads at most 512 assistant rows in descending `seq` order.
`data.tokens` contains `input`, `output`, `reasoning`, and `cache.read`.
`contextUsed` is the latest assistant's `input` plus `cache.read` plus `cache.write`.
It stays null when that message has no complete usage.

For idle sessions, input totals use `tokens_input` plus `tokens_cache_read`.
Output totals use `tokens_output` plus `tokens_reasoning`.
These `session_v2` counters differed slightly from message sums in inspection.
For nonidle sessions, totals sum recorded assistant usage instead.
They stay null if the 512 row tail is incomplete or usage is unavailable.
Messages with an explicit error and no usage do not add tokens.
Cache write tokens are not added to the contract's input total.

`data.content` has `text`, `reasoning`, and `tool` parts.
`progress` uses `common.find_progress()` on text parts, newest message first.
Reasoning, user text, and tool results do not supply progress.
`lastActivity` uses the latest assistant activity with content.
It prefers the last tool part's `name`, without arguments or results.
Otherwise it uses the first line of the last text part.
`common.one_line()` limits the result to 120 characters.
Tool time uses `completed`, then `ran`, then `created` from the part's `time`.
Text time uses `completed`, then `streamed`, then `created` from message `time`.
`time_created` is the fallback for `lastActivityAt`.
Missing activity or progress leaves the respective fields null.

`id` echoes the run ID. `found` and `sessionId` report the link.
A missing database returns `found: false` without an error.
`error` is null on success. Failures report only the exception type.
SQL work has a 1.5 second deadline. Indexed session lookups bound message reads.

## Quirks found live (2026-10-07)

- Headless `opencode run` sessions get `time_suspended` at start. It does
  not mean paused: a session that is not idle is `running`.
- Idle with a null `idle_outcome` is a pause between turns: `running`.
- The newest assistant message has no `tokens` while streaming. Context
  comes from the newest message that has them.
- Tool arguments live in `content[].state.input` (`command`, `path`).
