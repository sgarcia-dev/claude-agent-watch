export type ToolName = 'codex' | 'opencode' | 'agy' | 'hermes' | 'claude'

// 'lost': no session matched within the link window, so none ever will.
export type RunState = 'starting' | 'running' | 'waiting' | 'done' | 'failed' | 'lost' | 'unknown'

// What the probe reports for one run. Every field may be unknown.
export type Probed = {
  found: boolean
  sessionId: string | null
  state: 'running' | 'done' | 'failed' | 'waiting' | 'unknown'
  model: string | null
  effort: string | null
  contextUsed: number | null
  contextWindow: number | null
  inputTokens: number | null
  outputTokens: number | null
  progress: { done: number; total: number } | null
  steps: number | null
  lastActivity: string | null
  lastActivityAt: number | null
  // The agent's latest message in its own words, on one line.
  lastMessage: string | null
  lastMessageAt: number | null
  endedAt: number | null
  children: number | null
  error: string | null
}

export type Run = {
  // The tool_use_id of the Bash or Agent call that started it.
  id: string
  tool: ToolName
  // Short label: the Agent description, or the CLI's first words.
  label: string
  command: string
  cwd: string | null
  startedAt: number
  flagModel: string | null
  flagEffort: string | null
  isBackground: boolean
  backgroundTaskId: string | null
  // A session ID known before the probe links one (a Claude subagent's agentId).
  sessionHint: string | null
  // A Claude subagent's status as the engine's agent list reports it.
  agentStatus: string | null
  // The agent CLI's process, once the process scan finds it.
  pid: number | null
  // When the foreground Bash call that launched it returned. The run ends
  // then unless its process lives on (a `cmd &` launch).
  returnedAt: number | null
  // When the run ended: its process exited, its tool call or background task
  // ended, or the engine says so.
  exitedAt: number | null
  exitFailed: boolean
  // When the mod first saw the run finished; drives the linger time.
  finishedAt: number | null
  probe: Probed | null
  probedAt: number | null
}

declare module 'claude-code' {
  interface PluginState {
    'agent-watch': {
      runs: Run[]
      lastPollAt: number
    }
  }
}
