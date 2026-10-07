import { describe, expect, mock, test } from 'claude-code/testing'

import { detect, segments } from '../hooks/detect'
import { remaining, statusLine } from '../hooks/format'
import type { Run } from '../types'

const PANE = { plugin: 'agent-watch', component: 'Pane', requestId: 'agent-watch' } as const
const PANE_PROPS = { title: 'Agents', isFocused: false, bodyColumns: 100 }

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

  test('splits on operators but not inside quotes', () => {
    expect(segments(`a 'b; c' && d "e|f"`)).toEqual([['a', 'b; c'], ['d', 'e|f']])
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
      contextUsed: null, contextWindow: null, inputTokens: null, outputTokens: null,
      progress: { done: 1, total: 4 }, steps: null, lastActivity: null, lastActivityAt: null,
      endedAt: null, children: null, error: null,
    }
    expect(remaining(sampleRun({ probe }), 60_000)).toBe(180_000)
  })

  test('the status line hides when nothing runs', () => {
    expect(statusLine([sampleRun({ exitedAt: 5, finishedAt: 5 })], 10)).toBeUndefined()
    expect(statusLine([sampleRun({})], 125_000)).toBe('◆ codex 2m05s')
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
        stdout: JSON.stringify({
          runs: request.runs.map(r => ({
            id: r.id, found: true, sessionId: 'thread-1', state: 'running', model: 'gpt-6.1-sol', effort: 'medium',
            contextUsed: 64_000, contextWindow: 256_000, inputTokens: 100_000, outputTokens: 2_000,
            progress: { done: 2, total: 5 }, steps: 9, lastActivity: 'exec: npm test', lastActivityAt: 1_000_000,
            endedAt: null, children: 0, error: null,
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

    await clock.advance(9_000)

    for (const surface of ['terminal', 'desktop'] as const) {
      const ui = await $.ui.mount({ ...PANE, surface, props: PANE_PROPS })
      expect(await ui.find({ text: /25%/ })).toBeDefined()
      expect(await ui.find({ text: /2\/5/ })).toBeDefined()
      expect(await ui.find({ text: /exec: npm test/ })).toBeDefined()
      await ui.unmount()
    }
    expect(statuses.at(-1)).toBe('◆ codex 8s 2/5 ctx 25%')
  })
})
