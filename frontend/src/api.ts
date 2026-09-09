import type { ChatEvent, ChatMessage, ChatSummary } from './types'

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message)
  }
}

const writeHeaders = {
  'Content-Type': 'application/json',
  'X-BI-Agent': 'web',
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function parseEvent(name: string, data: unknown): ChatEvent {
  if (!isRecord(data)) throw new Error('SSE data必须是对象')
  if (name === 'status' && (data.stage === 'thinking' || data.stage === 'querying' || data.stage === 'answering')) {
    return { event: name, data: { stage: data.stage } }
  }
  if (name === 'artifact') return { event: name, data }
  if (name === 'message' &&
      (data.role === 'user' || data.role === 'assistant') &&
      typeof data.id === 'string' && typeof data.content === 'string' &&
      Array.isArray(data.artifacts) &&
      (data.status === 'complete' || data.status === 'error') &&
      typeof data.created_at === 'string') {
    return { event: name, data: data as ChatMessage }
  }
  if (name === 'error' && typeof data.code === 'string' && typeof data.message === 'string') {
    return { event: name, data: { code: data.code, message: data.message } }
  }
  if (name === 'done' && (data.status === 'complete' || data.status === 'error')) {
    return { event: name, data: { status: data.status } }
  }
  if (!['status', 'artifact', 'message', 'error', 'done'].includes(name)) {
    throw new Error('未知SSE事件')
  }
  throw new Error('SSE事件数据不符合契约')
}

export function createSseParser(onEvent: (event: ChatEvent) => void) {
  const decoder = new TextDecoder('utf-8', { fatal: true })
  let buffer = ''

  function readFrame(frame: string) {
    let name: string | undefined
    let dataText: string | undefined
    for (const line of frame.split('\n')) {
      if (!line) continue
      if (line.startsWith('event: ') && name === undefined) {
        name = line.slice(7)
      } else if (line.startsWith('data: ') && dataText === undefined) {
        dataText = line.slice(6)
      } else {
        throw new Error('SSE帧格式不合法')
      }
    }
    if (!name || dataText === undefined) throw new Error('SSE帧缺少event或data')
    let data: unknown
    try {
      data = JSON.parse(dataText)
    } catch {
      throw new Error('SSE JSON不合法')
    }
    onEvent(parseEvent(name, data))
  }

  function drain() {
    buffer = buffer.replaceAll('\r\n', '\n')
    for (;;) {
      const boundary = buffer.indexOf('\n\n')
      if (boundary < 0) return
      readFrame(buffer.slice(0, boundary))
      buffer = buffer.slice(boundary + 2)
    }
  }

  return {
    push(bytes: Uint8Array) {
      try {
        buffer += decoder.decode(bytes, { stream: true })
      } catch {
        throw new Error('SSE不是有效UTF-8')
      }
      drain()
    },
    finish() {
      try {
        buffer += decoder.decode()
      } catch {
        throw new Error('SSE不是有效UTF-8')
      }
      drain()
      if (buffer) throw new Error('SSE流以不完整帧结束')
    },
  }
}

async function apiError(response: Response): Promise<ApiError> {
  const payload: unknown = await response.json().catch(() => null)
  const message = isRecord(payload) && typeof payload.message === 'string'
    ? payload.message
    : `请求失败（${response.status}）`
  return new ApiError(response.status, message)
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: 'same-origin', ...init })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<T>
}

export function listChats() {
  return json<ChatSummary[]>('/api/chats')
}

export function createChat() {
  return json<ChatSummary>('/api/chats', {
    method: 'POST', headers: writeHeaders, body: '{}',
  })
}

export function loadMessages(chatId: string) {
  return json<ChatMessage[]>(`/api/chats/${chatId}/messages`)
}

export function renameChat(chatId: string, title: string) {
  return json<ChatSummary>(`/api/chats/${chatId}`, {
    method: 'PATCH', headers: writeHeaders, body: JSON.stringify({ title }),
  })
}

export async function deleteChat(chatId: string) {
  const response = await fetch(`/api/chats/${chatId}`, {
    method: 'DELETE', credentials: 'same-origin', headers: writeHeaders,
  })
  if (!response.ok) throw await apiError(response)
}

export async function sendMessage(
  chatId: string,
  content: string,
  onEvent: (event: ChatEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const response = await fetch(`/api/chats/${chatId}/messages`, {
    method: 'POST', credentials: 'same-origin', signal, headers: writeHeaders,
    body: JSON.stringify({ content }),
  })
  if (!response.ok || !response.body) throw await apiError(response)
  const parser = createSseParser(onEvent)
  const reader = response.body.getReader()
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    parser.push(value)
  }
  parser.finish()
}
