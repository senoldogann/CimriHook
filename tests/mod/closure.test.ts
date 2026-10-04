import { test, expect } from 'claude-code/testing'
import type { SessionMessage } from 'claude-code'
import { CLOSE_INSTRUCTION, project } from '../../src/cimrihook/mod/closure'

const prefix: SessionMessage[] = [
  { role: 'user', text: 'Keep this original constraint', toolUses: [], handle: 'prefix-user' },
  { role: 'assistant', text: 'Ready', toolUses: [], handle: 'prefix-answer' },
]
const current: SessionMessage[] = [...prefix,
  { role: 'user', text: 'Fix the task', toolUses: [], handle: 'task-user' },
  { role: 'user', text: '', toolUses: [], toolResults: [
    { tool_use_id: 'read-1', text: 'DISPOSABLE_OBSERVATION_7f928', isError: false },
  ] },
  { role: 'user', text: 'Also preserve my correction', toolUses: [], toolResults: [
    { tool_use_id: 'mixed-1', text: 'extra tool observation', isError: false },
  ] },
  { role: 'assistant', text: '', toolUses: [{ tool_use_id: 'edit-1', tool: 'Edit',
    input: { file_path: '/workspace/calc.py', old_string: 'wrong', new_string: 'fixed' }, text: 'File updated' }] },
  { role: 'user', text: '', toolUses: [], toolResults: [
    { tool_use_id: 'edit-1', text: 'File updated', isError: false },
  ] },
  { role: 'assistant', text: '', toolUses: [{ tool_use_id: 'test-1', tool: 'Bash',
    input: { command: 'python3 -m unittest -q' }, text: 'Ran 1 test\n\nOK' }] },
  { role: 'user', text: '', toolUses: [], toolResults: [
    { tool_use_id: 'test-1', text: 'Ran 1 test\n\nOK', isError: false },
  ] },
  { role: 'assistant', text: 'Fixed and tested', toolUses: [] },
]

for (const failure of ['none', 'manual', 'auto-disabled', 'all-disabled', 'verification', 'prefix', 'file', 'archive', 'receipt-write', 'request-read', 'hook-catch', 'evidence']) {
  test(`closure ${failure}: veto failures before downstream summary`, async ($, on) => {
    const base = '/probe/closure/session-1'
    const closure = {
      sessionId: 'session-1', anchor: project(prefix), completed: project(current),
      originalPrefixLength: prefix.length,
      evidenceToolUseIds: failure === 'evidence' ? ['edit-1', 'missing'] : ['edit-1', 'test-1'],
      files: { 'calc.py': 'fixed', 'test_calc.py': 'tests' },
      proof: { exit_code: failure === 'verification' ? 1 : 0, tests_unchanged: true,
        command: ['python3', '-m', 'unittest', '-q'] }, receipt: 'Externally verified',
    }
    if (failure === 'prefix') closure.anchor[0].text = 'Changed prefix'
    const files = new Map<string, string>([
      [`${base}.request.json`, JSON.stringify(closure)],
      [`${base}.anchor.json`, JSON.stringify(project(prefix))],
      ['/workspace/calc.py', failure === 'file' ? 'changed' : 'fixed'],
      ['/workspace/test_calc.py', 'tests'],
    ])
    let summaries = 0
    const env: Record<string, string> = { CIMRIHOOK_HOME: '/probe',
      CIMRIHOOK_MOD_CLOSE_PROBE: '1', CIMRIHOOK_PROBE_WORKSPACE: '/workspace',
      CIMRIHOOK_PROBE_PHASE: 'closed' }
    if (failure === 'auto-disabled') env.DISABLE_AUTO_COMPACT = '1'
    if (failure === 'all-disabled') env.DISABLE_COMPACT = '1'
    on('env.get', (_, e) => {
      if (failure === 'hook-catch') throw new Error('unexpected hook failure')
      return { value: env[e.name] }
    })
    on('session.id', () => ({ value: 'session-1' }))
    on('fs.exists', (_, e) => ({ value: files.has(e.path) }))
    on('fs.read', (_, e) => {
      if (failure === 'request-read' && e.path.endsWith('.request.json')) throw new Error('read failed')
      const value = files.get(e.path)
      if (value === undefined) throw new Error(`Missing ${e.path}`)
      return { value }
    })
    on('fs.write', (_, e) => {
      if (failure === 'archive' && e.path.endsWith('.projection-archive.json')) throw new Error('archive failed')
      if (failure === 'receipt-write' && e.path.endsWith('.closed.json')) throw new Error('receipt failed')
      files.set(e.path, e.text)
      return { value: undefined }
    })
    on('session.messages', () => ({ value: current }))
    on('session.compact', () => { summaries += 1; return { messages: [] } })
    const controlled = failure === 'manual' ? [...current, { role: 'user' as const, toolUses: [],
      text: `<command-name>/compact</command-name>\n<command-message>compact</command-message>\n<command-args>${CLOSE_INSTRUCTION}</command-args>` }] : current
    const result = await $.session.compact({ trigger: failure === 'manual' ? 'manual' : 'plugin', messages: controlled,
      instructions: CLOSE_INSTRUCTION })
    expect(summaries).toBe(0)
    if (failure === 'none' || failure === 'manual' || failure === 'auto-disabled') {
      expect(result.messages?.[0].handle).toBe('prefix-user')
      const saved = JSON.parse(files.get(`${base}.closed.json`) ?? '{}')
      expect(saved.projected[0].text).toBe('Keep this original constraint')
      expect(saved.projected.map((m: SessionMessage) => m.text).join('\n')).toContain('Also preserve my correction')
      expect(saved.projected.map((m: SessionMessage) => m.text).join('\n')).not.toContain('DISPOSABLE_OBSERVATION_7f928')
      expect(saved.projected.flatMap((m: SessionMessage) => m.toolResults ?? []).some((tool) => tool.text.includes('Ran 1 test'))).toBe(true)
      expect(files.get(`${base}.request.json`)).toBe('{"consumed":true}')
    } else {
      expect(result).toEqual(expect.objectContaining({ skip: expect.any(String) }))
      expect(files.has(`${base}.closed.json`)).toBe(false)
      expect(files.get(`${base}.request.json`)).toBe(JSON.stringify(closure))
    }
  })
}
