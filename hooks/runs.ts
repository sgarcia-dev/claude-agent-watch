// Run bookkeeping and the process-scan merge. Pure functions, no `$`.

import type { Run, ToolName } from '../types'
import { detectWords } from './detect'
import { labelFor } from './format'

export const MAX_RUNS = 30
// After a Bash call returns, keep scanning this long: a script it started
// may still be bringing its agents up.
const SCAN_AFTER_CALL_MS = 15_000
// A background Bash task with no end notice stops counting after this.
const OPEN_TASK_MS = 2 * 60 * 60_000
// A process may show up to this long before or after the call that made it
// (ps start times are whole seconds).
const SLACK_MS = 3_000

// One Bash tool call of this session, for the process scan's time windows.
export type BashCall = {
  id: string
  startedAt: number
  // Foreground: when the call returned. Background: when its task ended.
  endedAt: number | null
  isBackground: boolean
  description: string
  taskId: string | null
}

// One agent CLI process the probe found for this session.
export type SeenProcess = {
  pid: number
  tool: Exclude<ToolName, 'claude'>
  words: string[]
  startedAt: number | null
  cwd: string | null
}

export function newRun(fields: Partial<Run> & Pick<Run, 'id' | 'tool' | 'label' | 'command' | 'startedAt'>): Run {
  return {
    cwd: null,
    flagModel: null,
    flagEffort: null,
    isBackground: false,
    backgroundTaskId: null,
    sessionHint: null,
    agentStatus: null,
    pid: null,
    returnedAt: null,
    exitedAt: null,
    exitFailed: false,
    finishedAt: null,
    probe: null,
    probedAt: null,
    ...fields,
  }
}

export function withRun(runs: Run[] | undefined, run: Run): Run[] {
  return [...(runs ?? []).filter(r => r.id !== run.id), run].slice(-MAX_RUNS)
}

export function patched(runs: Run[] | undefined, id: string, patch: Partial<Run>): Run[] {
  return (runs ?? []).map(r => (r.id === id ? { ...r, ...patch } : r))
}

function isOpen(call: BashCall, now: number): boolean {
  if (call.endedAt === null) return now - call.startedAt < OPEN_TASK_MS
  return now - call.endedAt < SCAN_AFTER_CALL_MS
}

// Scan the process table while a Bash call runs, shortly after one ends,
// and while a background Bash task is open.
export function wantsScan(calls: readonly BashCall[], now: number): boolean {
  return calls.some(c => isOpen(c, now))
}

// Time windows when this session was launching something. An orphaned agent
// process (its shell exited) counts as this session's only inside one.
// A background task's window is its first seconds: its later children stay
// under Claude Code in the process tree, so they need no window.
export function scanWindows(calls: readonly BashCall[]): [number, number | null][] {
  return calls.map(c => [c.startedAt, c.isBackground ? c.startedAt + 5_000 : c.endedAt])
}

export function trimCalls(calls: readonly BashCall[], now: number): BashCall[] {
  return calls.filter(c => c.endedAt === null || now - c.endedAt < OPEN_TASK_MS).slice(-50)
}

function callAt(calls: readonly BashCall[], at: number, now: number): BashCall | undefined {
  return [...calls].reverse().find(c => at >= c.startedAt - SLACK_MS && at <= (c.endedAt ?? now) + SLACK_MS)
}

// Folds one process scan into the runs:
// - a process found for the first time joins the run its launch command
//   created, or becomes a new run (launched by a script, a loop, a wrapper);
// - a run whose process is gone has ended;
// - a foreground run with no process ended when its call returned.
// `processes` is null when the scan did not run; then only the last rule
// applies. `seen` holds pids already shown, so a finished run's process
// that lives on does not come back as a new run.
export function mergeProcesses(
  runs: readonly Run[],
  processes: readonly SeenProcess[] | null,
  calls: readonly BashCall[],
  seen: Set<number>,
  now: number,
): { runs: Run[]; added: number } {
  const next = runs.map(r => ({ ...r }))
  let added = 0
  if (processes !== null) {
    const ordered = [...processes].sort((a, b) => (a.startedAt ?? now) - (b.startedAt ?? now))
    for (const p of ordered) {
      if (seen.has(p.pid) || next.some(r => r.pid === p.pid)) continue
      seen.add(p.pid)
      const at = p.startedAt ?? now
      const owner = next
        .filter(r =>
          r.tool === p.tool && r.pid === null && r.exitedAt === null &&
          at >= r.startedAt - SLACK_MS && at <= (r.returnedAt ?? now) + SLACK_MS)
        .sort((a, b) => a.startedAt - b.startedAt)[0]
      if (owner) {
        owner.pid = p.pid
        owner.cwd = owner.cwd ?? p.cwd
        continue
      }
      const launch = detectWords(p.words, p.cwd)
      const call = callAt(calls, at, now)
      const base = call?.description || labelFor(p.words.join(' '), p.tool)
      const twins = next.filter(r => r.label === base || r.label.startsWith(`${base} (`)).length
      next.push(newRun({
        id: `pid-${p.pid}`,
        tool: p.tool,
        label: twins > 0 ? `${base} (${twins + 1})` : base,
        command: p.words.join(' ').slice(0, 2000),
        cwd: p.cwd ?? launch?.cwd ?? null,
        startedAt: Math.min(at, now),
        flagModel: launch?.flagModel ?? null,
        flagEffort: launch?.flagEffort ?? null,
        isBackground: true,
        backgroundTaskId: call?.isBackground ? call.taskId : null,
        pid: p.pid,
      }))
      added++
    }
  }
  const alive = processes === null ? null : new Set(processes.map(p => p.pid))
  for (const r of next) {
    if (r.exitedAt !== null || r.tool === 'claude') continue
    if (r.pid !== null && alive !== null && !alive.has(r.pid)) r.exitedAt = now
    else if (r.pid === null && r.returnedAt !== null) r.exitedAt = r.returnedAt
  }
  return { runs: next.slice(-MAX_RUNS), added }
}
