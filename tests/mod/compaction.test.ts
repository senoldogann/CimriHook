import { test, expect, mock } from 'claude-code/testing'
import type { SessionMessage } from 'claude-code'

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
    // Yalnız otomatik kapalıysa kullanıcının /compact isteği çalışmaya devam eder.
    const manual = await $.session.compact({ trigger: 'manual', messages })
    expect(compactions).toBe((automatic ? 1 : 0) + (disabled ? 0 : 1) + (mode === 'DISABLE_COMPACT' ? 0 : 1))
    expect(manual.skip !== undefined).toBe(mode === 'DISABLE_COMPACT')
  })
}
