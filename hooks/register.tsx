// agent-watch: live status for the agent CLIs and subagents Claude launches.
//
// How it works: a tool.call hook spots Bash commands that start Codex,
// OpenCode, Antigravity (agy) or Hermes, and Agent calls that start Claude
// subagents. Each becomes a Run in $.state. Every 5 seconds the mod runs
// probe/agent_probe.py, which reads each tool's own session files and
// reports model, context use, PROGRESS lines and last activity. The probe
// also lists the agent CLI processes this session started, which catches
// launches inside scripts and loops, and tells when each process exits. A
// pane and a status line entry show the runs; both go away once nothing is
// running.

import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { Probed, Run } from '../types'
import { agentIdFrom, backgroundTaskId, detect, taskEnds } from './detect'
import { mergeProcesses, newRun, patched, scanWindows, trimCalls, wantsScan, withRun } from './runs'
import type { BashCall, SeenProcess } from './runs'
import {
  STATE_STYLE,
  TOOL_STYLE,
  bar,
  contextColor,
  contextShare,
  duration,
  elapsed,
  isActive,
  labelFor,
  modelOf,
  paneText,
  percent,
  progressShare,
  remaining,
  stateOf,
  statusLine,
  steps,
  tokens,
} from './format'

const PANE = 'agent-watch'
// Each check is a local Python run over session files: no tokens, ~50-100 ms.
const POLL_MS = 5_000
// A finished run stays on screen this long before it drops off.
const LINGER_MS = 60_000
// A run that ended with no session or process ever found (a launch that
// failed at once) has nothing to show; it goes sooner.
const EMPTY_LINGER_MS = 15_000
// A run the probe never links and no tool call ends is dropped after this.
const STALE_MS = 6 * 60 * 60_000

const runsAtom = atom({ plugin: 'agent-watch', key: 'runs' } as const, [] as Run[])
const lastPollAtom = atom({ plugin: 'agent-watch', key: 'lastPollAt' } as const, 0)

// Module variables reset on every reload; session.start sets them again.
// pollSoon is a closure over the session's `$`, so tool.call hooks can ask
// for an early check without holding `$` themselves.
let pollSoon: ((ms: number) => void) | null = null
let isPolling = false
// This session's recent Bash calls, and agent pids already shown.
let bashCalls: BashCall[] = []
const seenPids = new Set<number>()

type ProbeAnswer = { runs: (Probed & { id: string })[]; processes?: SeenProcess[] | null; scanError?: string }

// `all` includes finished runs, so their sessions stay claimed.
function probeRequest(runs: Run[], all: Run[], now: number, isScan: boolean): string {
  const claimed = (tool: string, id: string) =>
    all
      .filter(r => r.tool === tool && r.id !== id && r.probe?.sessionId)
      .map(r => r.probe!.sessionId as string)
  return JSON.stringify({
    now,
    runs: runs.map(r => ({
      id: r.id,
      tool: r.tool,
      startedAt: r.startedAt,
      cwd: r.cwd,
      command: r.command,
      endedBy: r.exitedAt,
      sessionId: r.probe?.sessionId ?? r.sessionHint,
      claimed: claimed(r.tool, r.id),
    })),
    scan: isScan
      ? { windows: scanWindows(bashCalls), known: all.map(r => r.pid).filter((p): p is number => p !== null) }
      : undefined,
  })
}

function parseProbe(stdout: string): { answers: Map<string, Probed>; processes: SeenProcess[] | null; scanError: string | null } {
  const answers = new Map<string, Probed>()
  const parsed = JSON.parse(stdout) as ProbeAnswer
  for (const answer of parsed.runs) answers.set(answer.id, answer)
  return { answers, processes: parsed.processes ?? null, scanError: parsed.scanError ?? null }
}

// Folds one probe pass into the runs; returns the new list and the runs
// that finished in this pass.
function applyProbe(
  runs: Run[] | undefined,
  answers: Map<string, Probed>,
  agentStatus: Map<string, string>,
  now: number,
): { runs: Run[]; finished: Run[] } {
  const finished: Run[] = []
  const next = (runs ?? [])
    .map(r => {
      const probe = answers.get(r.id)
      const run: Run = {
        ...r,
        probe: probe ?? r.probe,
        probedAt: probe ? now : r.probedAt,
        agentStatus:
          r.tool === 'claude' && r.sessionHint ? agentStatus.get(r.sessionHint) ?? r.agentStatus : r.agentStatus,
      }
      if (run.finishedAt === null && !isActive(run)) {
        run.finishedAt = now
        finished.push(run)
      }
      return run
    })
    .filter(r => r.finishedAt === null || now - r.finishedAt < (r.probe?.found || r.pid !== null ? LINGER_MS : EMPTY_LINGER_MS))
    .filter(r => now - r.startedAt < STALE_MS)
  return { runs: next, finished }
}

