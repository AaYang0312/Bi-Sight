import { describe, expect, it } from 'vitest'

import { createSseParser } from './api'
import type { ChatEvent } from './types'

describe('createSseParser', () => {
  it('parses UTF-8 split inside a multibyte character', () => {
    const events: ChatEvent[] = []
    const parser = createSseParser((event) => events.push(event))
    const bytes = new TextEncoder().encode(
      'event: error\ndata: {"code":"demo","message":"你好"}\n\n',
    )
    const split = bytes.findIndex((byte) => byte > 0x7f) + 1
    parser.push(bytes.slice(0, split))
    parser.push(bytes.slice(split))
    parser.finish()
    expect(events).toEqual([
      { event: 'error', data: { code: 'demo', message: '你好' } },
    ])
  })

  it('rejects unknown event names and unfinished frames', () => {
    expect(() => {
      const parser = createSseParser(() => {})
      parser.push(new TextEncoder().encode('event: token\ndata: {}\n\n'))
    }).toThrow('未知SSE事件')
    expect(() => {
      const parser = createSseParser(() => {})
      parser.push(new TextEncoder().encode('event: done\ndata: {"status":"complete"}'))
      parser.finish()
    }).toThrow('不完整')
  })
})
