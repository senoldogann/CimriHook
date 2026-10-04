// CimriHook: cache ömrü yöneticisi, limit kayıtları ve isteğe bağlı doğrulanmış görev kapatma deneyi.
import type { Register, SessionMessage, ToolResultSummary } from 'claude-code'
import { canonical, CLOSE_INSTRUCTION, closeMessages, completedMessages, object, project, validRequest } from './closure'

const CACHE_LIFETIME_MS = 60 * 60 * 1000
const WARM_MARGIN_MS = 5 * 60 * 1000
const TICK_MS = 60 * 1000
const DEFAULT_MIN_TOKENS = 100_000
const KEEP_RECENT_MESSAGES = 12
const RECENT_RESULTS_SHARE = 0.1
const MASK_MIN_CHARS = 500
const MAX_KEPT_SHARE = 0.6

/** Engine çağrıları hook içindedir; yardımcılar yalnız verilen değerleri dönüştürür. */
function homeOf(configured: string | undefined, home: string | undefined): string {
  return configured ?? `${home ?? '.'}/.cimrihook`
}

function worthCompacting(usage: { context: { tokens?: number }; rateLimits: readonly unknown[] }, min: number): boolean {
  return usage.rateLimits.length > 0 && (usage.context.tokens ?? 0) >= min
}

/** Native kapatma bayraklarında "0" ve "false" açık sayılmaz. */
function flagEnabled(value: string | undefined): boolean {
  return ['1', 'true', 'yes', 'on'].includes(value?.toLowerCase() ?? '')
}

