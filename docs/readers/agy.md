# Antigravity reader

The default root is `~/.gemini/antigravity-cli`.
`AGENT_WATCH_AGY_HOME` overrides it for synthetic tests.
Each `conversations/<uuid>.db` is one conversation.
The filename supplies `sessionId`.
All databases are opened through `common.connect_ro()` and closed promptly.
The reader never uses history, logs, OAuth files, or brain content.

The reader relies on these tables and columns:

- `steps`: `idx`, `step_type`, `status`, `metadata`, `step_payload`.
- `gen_metadata`: `idx`, `data`.
- `trajectory_metadata_blob`: `id`, `data`, with `id = 'main'`.

Blobs use protobuf wire encoding.
The decoder handles varints, length-delimited fields, fixed32, and fixed64.
It follows explicit field paths. It does not search arbitrary text for values.
Field names were checked against descriptors in the installed CLI binary.
Values and relationships were checked in at least two real conversations.
Recheck those paths after a CLI format change.

In the paths below, a dot means another nested protobuf message.
`steps.metadata.1` is `created_at`, a protobuf Timestamp.
Timestamp fields 1 and 2 hold seconds and nanoseconds.
The first step's creation time supplies the link start.
The fallback is `trajectory_metadata_blob.data.2`, its `created_at`.
If neither exists, the fallback is the file's `st_birthtime` when available.
Modification time is never used as a creation time.

`trajectory_metadata_blob.data.1` repeats `workspaces`.
Its field 1 is `workspace_folder_absolute_uri`.
Its field 2 is the git root and is never used as cwd.
Local file URIs are decoded and compared with `common.same_dir()`.
Multiple distinct workspaces make linking unavailable.
`common.pick_session()` applies the start window, claims, and earliest choice.
Recorded workspaces must match the run's cwd. Missing workspaces allow linking.
An existing session link holds outside the window, unless it is claimed.

Step type 14 is user input. Type 15 is an assistant response.
Type 132 is a generic tool step.
Status 1 is pending, 2 running, 8 generating, and 11 queued.
These statuses report `running`. Status 9 reports `waiting`.
Status 6 is canceled, 7 error, and 12 interrupted. They report `failed`.
Status 3 is done. It reports `done` after 30 seconds of file inactivity.
The database, WAL, and rollback journal modification times count as activity.
A recently complete step and unsupported statuses report `unknown`.
Only the latest step determines state. An older tool error can be recovered.

`gen_metadata.data.1` is `chat_model`.
Its field 19 is `response_model`, used for `model`.
Its field 4 is `usage`. Usage fields 2, 3, and 5 are input, output, and cache-read tokens.
Usage fields 9 and 10 are thinking and response output tokens.
Field 3 includes both. Their equality was checked across multiple conversations.
`outputTokens` sums field 3 once per generation row.
Missing output counts or more than 512 generations leave that total null.
Input and cache-read counts were found, but full context accounting was unverified.
Therefore `inputTokens` and `contextUsed` stay null.

`steps.step_payload.20` is the assistant response message.
Its field 1 is `response`, field 8 `modified_response`, and field 3 thinking.
Progress uses field 8 when present, otherwise field 1.
It scans assistant responses newest first with `common.find_progress()`.
User messages, thinking, and tool output cannot supply progress.
`steps.metadata.4.2` is the tool call name used for `lastActivity`.
Assistant activity uses the label `assistant response` to avoid content exposure.
`common.one_line()` formats activity. Tool arguments are never returned.
`lastActivityAt` is the latest recorded step timestamp from fields 1, 6, 7, 8, 22, or 32.
These are creation, viewability, generation end, completion, chunk end, and start.
`steps` is the number of rows in the steps table.

`effort`, `contextWindow`, and `children` stay null because verified data is absent.
`endedAt` stays null because step completion does not prove the exact session end.
Missing progress, model, or activity remains null. Read errors expose only exception types.
New links inspect at most 128 recently modified databases.
Progress inspects at most 128 recent steps. Blobs are limited to 2 MB.
SQLite reads have a 1.5 second deadline and short busy waits.
Older unlinked conversations can fall outside these bounds.
