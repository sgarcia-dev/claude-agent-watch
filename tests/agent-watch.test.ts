import { describe, expect, mock, test } from 'claude-code/testing'

import { detect, segments, taskEnds } from '../hooks/detect'
import { isActive, remaining, stateOf, statusLine } from '../hooks/format'
import { mergeProcesses } from '../hooks/runs'
import type { BashCall, SeenProcess } from '../hooks/runs'
import type { Run } from '../types'

const PANE = { plugin: 'agent-watch', component: 'Pane', requestId: 'agent-watch' } as const
const PANE_PROPS = {
  title: 'Agents', isFocused: false, bodyColumns: 100, placement: 'dock' as const,
  scroll: { offset: 0, bodyRows: 40 }, view: {},
}

describe('detect', () => {
  test('finds a Codex run behind variables and redirects', () => {
    const launch = detect(
      'P=/tmp/w; codex exec --yolo -m gpt-6.1-sol -c model_reasoning_effort=medium -C /repo --json -o $P/x.md - < $P/p.md > $P/log 2> $P/err',
      '/home',
    )
    expect(launch).toEqual({ tool: 'codex', flagModel: 'gpt-6.1-sol', flagEffort: 'medium', cwd: '/repo' })
  })

  test('reads an OpenCode model and variant', () => {
    const launch = detect('cd sub && opencode run --model "opencode-go/glm-5.3-flash#high" --format json "task"', '/repo')
    expect(launch).toEqual({ tool: 'opencode', flagModel: 'opencode-go/glm-5.3-flash', flagEffort: 'high', cwd: '/repo/sub' })
  })

  test('a model still in a shell variable counts as unknown', () => {
    expect(detect('opencode run --model "$MODEL" --format json x', '/r')?.flagModel).toBeNull()
  })

  test('finds agy print mode and Hermes one-shot chat', () => {
    expect(detect("agy -p 'do it' --model gemini-3.8-flash-medium --effort medium", '/r')?.tool).toBe('agy')
    expect(detect('hermes chat --oneshot -Q -q "task" --model gpt-6-sol --reasoning medium', '/r')).toEqual({
      tool: 'hermes',
      flagModel: 'gpt-6-sol',
      flagEffort: 'medium',
      cwd: '/r',
    })
  })

  test('ignores help, version, and names inside quoted prompts', () => {
    expect(detect('codex exec --help', '/r')).toBeNull()
    expect(detect('opencode --version', '/r')).toBeNull()
    expect(detect('echo "run codex exec later; opencode run x"', '/r')).toBeNull()
    expect(detect('git status', '/r')).toBeNull()
  })

  test('reads background task endings from a notification row', () => {
    const text = '<task-notification>\n<task-id>b123</task-id>\n<status>failed</status>\n</task-notification>'
    expect(taskEnds([{ type: 'text', text }])).toEqual([{ taskId: 'b123', status: 'failed' }])
    expect(taskEnds('plain text')).toEqual([])
  })

  test('splits on operators but not inside quotes', () => {
    expect(segments(`a 'b; c' && d "e|f"`)).toEqual([['a', 'b; c'], ['d', 'e|f']])
  })

  test('sees through the perl alarm wrapper the OpenCode and Hermes skills use', () => {
    const launch = detect(`M=x; perl -e 'alarm shift; exec @ARGV' 2700 opencode run --model "opencode-go/glm-5.3-flash#high" --format json "task"`, '/r')
    expect(launch?.tool).toBe('opencode')
    expect(launch?.flagModel).toBe('opencode-go/glm-5.3-flash')
  })

  test('a heredoc body is data: an apostrophe in it hides nothing, a launch in it starts nothing', () => {
    const writesScript = `cat > run.sh <<'EOF'\ncodex exec -m gpt-6.1-sol "$1"\nEOF\nchmod +x run.sh`
    expect(detect(writesScript, '/r')).toBeNull()
    const thenLaunches = `cat > p.txt <<EOF\nDon't stop until it's done.\nEOF\nagy -p "$(cat p.txt)" --model gemini-3.8-flash-medium`
    expect(detect(thenLaunches, '/r')?.tool).toBe('agy')
  })
})

