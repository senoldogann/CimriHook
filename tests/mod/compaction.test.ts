import { test, expect, mock } from 'claude-code/testing'
import type { SessionMessage } from 'claude-code'

declare const setTimeout: (fn: () => void, ms: number) => unknown

const messages: SessionMessage[] = [{ role: 'user', text: 'Keep working', toolUses: [] }]
const turn = { answer: 'Done', durationMs: 10, isAborted: false, turnId: 'turn-1',
  reason: 'answer' } as const

for (const mode of ['enabled', 'DISABLE_AUTO_COMPACT', 'DISABLE_COMPACT', 'headless', 'false-flags']) {
  test(`compaction waits for turn completion: ${mode}`, async ($, on) => {
    const clock = mock.clock(on)
    const env: Record<string, string> = { CIMRIHOOK_MOD_BOUNDARY_TOKENS: '100000',
      ...(mode.startsWith('DISABLE_') ? { [mode]: '1' } : {}),
      ...(mode === 'false-flags' ? { DISABLE_AUTO_COMPACT: '0', DISABLE_COMPACT: 'false' } : {}) }
    const automatic = mode === 'enabled' || mode === 'false-flags'
    let compactions = 0
    let prompts = 0
    const logs: string[] = []
    on('env.get', (_, e) => ({ value: env[e.name] }))
    on('session.surfaces', () => ({ value: mode === 'headless' ? [] : ['terminal'] }))
    on('session.usage', () => ({ value: { startedAt: 0,
      context: { tokens: 200_000, window: 1_000_000 }, rateLimits: [
        { kind: 'five_hour', percentUsed: 10, resetsAt: 5_000_000 },
      ] } }))
    on('session.compact', () => { compactions += 1; return { messages } })
    on('session.messages', () => ({ value: messages }))
    on('session.id', () => ({ value: 'session-1' }))
    on('ui.log', (_, e) => { logs.push(JSON.stringify(e)); return { value: undefined } })
    on('prompt.submit', (_, e) => { prompts += 1; return { text: e.text } })
    on('turn.complete', () => ({ text: 'Done' }))
    on('classic.SessionStart', () => ({}))
    await clock.advance(3_600_000)
    await $.classic.SessionStart({ source: 'resume', seconds_since_last_response: 3601 })
    await $.prompt.submit({ text: 'Keep working' })
    expect(logs).toEqual([])
    expect(compactions).toBe(0)
    expect(prompts).toBe(1)
    await $.turn.complete(turn)
    expect(logs).toEqual([])
    expect(compactions).toBe(automatic ? 1 : 0)
    const auto = await $.session.compact({ trigger: 'auto', messages })
    const disabled = mode === 'DISABLE_AUTO_COMPACT' || mode === 'DISABLE_COMPACT'
    expect(auto.skip !== undefined).toBe(disabled)
    expect(compactions).toBe((automatic ? 1 : 0) + (disabled ? 0 : 1))
    // With only automatic compaction disabled, the user's /compact still runs.
    const manual = await $.session.compact({ trigger: 'manual', messages })
    expect(compactions).toBe((automatic ? 1 : 0) + (disabled ? 0 : 1) + (mode === 'DISABLE_COMPACT' ? 0 : 1))
    expect(manual.skip !== undefined).toBe(mode === 'DISABLE_COMPACT')
  })
}

for (const scenario of [
  { name: 'long response', minutes: 35, cached: 200_000, compacts: 1 },
  { name: 'expired during response', minutes: 65, cached: 200_000, compacts: 0 },
  { name: 'no cache reported', minutes: 35, cached: 0, compacts: 0 },
]) {
  test(`warm compaction counts from cached request start: ${scenario.name}`, async ($, on) => {
    const clock = mock.clock(on)
    let compactions = 0
    const logs: string[] = []
    on('session.start', (_, e) => ({ cwd: e.cwd }))
    on('session.surfaces', () => ({ value: ['terminal'] }))
    on('env.get', () => ({ value: undefined }))
    on('session.usage', () => ({ value: { startedAt: 0,
      context: { tokens: 200_000, window: 1_000_000 },
      rateLimits: [{ kind: 'five_hour', percentUsed: 10, resetsAt: 5_000_000 }] } }))
    on('session.compact', () => { compactions += 1; return { messages } })
    on('ui.toast', () => ({ value: undefined }))
    on('ui.log', (_, e) => { logs.push(JSON.stringify(e)); return { value: undefined } })
    on('turn.start', (_, e) => ({ turnId: e.turnId }))
    on('turn.complete', () => ({ text: 'Done' }))
    on('turn.step', async function* (_, e) {
      await clock.advance(scenario.minutes * 60_000)
      const usage = { input_tokens: 0, output_tokens: 1,
        cache_read_input_tokens: scenario.cached, cache_creation_input_tokens: 0 }
      yield { kind: 'stop', stopReason: 'end_turn', usage }
      return { turnId: e.turnId, index: e.index, answer: 'Done', toolUses: [],
        stopReason: 'end_turn', usage }
    })
    await $.session.start({ cwd: '/workspace' })
    await $.turn.start({ turnId: 'turn-1' })
    for await (const event of $.turn.step({ turnId: 'turn-1', index: 0,
      model: 'claude-opus-5-5', effort: 'medium' })) {
      expect(event.kind).toBe('stop')
    }
    await $.turn.complete({ ...turn, durationMs: scenario.minutes * 60_000 })
    expect(compactions).toBe(0)
    await clock.advance(20 * 60_000)
    await clock.settle()
    // Let the native timer drain its setImmediate/RPC queue without moving the clock.
    for (let attempt = 0; attempt < 20 && compactions === 0; attempt += 1) {
      await new Promise<void>((resolve) => setTimeout(resolve, 10))
    }
    expect(logs).toEqual([])
    expect(compactions).toBe(scenario.compacts)
    await clock.advance(4 * 60_000)
    await clock.settle()
    expect(compactions).toBe(scenario.compacts)
  })
}