export const register: Register = (on) => {
  const session = {
    running: false, compacted: false, compacting: false,
    lastTurnEnd: undefined as number | undefined,
    limits: undefined as { id: string; lines: string[] } | undefined,
    prefixRecorded: false,
  }
  const closure = { enabled: false, requests: [] as unknown[] }

  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined) session.running = true
    return next(e)
  })

  on('turn.step', async function* ($, e, next) {
    const started = await $.clock.now()
    const result = yield* next(e)
    if (e.agentId === undefined && await $.env.get('CIMRIHOOK_MOD_CLOSE_PROBE') === '1') {
      const base = `${await $.env.get('CIMRIHOOK_HOME')}/closure/${await $.session.id()}`
      const phase = await $.env.get('CIMRIHOOK_PROBE_PHASE')
      closure.requests.push({ index: e.index, model: e.model, effort: e.effort, started,
        ended: await $.clock.now(), usage: result.usage })
      await $.fs.write(`${base}.${phase}.requests.json`, JSON.stringify(closure.requests))
    }
    return result
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if (e.agentId === undefined) {
      session.running = false
      session.lastTurnEnd = await $.clock.now()
      session.compacted = false
      if (await $.env.get('CIMRIHOOK_MOD_CLOSE_PROBE') === '1') {
        const base = `${await $.env.get('CIMRIHOOK_HOME')}/closure/${await $.session.id()}`
        if (await $.fs.exists(`${base}.anchor.json`)) {
          await $.fs.write(`${base}.completed.json`, JSON.stringify(project(await $.session.messages())))
        }
      }
      if (!session.prefixRecorded) {
        try {
          const usage = await $.session.usage({ breakdown: 'full' })
          const rows = usage.context.breakdown?.categories
          if (rows !== undefined) {
            const record = { t: await $.clock.now(), rows: rows.map(({ name, tokens, kind }) => ({ name, tokens, kind })) }
            const home = homeOf(await $.env.get('CIMRIHOOK_HOME'), await $.env.get('HOME'))
            await $.fs.write(`${home}/prefix/${await $.session.id()}.json`, `${JSON.stringify(record)}\n`)
            session.prefixRecorded = true
          }
        } catch (error) {
          $.ui.log(`CimriHook prefix record: ${String(error)}`, { to: 'debug' })
        }
      }
      // Prompt'u tutan hook içinde compact desteklenmez; yalnız tamamlanan ana turdan sonra.
      const boundary = await $.env.get('CIMRIHOOK_MOD_BOUNDARY_TOKENS')
      if (boundary !== undefined && !e.isAborted && !session.compacted && !session.compacting &&
          !flagEnabled(await $.env.get('DISABLE_AUTO_COMPACT')) &&
          !flagEnabled(await $.env.get('DISABLE_COMPACT')) &&
          (await $.session.surfaces()).length > 0 &&
          ((await $.session.usage()).context.tokens ?? 0) >= Number(boundary)) {
        session.compacting = true
        try {
          const outcome = await $.session.compact()
          session.compacted = outcome.skip === undefined
        } catch (error) {
          $.ui.log(`CimriHook boundary compaction: ${String(error)}`, { to: 'debug' })
        } finally {
          session.compacting = false
        }
      }
    }
    return result
  })

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
    // Etkileşimli yüzeyi olmayan -p/SDK oturumlarında bu API desteklenmez.
    if ((await $.session.surfaces()).length === 0) return result
    $.clock.every(TICK_MS, () => {
      void (async () => {
        if (session.running || session.compacted || session.compacting || session.lastTurnEnd === undefined) return
        if (flagEnabled(await $.env.get('DISABLE_AUTO_COMPACT')) ||
            flagEnabled(await $.env.get('DISABLE_COMPACT'))) return
        if ((await $.session.surfaces()).length === 0) return
        const idle = (await $.clock.now()) - session.lastTurnEnd
        if (idle < CACHE_LIFETIME_MS - WARM_MARGIN_MS || idle >= CACHE_LIFETIME_MS) return
        if (!worthCompacting(await $.session.usage(), minTokens)) return
        session.compacting = true
        try {
          const outcome = await $.session.compact()
          session.compacted = outcome.skip === undefined
          if (session.compacted) $.ui.toast('CimriHook compacted this idle session before its prompt cache expired')
        } finally {
          session.compacting = false
        }
      })().catch((error: unknown) => {
        $.ui.log(`CimriHook idle compaction: ${String(error)}`, { to: 'debug' })
      })
    })
    return result
  })

  on('prompt.submit', async ($, e, next) => {
    if (await $.env.get('CIMRIHOOK_MOD_CLOSE_PROBE') === '1' && e.turnId === undefined) {
      closure.enabled = true
      const base = `${await $.env.get('CIMRIHOOK_HOME')}/closure/${await $.session.id()}`
      if (await $.fs.exists(`${base}.request.json`)) {
        const pending: unknown = JSON.parse(await $.fs.read(`${base}.request.json`))
        if (!(object(pending) && pending.consumed === true) && e.text.trim() !== `/compact ${CLOSE_INSTRUCTION}`) {
          return { drop: 'CimriHook closure: pending request requires the controlled /compact command' }
        }
      }
      if (await $.fs.exists(`${base}.armed.json`) && !await $.fs.exists(`${base}.anchor.json`)) {
        await $.fs.write(`${base}.anchor.json`, JSON.stringify(project(await $.session.messages())))
      }
      const phase = await $.env.get('CIMRIHOOK_PROBE_PHASE')
      if (phase !== undefined) {
        const before = project(await $.session.messages())
        await $.fs.write(`${base}.${phase}.start.json`, JSON.stringify({
          messages: before, hasObservation: canonical(before).includes('DISPOSABLE_OBSERVATION_7f928'),
        }))
      }
    }
    return next(e)
  }).catch(($, e, next) => {
    if (closure.enabled && !next.called) return { drop: `CimriHook closure prompt veto: ${next.error.message}` }
    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    const result = await next(e)
    const usd = e.cost?.usd
    if (e.rateLimits.length === 0 || usd === undefined) return result
    try {
      const id = await $.session.id()
      const home = homeOf(await $.env.get('CIMRIHOOK_HOME'), await $.env.get('HOME'))
      const path = `${home}/limits/${id}.jsonl`
      if (session.limits?.id !== id) {
        const lines = (await $.fs.exists(path)) ? (await $.fs.read(path)).split('\n').filter((line) => line !== '') : []
        session.limits = { id, lines }
      }
      const limits = e.rateLimits.map((limit) => ({ kind: limit.kind, percentUsed: limit.percentUsed, resetsAt: limit.resetsAt }))
      session.limits.lines.push(JSON.stringify({ t: await $.clock.now(), usd, limits }))
      await $.fs.write(path, `${session.limits.lines.join('\n')}\n`)
    } catch (error) {
      $.ui.log(`CimriHook limit meter: ${String(error)}`, { to: 'debug' })
    }
    return result
  })

  on('session.compact', async ($, e, next) => {
    if (flagEnabled(await $.env.get('DISABLE_COMPACT'))) {
      return { skip: 'CimriHook: DISABLE_COMPACT is enabled' }
    }
    if (e.trigger === 'auto' && flagEnabled(await $.env.get('DISABLE_AUTO_COMPACT'))) {
      return { skip: 'CimriHook: DISABLE_AUTO_COMPACT is enabled' }
    }
    if (e.agentId === undefined && (e.trigger === 'plugin' || e.trigger === 'manual') &&
        e.instructions === CLOSE_INSTRUCTION && await $.env.get('CIMRIHOOK_MOD_CLOSE_PROBE') === '1') {
      try {
        const base = `${await $.env.get('CIMRIHOOK_HOME')}/closure/${await $.session.id()}`
        const raw: unknown = JSON.parse(await $.fs.read(`${base}.request.json`))
        const anchor: unknown = JSON.parse(await $.fs.read(`${base}.anchor.json`))
        if (!validRequest(raw) || raw.sessionId !== await $.session.id()) {
          return { skip: 'CimriHook closure: invalid external verification' }
        }
        const current = completedMessages(e.messages)
        if (canonical(raw.anchor) !== canonical(anchor) ||
            canonical(project(current)) !== canonical(raw.completed) ||
            canonical(project(current.slice(0, raw.anchor.length))) !== canonical(raw.anchor)) {
          await $.fs.write(`${base}.rejection.json`, JSON.stringify({
            anchorMatched: canonical(raw.anchor) === canonical(anchor),
            completedMatched: canonical(project(current)) === canonical(raw.completed),
            prefixMatched: canonical(project(current.slice(0, raw.anchor.length))) === canonical(raw.anchor),
            current: project(current),
          }))
          return { skip: 'CimriHook closure: transcript or anchor changed' }
        }
        const workspace = await $.env.get('CIMRIHOOK_PROBE_WORKSPACE')
        if (workspace === undefined) return { skip: 'CimriHook closure: missing fixture workspace' }
        for (const [name, expected] of Object.entries(raw.files)) {
          if (await $.fs.read(`${workspace}/${name}`) !== expected) return { skip: `CimriHook closure: verified file changed (${name})` }
        }
        const closed = closeMessages(current, raw)
        // Önce projeksiyon arşivi; tam JSONL yedeğini deney sürücüsü alır.
        await $.fs.write(`${base}.projection-archive.json`, JSON.stringify(project(e.messages)))
        await $.fs.write(`${base}.closed.json`, JSON.stringify({
          messagesBefore: e.messages.length, messagesAfter: closed.length,
          charsBefore: canonical(project(e.messages)).length, charsAfter: canonical(project(closed)).length,
          projected: project(closed),
        }))
        await $.fs.write(`${base}.request.json`, JSON.stringify({ consumed: true }))
        session.compacted = true
        return { messages: closed }
      } catch (error) {
        return { skip: `CimriHook closure veto: ${String(error)}` }
      }
    }
    const main = e.agentId === undefined
    const mask = main && e.trigger === 'auto' && await $.env.get('CIMRIHOOK_MOD_MASK') === '1'
    const masked = mask ? maskOlderResults(e.messages) : undefined
    const result = masked !== undefined && size(masked) <= MAX_KEPT_SHARE * size(e.messages) ?
      { messages: masked } : await next(e)
    if (main && e.trigger !== 'precompute' && result.skip === undefined) session.compacted = true
    return result
  }).catch(($, e, next) => {
    if (e.instructions === CLOSE_INSTRUCTION) return { skip: `CimriHook closure hook veto: ${next.error.message}` }
    return next(e)
  })
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
  const whole = wholeResultsStart(messages)
  const last = lastAssistantRows(messages)
  return messages.flatMap((message, index) => {
    if (message.role === 'assistant') {
      if (index >= last.start && index <= last.end) return [message]
      return message.text === '' && message.toolUses.length === 0 ? [] : [rebuilt(message, [])]
    }
    if (message.toolResults === undefined) return [message]
    return [rebuilt(message, index >= whole ? message.toolResults : message.toolResults.map(masked))]
  })
}

/**
 * Where whole tool results begin: the newest message with tool results always stays whole (the
 * agent is working with it), older ones within KEEP_RECENT_MESSAGES while all kept results stay
 * within RECENT_RESULTS_SHARE of the context. A compaction that kept large recent results would
 * leave the context close to the trigger, and Claude Code stops a turn whose context refills
 * within three turns of a compaction.
 */
function wholeResultsStart(messages: readonly SessionMessage[]): number {
  const budget = RECENT_RESULTS_SHARE * size(messages)
  const floor = Math.max(0, messages.length - KEEP_RECENT_MESSAGES)
  const newest = messages.findLastIndex((message) => message.toolResults !== undefined)
  let kept = 0
  let start = newest < 0 ? messages.length : newest
  for (let index = messages.length - 1; index >= floor; index -= 1) {
    kept += resultChars(messages[index])
    if (index < start && kept > budget) break
    start = Math.min(start, index)
  }
  return start
}

function resultChars(message: SessionMessage | undefined): number {
  return (message?.toolResults ?? []).reduce((sum, result) => sum + result.text.length, 0)
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
      resultChars(message),
    0,
  )
}
