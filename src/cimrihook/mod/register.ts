// CimriHook mod: schedules compaction around the prompt cache's lifetime.
//
// Warm compaction: on a subscription the main conversation's cache lives one hour. When the session
// has sat idle until five minutes before that hour runs out and its context is large, the mod
// compacts while the cache is still warm: the summary request reads the context at the cache-read
// price, and when the person comes back the first request writes the short summary instead of the
// whole conversation.
//
// Cold fallback: when the timer could not run (the machine slept), the first prompt sent into an
// idle session whose cache has expired is preceded by a compaction; that request was going to
// rewrite the whole context anyway.
//
// Mask-first (opt-in, CIMRIHOOK_MOD_MASK=1): an automatic compaction of the main conversation keeps
// every message and replaces older tool results with a short placeholder instead of an LLM summary.
// When that would not shrink the context enough, the engine's own summary runs instead.
import type { Register, SessionMessage, ToolResultSummary } from 'claude-code'

const CACHE_LIFETIME_MS = 60 * 60 * 1000 // main conversation on a subscription
const WARM_MARGIN_MS = 5 * 60 * 1000 // compact this long before the cache expires
const TICK_MS = 60 * 1000
const DEFAULT_MIN_TOKENS = 100_000 // below this a cold rewrite is cheap
const KEEP_RECENT_MESSAGES = 12 // mask-first leaves the newest messages whole
const MASK_MIN_CHARS = 500 // shorter tool results stay
const MAX_KEPT_SHARE = 0.6 // mask-first must remove at least 40% or the summary runs

type Session = {
  running: boolean
  lastTurnEnd: number | undefined
  compacted: boolean
}

export const register: Register = (on) => {
  const session: Session = { running: false, lastTurnEnd: undefined, compacted: false }

  on('turn.start', async ($, e, next) => {
    session.running = true
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if (e.agentId === undefined) {
      session.running = false
      session.lastTurnEnd = await $.clock.now()
      session.compacted = false
    }
    return result
  })

  on('session.start', async ($, e, next) => {
    const result = await next(e)
    const minTokens = Number((await $.env.get('CIMRIHOOK_MOD_MIN_TOKENS')) ?? DEFAULT_MIN_TOKENS)
    $.clock.every(TICK_MS, () => {
      void (async () => {
        if (session.running || session.compacted || session.lastTurnEnd === undefined) return
        const idle = (await $.clock.now()) - session.lastTurnEnd
        if (idle < CACHE_LIFETIME_MS - WARM_MARGIN_MS || idle >= CACHE_LIFETIME_MS) return
        if (!(await worthCompacting($, minTokens))) return
        session.compacted = true
        const outcome = await $.session.compact()
        if (outcome.skip === undefined) {
          $.ui.toast('CimriHook compacted this idle session before its prompt cache expired')
        }
      })().catch(() => undefined)
    })
    return result
  })

  on('prompt.submit', async ($, e, next) => {
    if (e.turnId !== undefined || session.compacted || session.lastTurnEnd === undefined) {
      return next(e)
    }
    const idle = (await $.clock.now()) - session.lastTurnEnd
    const minTokens = Number((await $.env.get('CIMRIHOOK_MOD_MIN_TOKENS')) ?? DEFAULT_MIN_TOKENS)
    if (idle >= CACHE_LIFETIME_MS && (await worthCompacting($, minTokens))) {
      session.compacted = true
      await $.session.compact().catch(() => undefined)
    }
    return next(e)
  })

  on('session.compact', { trigger: 'auto' }, async ($, e, next) => {
    if (e.agentId !== undefined || (await $.env.get('CIMRIHOOK_MOD_MASK')) !== '1') return next(e)
    const masked = maskOlderResults(e.messages)
    if (size(masked) > MAX_KEPT_SHARE * size(e.messages)) return next(e)
    return { messages: masked }
  })
}

/** Is this a subscription session (1-hour cache) with a context large enough to compact? */
async function worthCompacting(
  $: { session: { usage: () => Promise<{ context: { tokens?: number }; rateLimits: readonly unknown[] }> } },
  minTokens: number,
): Promise<boolean> {
  const usage = await $.session.usage()
  return usage.rateLimits.length > 0 && (usage.context.tokens ?? 0) >= minTokens
}

/** Older tool results become a placeholder; the newest messages and errors stay whole. */
function maskOlderResults(messages: readonly SessionMessage[]): SessionMessage[] {
  const cut = Math.max(0, messages.length - KEEP_RECENT_MESSAGES)
  return messages.map((message, index) =>
    index >= cut || message.toolResults === undefined ? message : masked(message, message.toolResults),
  )
}

function masked(message: SessionMessage, results: readonly ToolResultSummary[]): SessionMessage {
  // The tool's stored record (`result`) is left out: Claude Code rebuilds a resumed conversation's
  // tool results from it, which would bring the whole output back.
  const kept = results.map((result) =>
    result.isError || result.text.length < MASK_MIN_CHARS
      ? result
      : {
          tool_use_id: result.tool_use_id,
          isError: result.isError,
          text:
            `[CimriHook removed this ${result.text.split('\n').length}-line tool result to keep the ` +
            'context small; run the tool again if you need it]',
        },
  )
  if (kept.every((result, index) => result === results[index])) return message
  return { role: message.role, text: message.text, toolUses: message.toolUses, toolResults: kept }
}

function size(messages: readonly SessionMessage[]): number {
  return messages.reduce(
    (total, message) =>
      total +
      message.text.length +
      JSON.stringify(message.toolUses.map((use) => use.input)).length +
      (message.toolResults ?? []).reduce((sum, result) => sum + result.text.length, 0),
    0,
  )
}
