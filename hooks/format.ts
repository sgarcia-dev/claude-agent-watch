// Turns run data into short display strings. Pure functions.

import type { Run, RunState, ToolName } from '../types'

export const TOOL_STYLE: Record<ToolName, { glyph: string; color: string; name: string }> = {
  codex: { glyph: '◆', color: '#10a37f', name: 'Codex' },
  opencode: { glyph: '■', color: '#4f8cff', name: 'OpenCode' },
  agy: { glyph: '▲', color: '#f4b400', name: 'Antigravity' },
  hermes: { glyph: '●', color: '#c678dd', name: 'Hermes' },
  claude: { glyph: '✻', color: 'claude', name: 'Claude' },
}

export function duration(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m${String(s % 60).padStart(2, '0')}s`
  const h = Math.floor(m / 60)
  return `${h}h${String(m % 60).padStart(2, '0')}m`
}

export function tokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`
  if (n >= 1000) return `${Math.round(n / 1000)}k`
  return String(n)
}

export function stateOf(run: Run): RunState {
  // The engine's own agent list is the best word on a Claude subagent.
  if (run.agentStatus === 'completed') return 'done'
  if (run.agentStatus === 'failed' || run.agentStatus === 'killed') return 'failed'
  if (run.agentStatus === 'waiting') return 'waiting'
  const probed = run.probe?.found ? run.probe.state : null
  if (probed === 'done' || probed === 'failed') return probed
  if (run.exitedAt !== null) return run.exitFailed ? 'failed' : 'done'
  if (probed === 'waiting') return 'waiting'
  if (probed === 'running') return 'running'
  return run.probe?.found ? 'unknown' : 'starting'
}

export function isActive(run: Run): boolean {
  const s = stateOf(run)
  return s !== 'done' && s !== 'failed'
}

export function endOf(run: Run): number | null {
  if (isActive(run)) return null
  return run.probe?.endedAt ?? run.exitedAt ?? run.finishedAt
}

export function elapsed(run: Run, now: number): number {
  return (endOf(run) ?? now) - run.startedAt
}

// Model the session really used, else the launch flag, else "default".
export function modelOf(run: Run): { model: string; effort: string | null; isObserved: boolean } {
  const p = run.probe
  if (p?.model) return { model: p.model, effort: p.effort ?? run.flagEffort, isObserved: true }
  if (run.flagModel) return { model: run.flagModel, effort: run.flagEffort, isObserved: false }
  return { model: 'default', effort: run.flagEffort, isObserved: false }
}

export function contextShare(run: Run): number | null {
  const p = run.probe
  if (!p || p.contextUsed === null || !p.contextWindow) return null
  return Math.min(1, p.contextUsed / p.contextWindow)
}

export function progressShare(run: Run): number | null {
  const pr = run.probe?.progress
  if (!pr || pr.total <= 0) return null
  return pr.done / pr.total
}

// Rough time left: elapsed time scaled by the steps still to do.
export function remaining(run: Run, now: number): number | null {
  const share = progressShare(run)
  if (share === null || share <= 0 || share >= 1 || !isActive(run)) return null
  const spent = elapsed(run, now)
  return (spent * (1 - share)) / share
}

export function bar(share: number, width: number): string {
  const cells = Math.max(1, width)
  const full = Math.round(share * cells)
  return '█'.repeat(full) + '░'.repeat(cells - full)
}

export function percent(share: number): string {
  return `${Math.round(share * 100)}%`
}

export function contextColor(share: number): string {
  if (share >= 0.85) return 'error'
  if (share >= 0.6) return 'warning'
  return 'success'
}

export const STATE_STYLE: Record<RunState, { word: string; color: string }> = {
  starting: { word: 'starting', color: 'subtle' },
  running: { word: 'running', color: 'success' },
  waiting: { word: 'needs input', color: 'warning' },
  done: { word: 'done', color: 'inactive' },
  failed: { word: 'failed', color: 'error' },
  unknown: { word: 'running?', color: 'subtle' },
}

// One compact entry per run for the status line.
export function statusEntry(run: Run, now: number): string {
  const style = TOOL_STYLE[run.tool]
  const parts = [`${style.glyph} ${run.tool === 'claude' ? run.label.slice(0, 14) : run.tool}`, duration(elapsed(run, now))]
  const pr = progressShare(run)
  if (pr !== null) parts.push(`${run.probe!.progress!.done}/${run.probe!.progress!.total}`)
  const ctx = contextShare(run)
  if (ctx !== null) parts.push(`ctx ${percent(ctx)}`)
  if (stateOf(run) === 'waiting') parts.push('needs input')
  return parts.join(' ')
}

export function statusLine(runs: readonly Run[], now: number): string | undefined {
  const active = runs.filter(isActive)
  if (active.length === 0) return undefined
  const shown = active.slice(0, 4).map(r => statusEntry(r, now))
  if (active.length > 4) shown.push(`+${active.length - 4}`)
  return shown.join(' · ')
}

// First words of the command that launched a CLI run, for its label.
export function labelFor(command: string, tool: ToolName): string {
  const at = command.search(new RegExp(`\\b${tool}\\b`))
  const tail = at >= 0 ? command.slice(at) : command
  const line = tail.split('\n')[0] ?? ''
  return line.length > 48 ? line.slice(0, 47) + '…' : line
}
