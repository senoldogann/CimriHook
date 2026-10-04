// Yalnız dışarıdan doğrulanan, açıkça etkinleştirilmiş görev kapatma deneyi.
import type { SessionMessage } from 'claude-code'
export const CLOSE_INSTRUCTION = 'CimriHook externally verified closure pilot'

type Projection = Omit<SessionMessage, 'handle'>
export type ClosureRequest = {
  sessionId: string
  anchor: Projection[]
  completed: Projection[]
  originalPrefixLength: number
  files: Record<string, string>
  proof: { exit_code: number; tests_unchanged: boolean; command: string[] }
  receipt: string
  evidenceToolUseIds: string[]
}

/** Kalıcı kayıtlar yalnız görünür metin ve araç projeksiyonudur; handle saklanmaz. */
export function project(messages: readonly SessionMessage[]): Projection[] {
  return messages.map(({ role, text, toolUses, toolResults }) => ({
    role, text,
    // decoded result gövdesi turn sonunda boşaltılabilir; provider'ın okuduğu text sabittir.
    toolUses: toolUses.map(({ result: _result, ...tool }) => tool),
    ...(toolResults === undefined ? {} : {
      toolResults: toolResults.map(({ result: _result, ...tool }) => tool),
    }),
  }))
}

/** Yalnız engine'in bu kontrollü /compact komutu için eklediği son kayıt ayrılır. */
export function completedMessages(messages: readonly SessionMessage[]): readonly SessionMessage[] {
  const last = messages.at(-1)
  const command = /^<command-name>\/compact<\/command-name>\s*<command-message>compact<\/command-message>\s*<command-args>CimriHook externally verified closure pilot<\/command-args>$/
  return last?.role === 'user' && last.toolUses.length === 0 &&
    (last.toolResults?.length ?? 0) === 0 && command.test(last.text) ? messages.slice(0, -1) : messages
}

export function canonical(value: unknown): string {
  if (value === undefined) return 'null'
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (typeof value === 'object' && value !== null) {
    const object = value as Record<string, unknown>
    return `{${Object.keys(object).filter((key) => object[key] !== undefined).sort().map((key) => `${JSON.stringify(key)}:${canonical(object[key])}`).join(',')}}`
  }
  return JSON.stringify(value)
}

export function object(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function messages(value: unknown): value is Projection[] {
  return Array.isArray(value) && value.every((message: unknown) =>
    object(message) && (message.role === 'user' || message.role === 'assistant') &&
    typeof message.text === 'string' && Array.isArray(message.toolUses) &&
    (message.toolResults === undefined || Array.isArray(message.toolResults)))
}

export function validRequest(value: unknown): value is ClosureRequest {
  return object(value) && typeof value.sessionId === 'string' &&
    messages(value.anchor) && messages(value.completed) &&
    typeof value.originalPrefixLength === 'number' && Number.isInteger(value.originalPrefixLength) &&
    value.originalPrefixLength >= 0 && value.originalPrefixLength <= value.anchor.length &&
    typeof value.receipt === 'string' && value.receipt.length > 0 &&
    Array.isArray(value.evidenceToolUseIds) && value.evidenceToolUseIds.length >= 2 &&
    value.evidenceToolUseIds.every((id) => typeof id === 'string' && id.length > 0) &&
    object(value.files) && Object.keys(value.files).length > 0 &&
    Object.keys(value.files).every((path) => /^[A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)*$/.test(path) &&
      path.split('/').every((part) => part !== '.' && part !== '..')) &&
    Object.values(value.files).every((content) => typeof content === 'string') &&
    object(value.proof) && value.proof.exit_code === 0 && value.proof.tests_unchanged === true &&
    Array.isArray(value.proof.command) && value.proof.command.length === 4 &&
    value.proof.command.every((item) => typeof item === 'string' && item.length > 0) &&
    (value.proof.command.slice(1).join(' ') === '-m unittest -q' ||
      value.proof.command.join(' ') === '.venv/bin/python -m pytest -q')
}

/** Başarılı işin kullanıcı girdileri ve son yanıtı korunur, araç gözlemleri çıkarılır. */
export function closeMessages(current: readonly SessionMessage[], closure: ClosureRequest): SessionMessage[] {
  const tail = current.slice(closure.anchor.length)
  const users = tail.filter((message) => message.role === 'user' && message.text.length > 0)
    .map((message): SessionMessage => ({ role: 'user', text: message.text, toolUses: [] }))
  const last = tail.filter((message) => message.role === 'assistant' && message.text.length > 0).at(-1)
  if (last === undefined) throw new Error('Completed task has no final answer')
  const ids = new Set(closure.evidenceToolUseIds)
  const calls = tail.flatMap((message) => message.toolUses).filter((tool) => ids.has(tool.tool_use_id))
  const results = tail.flatMap((message) => message.toolResults ?? []).filter((tool) => ids.has(tool.tool_use_id))
  if (calls.length !== ids.size || results.length !== ids.size ||
      calls.some((tool) => tool.isError === true) || results.some((tool) => tool.isError)) {
    throw new Error('Completed task has no successful paired evidence for every selected tool')
  }
  const evidence = tail.flatMap((message): SessionMessage[] => {
    const uses = message.toolUses.filter((tool) => ids.has(tool.tool_use_id))
    const outputs = (message.toolResults ?? []).filter((tool) => ids.has(tool.tool_use_id))
    return uses.length === 0 && outputs.length === 0 ? [] : [{
      role: message.role, text: '', toolUses: uses,
      ...(outputs.length === 0 ? {} : { toolResults: outputs }),
    }]
  })
  // Daha önce yeniden kurulan kayıtlardaki handle'ları tekrar kullanmak resume'da UUID çoğaltır.
  // Yalnız hiç yeniden kurulmamış ortak prefiks engine handle'larını korur.
  const original = current.slice(0, closure.originalPrefixLength)
  const previousReceipts = project(current.slice(closure.originalPrefixLength, closure.anchor.length))
  return [...original, ...previousReceipts, ...users, ...project(evidence), {
    role: 'assistant', toolUses: [],
    text: `${last.text}\n\n[CimriHook externally verified task receipt]\n${closure.receipt}`,
  }]
}
