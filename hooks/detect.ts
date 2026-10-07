// Spots agent CLI launches in a Bash command and reads the model flags.
// Pure functions, no `$`, so tests can call them directly.

import type { ToolName } from '../types'

export type Launch = {
  tool: Exclude<ToolName, 'claude'>
  flagModel: string | null
  flagEffort: string | null
  cwd: string | null
}

// Splits a shell command into simple commands, respecting quotes.
// Each segment is a list of words with quotes removed.
export function segments(command: string): string[][] {
  const out: string[][] = []
  let words: string[] = []
  let word = ''
  let hasWord = false
  let quote: '"' | "'" | null = null
  const endWord = () => {
    if (hasWord) words.push(word)
    word = ''
    hasWord = false
  }
  const endSegment = () => {
    endWord()
    if (words.length > 0) out.push(words)
    words = []
  }
  for (let i = 0; i < command.length; i++) {
    const c = command[i] ?? ''
    if (quote) {
      if (c === quote) quote = null
      else if (c === '\\' && quote === '"' && i + 1 < command.length) word += command[++i] ?? ''
      else word += c
      continue
    }
    if (c === '"' || c === "'") {
      quote = c
      hasWord = true
    } else if (c === '\\' && i + 1 < command.length) {
      const n = command[++i] ?? ''
      if (n !== '\n') {
        word += n
        hasWord = true
      }
    } else if (c === ';' || c === '&' || c === '|' || c === '\n' || c === '(' || c === ')') {
      endSegment()
    } else if (c === ' ' || c === '\t') {
      endWord()
    } else if (c === '<' || c === '>') {
      // A redirection target is not an argument: skip the operator and its word.
      endWord()
      while (command[i + 1] === '>' || command[i + 1] === '&') i++
      while (command[i + 1] === ' ') i++
      let j = i + 1
      let q: string | null = null
      for (; j < command.length; j++) {
        const d = command[j] ?? ''
        if (q) {
          if (d === q) q = null
          continue
        }
        if (d === '"' || d === "'") q = d
        else if (' \t;&|\n()'.includes(d)) break
      }
      i = j - 1
    } else {
      word += c
      hasWord = true
    }
  }
  endSegment()
  return out
}

const WRAPPERS = new Set(['env', 'nohup', 'time', 'command', 'exec', 'caffeinate', 'timeout', 'gtimeout'])
const ASSIGN = /^[A-Za-z_][A-Za-z0-9_]*=/

// Drops leading `VAR=x`, `env`, `nohup`, `timeout 600` and similar.
function program(words: string[]): string[] {
  let i = 0
  while (i < words.length) {
    const w = words[i] ?? ''
    if (ASSIGN.test(w)) i++
    else if (WRAPPERS.has(w)) {
      i++
      if ((w === 'timeout' || w === 'gtimeout') && /^\d/.test(words[i] ?? '')) i++
      while (i < words.length && (words[i] ?? '').startsWith('-')) i++
    } else break
  }
  return words.slice(i)
}

function base(word: string): string {
  return word.slice(word.lastIndexOf('/') + 1)
}

function flag(words: string[], names: string[]): string | null {
  for (let i = 0; i < words.length; i++) {
    const w = words[i] ?? ''
    for (const n of names) {
      if (w === n && i + 1 < words.length) return literal(words[i + 1] ?? '')
      if (n.startsWith('--') && w.startsWith(n + '=')) return literal(w.slice(n.length + 1))
    }
  }
  return null
}

// A value that is still a shell variable tells us nothing.
function literal(value: string): string | null {
  return value === '' || value.includes('$') ? null : value
}

function codexEffort(words: string[]): string | null {
  for (let i = 0; i < words.length; i++) {
    const w = words[i] === '-c' || words[i] === '--config' ? words[i + 1] ?? '' : ''
    const m = /^model_reasoning_effort\s*=\s*"?([A-Za-z]+)"?$/.exec(w)
    if (m) return m[1] ?? null
  }
  return null
}

