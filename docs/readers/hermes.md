# Hermes reader

The default root is `~/.hermes`.
`AGENT_WATCH_HERMES_HOME` overrides it for synthetic tests.
The reader opens `state.db` and `profiles/*/state.db` through
`common.connect_ro()`. It closes every connection. It never copies the store.

## Linking

The `sessions` table supplies `id`, `source`, `cwd`, and `started_at`.
Only `cli`, `oneshot`, `tui`, and `api_server` sources qualify.
`parent_session_id` must be null. Chat platforms and child sessions do not qualify.
Times in this store are floating point epoch seconds.
The reader converts them to milliseconds for the contract.

A new link searches from five seconds before the run start to two minutes after.
The upper bound is exclusive. Claimed IDs are excluded.
A recorded cwd must match the run cwd through `common.same_dir()`.
A missing session cwd remains eligible. The earliest eligible start wins.
Root and profile candidates compete in the same selection.
A supplied session ID keeps its link outside the time window.
It must still have an allowed source, no parent, and not be claimed.

## Status fields

`model` comes from `sessions.model`.
`effort` comes from `model_config.reasoning_config.effort`.
`contextUsed` comes from `model_config._usage_anchor.prompt_tokens`.
`model_config` is JSON. Missing or malformed configuration leaves these null.
There is no fallback from cumulative tokens to context occupancy.

`contextWindow` uses `context_length_cache.yaml` beside the selected database,
then the root cache if no entry matches.
The reader parses flat `model@base_url: number` entries under `context_lengths:`.
It matches `sessions.model` and `sessions.billing_base_url` exactly.
Quoted keys and trailing numeric comments are supported. General YAML is not.
Missing keys, model names, or billing URLs leave the window null.

`inputTokens` and `outputTokens` use the session totals `input_tokens` and
`output_tokens`. Cache and reasoning breakdowns are not added again.
`steps` uses `api_call_count`, not `tool_call_count` or `message_count`.
`lastActivity` uses `last_activity_description` through `common.one_line()`.
`lastActivityAt` uses `last_activity_at`. `endedAt` uses `ended_at`.
Missing stored counters or activity fields remain null.

Without `ended_at`, state is `running`.
With it, `agent_close`, `cli_close`, `session_reset`, and `session_switch` mean `done`.
`startup_orphan_reap` means `failed` because the session was reaped as an orphan.
Other finished reasons mean `unknown`. Waiting is not inferred.

`children` counts sessions whose `parent_session_id` equals the linked ID.
`async_delegations` also exists, with `delegation_id`, `origin_session`,
`parent_session_id`, and `state`. Its rows are not added to the child count.
They track delegation delivery and could count an existing child twice.

## Progress and bounds

Progress reads `messages.session_id`, `role`, `content`, `timestamp`, and `id`.
Only assistant messages qualify. If `active` exists, only active rows qualify.
The reader checks the newest 100 messages, ordered by timestamp and then ID.
It reads at most the final 20,000 characters of each message.
`common.find_progress()` supplies the latest valid pair in the first matching row.
Progress stays null when this bounded tail has no valid marker.

Link searches read only rows in the start window. Known IDs use direct lookup.
The installed store indexes session starts, parent IDs, and session message times.
SQLite work has a 1.7 second deadline and a 50 millisecond busy timeout.
Cache reads are limited to 256,000 characters per file.
Missing stores return `found: false` without an error.
Read failures put a short exception type in `error`; they do not expose content.

## Quirks found live (2026-10-07)

- `task-hermes` launches with `--source tool`; that source counts as a CLI run.
- With Codex-backed models the assistant text, including PROGRESS lines,
  is in `messages.codex_message_items`, not `content`. Both are read.
- `last_activity_description` is often generic ("receiving stream
  response"). The latest `messages.tool_calls` entry is used instead.
