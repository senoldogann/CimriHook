// CimriHook mod: schedules compaction around the prompt cache's lifetime.
//
// Warm compaction: on a subscription the main conversation's cache lives one hour. When the session
// has sat idle until five minutes before that hour runs out and its context is large, the mod
// compacts while the cache is still warm: the summary request reads the context at the cache-read
// price, and when the person comes back the first request writes the short summary instead of the
// whole conversation.
//
// Cold fallback: when the timer could not run (the machine slept, or the session was resumed after a
// restart), the first prompt sent into an idle session whose cache has expired is preceded by a
// compaction; that request was going to rewrite the whole context anyway.
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

  // A resumed session starts with no turn of its own: take the last response's time from the
  // SessionStart hook input, so the cold fallback also works hours after a restart.
  on('classic.SessionStart', async ($, e, next) => {
    if (e.source === 'resume' && typeof e.seconds_since_last_response === 'number') {
      session.lastTurnEnd = (await $.clock.now()) - e.seconds_since_last_response * 1000
      session.compacted = false
    }
    return next(e)
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

/**
 * Older tool results become a placeholder; the newest messages and errors stay whole.
 *
 * Messages go back rebuilt, without the engine's handle, except the user's prompts and the last
 * assistant message. A message kept by its handle is the engine's own copy, and in Claude Code
 * 2.1.288 such copies tie a resumed session to the history before the compaction: an assistant copy
 * keeps the API message id that its original rows carry, a tool result copy names the original
 * assistant message as its parent, and `--resume` then loads the whole conversation again. The
 * last assistant message stays the engine's own: an automatic compaction can run in the middle of
 * a tool loop, and the API needs that message's thinking block to continue it. Older rows that
 * held only thinking are left out, since a rebuilt message cannot carry thinking.
 */
function maskOlderResults(messages: readonly SessionMessage[]): SessionMessage[] {
  const cut = Math.max(0, messages.length - KEEP_RECENT_MESSAGES)
  const last = lastAssistantRows(messages)
  return messages.flatMap((message, index) => {
    if (message.role === 'assistant') {
      if (index >= last.start && index <= last.end) return [message]
      return message.text === '' && message.toolUses.length === 0 ? [] : [rebuilt(message, [])]
    }
    if (message.toolResults === undefined) return [message]
    return [rebuilt(message, index >= cut ? message.toolResults : message.toolResults.map(masked))]
  })
}

/**
 * The rows of the last assistant message. Claude Code stores one row per content block (thinking,
 * text, tool use), so they are the adjacent assistant rows ending at the last one.
 */
function lastAssistantRows(messages: readonly SessionMessage[]): { start: number; end: number } {
  const end = messages.findLastIndex((message) => message.role === 'assistant')
  let start = end
  while (start > 0 && messages[start - 1]?.role === 'assistant') start -= 1
  return { start, end }
}

function masked(result: ToolResultSummary): ToolResultSummary {
  return result.isError || result.text.length < MASK_MIN_CHARS
    ? result
    : {
        tool_use_id: result.tool_use_id,
        isError: result.isError,
        text:
          `[CimriHook removed this ${result.text.split('\n').length}-line tool result to keep the ` +
          'context small; run the tool again if you need it]',
      }
}

/** The message as the engine builds it from its fields: no handle, so no link to the old one. */
function rebuilt(message: SessionMessage, results: readonly ToolResultSummary[]): SessionMessage {
  return results.length === 0
    ? { role: message.role, text: message.text, toolUses: message.toolUses }
    : { role: message.role, text: message.text, toolUses: message.toolUses, toolResults: [...results] }
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