function isHelp(words: string[]): boolean {
  return words.some(w => w === '--help' || w === '-h' || w === '--version' || w === '-V')
}

function joinPath(cwd: string | null, dir: string): string | null {
  if (dir.includes('$')) return null
  if (dir.startsWith('/')) return dir
  if (dir.startsWith('~')) return null
  if (!cwd) return null
  return `${cwd.replace(/\/$/, '')}/${dir}`
}

export function detect(command: string, cwd: string | null): Launch | null {
  let here = cwd
  for (const seg of segments(command)) {
    const words = program(seg)
    if (words.length === 0) continue
    const name = base(words[0] ?? '')
    const args = words.slice(1)
    if (name === 'cd' && args[0]) {
      here = joinPath(here, args[0])
      continue
    }
    if (isHelp(args)) continue
    if (name === 'codex' && (args[0] === 'exec' || args[0] === 'e')) {
      return {
        tool: 'codex',
        flagModel: flag(args, ['-m', '--model']),
        flagEffort: codexEffort(args),
        cwd: (() => {
          const dir = flag(args, ['-C', '--cd'])
          return dir ? joinPath(here, dir) : here
        })(),
      }
    }
    if (name === 'opencode' && args[0] === 'run') {
      const model = flag(args, ['-m', '--model'])
      const [id, variant] = model ? model.split('#') : [null, null]
      return {
        tool: 'opencode',
        flagModel: id ?? null,
        flagEffort: variant ?? flag(args, ['--variant']),
        cwd: (() => {
          const dir = flag(args, ['--dir', '--cwd'])
          return dir ? joinPath(here, dir) : here
        })(),
      }
    }
    if (name === 'agy' && args.some(w => w === '-p' || w === '--print' || w.startsWith('--prompt'))) {
      return {
        tool: 'agy',
        flagModel: flag(args, ['--model', '-m']),
        flagEffort: flag(args, ['--effort']),
        cwd: here,
      }
    }
    if (
      name === 'hermes' &&
      ((args[0] === 'chat' &&
        args.some(w => w === '-q' || w === '--query' || w === '--query-file' || w === '--oneshot' || w.startsWith('--query'))) ||
        args[0] === '-z')
    ) {
      return {
        tool: 'hermes',
        flagModel: flag(args, ['--model', '-m']),
        flagEffort: flag(args, ['--reasoning', '--effort']),
        cwd: here,
      }
    }
  }
  return null
}

// The background Bash result names its task: "...with ID: b1z2xn590."
export function backgroundTaskId(text: string | undefined): string | null {
  const m = /background with ID:\s*([A-Za-z0-9_-]+)/.exec(text ?? '')
  return m ? m[1] ?? null : null
}

// The Agent tool's result names the subagent: "agentId: a1b2c3".
export function agentIdFrom(text: string | undefined): string | null {
  const m = /agentId:\s*([A-Za-z0-9_-]+)/.exec(text ?? '')
  return m ? m[1] ?? null : null
}

// Claude Code tells the model a background command ended with a
// <task-notification> row naming the task ID and its status.
export function taskEnds(content: unknown): { taskId: string; status: string }[] {
  const text = typeof content === 'string'
    ? content
    : Array.isArray(content)
      ? content.map(b => (b && typeof b === 'object' && typeof (b as { text?: unknown }).text === 'string' ? (b as { text: string }).text : '')).join('\n')
      : ''
  if (!text.includes('<task-id>')) return []
  const out: { taskId: string; status: string }[] = []
  const re = /<task-id>([^<]+)<\/task-id>[\s\S]*?<status>([^<]+)<\/status>/g
  for (let m = re.exec(text); m; m = re.exec(text)) out.push({ taskId: (m[1] ?? '').trim(), status: (m[2] ?? '').trim() })
  return out
}