function finishToast(run: Run, now: number): string {
  const style = TOOL_STYLE[run.tool]
  const name = run.tool === 'claude' ? run.label : style.name
  const state = stateOf(run)
  if (state === 'lost') return `${style.glyph} ${name}: no session found to watch`
  const verb = state === 'failed' ? 'failed after' : 'finished in'
  return `${style.glyph} ${name} ${verb} ${duration(elapsed(run, now))}`
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'agent-watch',
      description: 'Show the agent-watch pane (add "poll" to check now, "clear" to drop finished runs)',
    })

    const poll = async (): Promise<void> => {
      if (isPolling) return
      isPolling = true
      try {
        const now = await $.clock.now()
        const before = (await read($, runsAtom)) ?? []
        bashCalls = trimCalls(bashCalls, now)
        const isScan = before.some(isActive) || wantsScan(bashCalls, now)
        // Idle: no process, no state writes, no redraws.
        if (before.length === 0 && !isScan) return
        const toProbe = before.filter(r => (isActive(r) || !r.probe?.found) && now - r.startedAt < STALE_MS)
        let answers = new Map<string, Probed>()
        let processes: SeenProcess[] | null = null
        if (toProbe.length > 0 || isScan) {
          try {
            const result = await $.process.run(['python3', '-I', `${$.plugin.root}/probe/agent_probe.py`], {
              stdin: probeRequest(toProbe, before, now, isScan),
              timeoutMs: 20_000,
            })
            if (result.exitCode === 0) {
              const parsed = parseProbe(result.stdout)
              answers = parsed.answers
              processes = parsed.processes
              if (parsed.scanError) $.ui.log(`agent-watch: process scan failed: ${parsed.scanError}`, { to: 'debug' })
            } else $.ui.log(`agent-watch: probe exited ${result.exitCode}: ${result.stderr.slice(0, 300)}`, { to: 'debug' })
          } catch (err) {
            $.ui.log(`agent-watch: probe failed: ${String(err).slice(0, 300)}`, { to: 'debug' })
          }
        }
        const agents = before.some(r => r.tool === 'claude') ? await $.agent.list().catch(() => []) : []
        const agentStatus = new Map(agents.map(a => [a.id, a.status as string]))

        let finished: Run[] = []
        let added = 0
        await update($, runsAtom, runs => {
          const merged = mergeProcesses(runs ?? [], processes, bashCalls, seenPids, now)
          added = merged.added
          const applied = applyProbe(merged.runs, answers, agentStatus, now)
          finished = applied.finished
          return applied.runs
        })
        if (added > 0) {
          // Found by the process scan: show it, and read its session soon.
          void $.ui.open({ id: PANE, title: 'Agents' })
          pollSoon?.(1_000)
        }
        await update($, lastPollAtom, () => now)
        for (const run of finished) $.ui.toast(finishToast(run, now))

        const after = (await read($, runsAtom)) ?? []
        $.ui.status(statusLine(after, now))
        // One copy per session, so sessions do not overwrite each other.
        const sessionId = await $.session.id().catch(() => 'session')
        await $.fs.write(`${$.plugin.root}/.pane/${sessionId}.txt`, paneText(after, now, now)).catch(() => undefined)
        if (after.length === 0 && before.length > 0) await $.ui.close({ id: PANE }).catch(() => undefined)
        for (const run of after) if (run.pid !== null) seenPids.add(run.pid)
      } finally {
        isPolling = false
      }
    }

    let soon: { cancel: () => void } | null = null
    pollSoon = (ms: number) => {
      soon?.cancel()
      soon = $.clock.after(ms, () => void poll())
    }
    $.clock.every(POLL_MS, () => void poll())

    const runs = (await read($, runsAtom)) ?? []
    if (runs.length > 0) pollSoon(1_000)
    return next(e)
  })

  on('command.run', { command: 'agent-watch' }, async ($, e) => {
    const arg = (e.args ?? '').trim()
    if (arg === 'clear') {
      await update($, runsAtom, runs => (runs ?? []).filter(isActive))
      $.ui.status(statusLine((await read($, runsAtom)) ?? [], await $.clock.now()))
      return { text: 'Cleared finished agent runs.' }
    }
    if (arg === 'poll') pollSoon?.(0)
    const runs = (await read($, runsAtom)) ?? []
    await $.ui.open({ id: PANE, title: 'Agents' })
    const active = runs.filter(isActive).length
    return { text: `agent-watch: ${active} running, ${runs.length - active} recently finished.` }
  })

  // Every Bash call is noted for the process scan's time windows. A call
  // whose command plainly starts an agent CLI also becomes a run at once.
  // Errors in this mod are logged and never stop the call; the engine's
  // .catch replays a call that already ran rather than running it again.
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    let runId: string | null = null
    let callId = ''
    try {
      const startedAt = await $.clock.now()
      callId = e.tool_use_id ?? `bash-${startedAt}`
      const isBackground = e.run_in_background === true
      bashCalls = [...bashCalls, { id: callId, startedAt, endedAt: null, isBackground, description: e.description ?? '', taskId: null }]
      const launch = detect(e.command, await $.session.cwd())
      if (launch) {
        runId = callId
        const run = newRun({
          id: runId,
          tool: launch.tool,
          label: e.description || labelFor(e.command, launch.tool),
          command: e.command.slice(0, 2000),
          cwd: launch.cwd,
          startedAt,
          flagModel: launch.flagModel,
          flagEffort: launch.flagEffort,
          isBackground,
        })
        await update($, runsAtom, runs => withRun(runs, run))
        void $.ui.open({ id: PANE, title: 'Agents' })
        $.ui.status(statusLine((await read($, runsAtom)) ?? [], startedAt))
      }
      pollSoon?.(3_000)
    } catch (err) {
      $.ui.log(`agent-watch: Bash launch check failed: ${String(err).slice(0, 300)}`, { to: 'debug' })
    }

    // A foreground run ends when its call returns, unless its process lives
    // on (`cmd &`); the next poll's process scan decides.
    const settle = async (isFailed: boolean, text: string | undefined): Promise<void> => {
      try {
        const now = await $.clock.now()
        const call = bashCalls.find(c => c.id === callId)
        if (call?.isBackground) call.taskId = backgroundTaskId(text)
        else if (call) call.endedAt = now
        if (runId !== null) {
          const id = runId
          const patch = call?.isBackground
            ? { backgroundTaskId: call.taskId }
            : { returnedAt: now, exitFailed: isFailed }
          await update($, runsAtom, runs => patched(runs, id, patch))
        }
        pollSoon?.(1_500)
      } catch (err) {
        $.ui.log(`agent-watch: Bash result check failed: ${String(err).slice(0, 300)}`, { to: 'debug' })
      }
    }
    let ran
    try {
      ran = await next(e)
    } catch (err) {
      await settle(true, undefined)
      throw err
    }
    await settle(ran.deny !== undefined || ran.isError === true, ran.text)
    return ran
  }).catch(($, e, next) => next(e))

  on('tool.call', { tool: 'Agent' }, async ($, e, next) => {
    let runId: string | null = null
    try {
      const startedAt = await $.clock.now()
      runId = e.tool_use_id ?? `agent-${startedAt}`
      const type = e.subagent_type ?? 'general-purpose'
      const run = newRun({
        id: runId,
        tool: 'claude',
        label: e.description || type,
        command: type,
        cwd: await $.session.cwd(),
        startedAt,
        flagModel: e.model ?? null,
        flagEffort: e.effort ?? null,
      })
      await update($, runsAtom, runs => withRun(runs, run))
      void $.ui.open({ id: PANE, title: 'Agents' })
      $.ui.status(statusLine((await read($, runsAtom)) ?? [], startedAt))
      pollSoon?.(3_000)
    } catch (err) {
      runId = null
      $.ui.log(`agent-watch: Agent launch check failed: ${String(err).slice(0, 300)}`, { to: 'debug' })
    }

    const settle = async (patch: (now: number) => Partial<Run>): Promise<void> => {
      if (runId === null) return
      const id = runId
      try {
        const now = await $.clock.now()
        await update($, runsAtom, runs => patched(runs, id, patch(now)))
        pollSoon?.(1_000)
      } catch (err) {
        $.ui.log(`agent-watch: Agent result check failed: ${String(err).slice(0, 300)}`, { to: 'debug' })
      }
    }
    let ran
    try {
      ran = await next(e)
    } catch (err) {
      await settle(now => ({ exitedAt: now, exitFailed: true }))
      throw err
    }
    // The result says whether the subagent already finished or runs on in
    // the background ("async_launched"); either way it names the agentId.
    const result = (ran.result ?? {}) as { status?: string; agentId?: string }
    const sessionHint = result.agentId ?? agentIdFrom(ran.text)
    const isFinished = ran.deny !== undefined || ran.isError === true || result.status === 'completed'
    await settle(now =>
      isFinished
        ? { sessionHint, exitedAt: now, exitFailed: result.status !== 'completed' }
        : { sessionHint, isBackground: true })
    return ran
  }).catch(($, e, next) => next(e))

  // A background CLI run is finished when Claude Code says so, whatever the
  // tool's own files say yet.
  on('session.append', async ($, e, next) => {
    const stored = await next(e)
    try {
      const ends = taskEnds(e.message.content)
      if (ends.length === 0) return stored
      const runs = (await read($, runsAtom)) ?? []
      const now = await $.clock.now()
      for (const end of ends) {
        for (const call of bashCalls) if (call.taskId === end.taskId) call.endedAt = now
        const run = runs.find(r => r.backgroundTaskId === end.taskId && r.exitedAt === null)
        if (!run) continue
        const exitFailed = end.status !== 'completed'
        await update($, runsAtom, list => patched(list, run.id, { exitedAt: now, exitFailed }))
        pollSoon?.(1_000)
      }
    } catch (err) {
      $.ui.log(`agent-watch: task notice check failed: ${String(err).slice(0, 300)}`, { to: 'debug' })
    }
    return stored
  }).catch(($, e, next) => next(e))

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const runs = (await read($, runsAtom)) ?? []
    const lastPollAt = (await read($, lastPollAtom)) ?? 0
    const now = await $.clock.now()
    const width = Math.max(30, e.props.bodyColumns ?? e.viewport?.columns ?? 80)
    const barWidth = Math.max(6, Math.min(20, Math.floor(width / 6)))
    const active = runs.filter(isActive).length

    if (runs.length === 0) {
      return (
        <Box flexDirection="column">
          <Text dimColor>No agents running. One appears here when Claude starts Codex, OpenCode, agy, Hermes or a subagent.</Text>
        </Box>
      )
    }

    return (
      <Box flexDirection="column">
        <Text dimColor>
          {active} running · {runs.length - active} finished · checked {lastPollAt ? `${duration(now - lastPollAt)} ago` : 'not yet'} · every {POLL_MS / 1000}s
        </Text>
        {runs.map(run => {
          const style = TOOL_STYLE[run.tool]
          const state = stateOf(run)
          const stateStyle = STATE_STYLE[state]
          const m = modelOf(run)
          const ctx = contextShare(run)
          const pr = progressShare(run)
          const left = remaining(run, now)
          const p = run.probe
          const isDone = !isActive(run)
          return (
            <Box key={run.id} flexDirection="column" marginTop={1}>
              <Text wrap="truncate-end">
                <Text color={style.color} bold>
                  {style.glyph} {style.name}
                </Text>
                <Text dimColor={isDone}> {run.label}</Text>
              </Text>
              <Text wrap="truncate-end">
                {'  '}
                <Text color={stateStyle.color}>{stateStyle.word}</Text>
                <Text> {duration(elapsed(run, now))}</Text>
                <Text dimColor> · </Text>
                <Text>{m.model}</Text>
                {m.effort ? <Text dimColor> {m.effort}</Text> : null}
                {!m.isObserved && m.model !== 'default' ? <Text dimColor> (requested)</Text> : null}
              </Text>
              <Text wrap="truncate-end">
                {'  ctx '}
                {ctx === null ? (
                  <Text dimColor>unknown</Text>
                ) : (
                  <Text>
                    <Text color={contextColor(ctx)}>{bar(ctx, barWidth)}</Text> {percent(ctx)}
                    <Text dimColor>
                      {' '}
                      {tokens(p!.contextUsed!)}/{tokens(p!.contextWindow!)}
                    </Text>
                  </Text>
                )}
                {'   progress '}
                {pr === null ? (
                  <Text dimColor>{p?.steps != null ? steps(p.steps) : 'no PROGRESS line yet'}</Text>
                ) : (
                  <Text>
                    <Text color={style.color}>{bar(pr, barWidth)}</Text> {p!.progress!.done}/{p!.progress!.total}
                    {left !== null ? <Text dimColor> ~{duration(left)} left</Text> : null}
                  </Text>
                )}
                {p?.children ? <Text dimColor> · {p.children} children</Text> : null}
              </Text>
              {p?.lastActivity ? (
                <Text wrap="truncate-end">
                  <Text dimColor>{isDone ? '  last: ' : '  now: '}</Text>
                  {p.lastActivity}
                  <Text dimColor>{p.lastActivityAt ? ` · ${duration(now - p.lastActivityAt)} ago` : ''}</Text>
                </Text>
              ) : null}
              {p?.lastMessage ? (
                <Text wrap="truncate-end">
                  <Text dimColor>{isDone ? '  result: ' : '  said: '}</Text>
                  <Text italic>{p.lastMessage}</Text>
                </Text>
              ) : null}
              {p?.error ? (
                <Text wrap="truncate-end" color="warning">
                  {'  reader: '}
                  {p.error}
                </Text>
              ) : null}
              {state === 'starting' && run.probedAt !== null ? (
                <Text dimColor>{'  waiting for its session file to appear'}</Text>
              ) : null}
            </Box>
          )
        })}
      </Box>
    )
  })
}