function sampleRun(fields: Partial<Run>): Run {
  return {
    id: 't1',
    tool: 'codex',
    label: 'codex exec',
    command: 'codex exec',
    cwd: '/r',
    startedAt: 0,
    flagModel: null,
    flagEffort: null,
    isBackground: true,
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

describe('format', () => {
  test('time left scales elapsed time by the steps still to do', () => {
    const probe = {
      found: true, sessionId: 's', state: 'running' as const, model: null, effort: null,
      contextUsed: null, contextWindow: null, compactAt: null, inputTokens: null, outputTokens: null,
      progress: { done: 1, total: 4 }, steps: null, lastActivity: null, lastActivityAt: null,
      lastMessage: null, lastMessageAt: null, endedAt: null, children: null, error: null,
    }
    expect(remaining(sampleRun({ probe }), 60_000)).toBe(180_000)
  })

  test('a run never linked past the window is lost, not running forever', () => {
    const notFound = { found: false } as Run['probe']
    expect(stateOf(sampleRun({ probe: notFound, probedAt: 60_000 }))).toBe('starting')
    const lost = sampleRun({ probe: notFound, probedAt: 200_000 })
    expect(stateOf(lost)).toBe('lost')
    expect(isActive(lost)).toBe(false)
  })

  test('the status line hides when nothing runs', () => {
    expect(statusLine([sampleRun({ exitedAt: 5, finishedAt: 5 })], 10)).toBeUndefined()
    expect(statusLine([sampleRun({})], 125_000)).toBe('◆ codex 2m05s')
  })

  test('many agents fold into one summary', () => {
    const many = (['codex', 'codex', 'opencode', 'hermes', 'claude'] as const).map((tool, i) => sampleRun({ id: `r${i}`, tool, label: 'w' }))
    expect(statusLine(many, 10_000)).toBe('5 running · ◆2 ■1 ●1 ✻1')
  })
})

describe('process scan', () => {
  const proc = (pid: number, startedAt: number, words = ['codex', 'exec', '-m', 'gpt-6.1-sol']): SeenProcess =>
    ({ pid, tool: 'codex', words, startedAt, cwd: '/w' })
  const call = (fields: Partial<BashCall>): BashCall =>
    ({ id: 'b1', startedAt: 0, endedAt: 1_000, isBackground: false, description: 'Two Codex workers', taskId: null, ...fields })

  test('a launch hidden in a script becomes a run, labeled by its Bash call', () => {
    const calls = [call({ endedAt: null, isBackground: true, taskId: 'bt1' })]
    const merged = mergeProcesses([], [proc(11, 500), proc(12, 600)], calls, new Set(), 2_000)
    expect(merged.added).toBe(2)
    expect(merged.runs.map(r => r.label)).toEqual(['Two Codex workers', 'Two Codex workers (2)'])
    expect(merged.runs[0]?.flagModel).toBe('gpt-6.1-sol')
    expect(merged.runs[0]?.backgroundTaskId).toBe('bt1')
  })

  test('a detected launch takes its process, and ends when the process exits', () => {
    const run = sampleRun({ id: 'toolu_1', startedAt: 1_000, isBackground: false, returnedAt: 1_200 })
    const seen = new Set<number>()
    const first = mergeProcesses([run], [proc(21, 1_100)], [], seen, 2_000)
    expect(first.added).toBe(0)
    expect(first.runs[0]?.pid).toBe(21)
    expect(first.runs[0]?.exitedAt).toBeNull()
    const later = mergeProcesses(first.runs, [], [], seen, 9_000)
    expect(later.runs[0]?.exitedAt).toBe(9_000)
    expect(isActive(later.runs[0]!)).toBe(false)
  })

  test('a foreground run with no process ended when its call returned', () => {
    const run = sampleRun({ startedAt: 1_000, isBackground: false, returnedAt: 1_500 })
    expect(mergeProcesses([run], [], [], new Set(), 3_000).runs[0]?.exitedAt).toBe(1_500)
    expect(mergeProcesses([run], null, [], new Set(), 3_000).runs[0]?.exitedAt).toBe(1_500)
  })

  test('a process is labeled by the call whose shell started it, not a later call', () => {
    const calls = [
      call({ id: 'a', startedAt: 1_000, endedAt: null, isBackground: true, description: 'Script A' }),
      call({ id: 'b', startedAt: 1_900, endedAt: 2_000, description: 'Read the pane' }),
    ]
    const fromA = { ...proc(41, 2_100), shellStartedAt: 1_400 }
    expect(mergeProcesses([], [fromA], calls, new Set(), 3_000).runs[0]?.label).toBe('Script A')
    const orphan = proc(42, 1_950)
    expect(mergeProcesses([], [orphan], calls, new Set(), 3_000).runs[0]?.label).toBe('Read the pane')
  })

  test('a process already shown never comes back as a new run', () => {
    expect(mergeProcesses([], [proc(31, 500)], [], new Set([31]), 2_000).added).toBe(0)
  })
})

describe('the mod', () => {
  test('a Codex launch shows in the pane, then the probe fills it in', async ($, on) => {
    const clock = mock.clock(on, { now: 1_000_000 })
    const statuses: (string | undefined)[] = []
    on('session.start', (_$, e) => ({ cwd: e.cwd }))
    on('command.register', () => ({ value: { command: 'agent-watch' } }))
    on('session.cwd', () => ({ value: '/repo' }))
    on('ui.open', () => ({ value: { isPlaced: true } }))
    on('ui.close', () => ({ value: undefined }))
    on('ui.status', (_$, e) => {
      statuses.push(e.text)
      return { value: undefined }
    })
    on('ui.toast', () => ({ value: undefined }))
    on('ui.log', () => ({ value: undefined }))
    on('agent.list', () => ({ value: [] }))
    on('tool.call', { tool: 'Bash' }, () => ({ result: { stdout: '', stderr: '', interrupted: false }, text: 'Command running in background with ID: b123.' }))
    on('process.run', (_$, e) => {
      const request = JSON.parse(e.init?.stdin ?? '{}') as { runs: { id: string }[] }
      return { value: {
        exitCode: 0,
        stderr: '',
        isStdoutTruncated: false,
        isStderrTruncated: false,
        stdout: JSON.stringify({
          processes: [],
          runs: request.runs.map(r => ({
            id: r.id, found: true, sessionId: 'thread-1', state: 'running', model: 'gpt-6.1-sol', effort: 'medium',
            contextUsed: 64_000, contextWindow: 256_000, compactAt: 128_000, inputTokens: 100_000, outputTokens: 2_000,
            progress: { done: 2, total: 5 }, steps: 9, lastActivity: 'exec: npm test', lastActivityAt: 1_000_000,
            lastMessage: 'Running the tests before I edit the reader.', lastMessageAt: 1_000_000, endedAt: null, children: 0, error: null,
          })),
        }),
      } }
    })

    await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
    await $.tool.call({
      tool: 'Bash',
      tool_use_id: 'toolu_1',
      command: 'codex exec --yolo -m gpt-6.1-sol -c model_reasoning_effort=medium - < p.md > log',
      description: 'Codex worker for the reader',
      run_in_background: true,
    })

    for (const surface of ['terminal', 'desktop'] as const) {
      const ui = await $.ui.mount({ ...PANE, surface, props: PANE_PROPS })
      expect(await ui.find({ text: /Codex worker for the reader/ })).toBeDefined()
      expect(await ui.find({ text: /starting/ })).toBeDefined()
      await ui.unmount()
    }

    await clock.advance(6_000)

    for (const surface of ['terminal', 'desktop'] as const) {
      const ui = await $.ui.mount({ ...PANE, surface, props: PANE_PROPS })
      expect(await ui.find({ text: /50%/ })).toBeDefined()
      expect(await ui.find({ text: /64k used · compacts at 128k of 256k/ })).toBeDefined()
      expect(await ui.find({ text: /2\/5/ })).toBeDefined()
      expect(await ui.find({ text: /now \d+s ago: exec: npm test/ })).toBeDefined()
      expect(await ui.find({ text: /said \d+s ago: Running the tests before I edit the reader/ })).toBeDefined()
      await ui.unmount()
    }
    expect(statuses.at(-1)).toBe('◆ codex 5s 2/5 ctx 50%')
  })
})
