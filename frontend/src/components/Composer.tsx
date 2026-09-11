import { useRef, useState } from 'react'

import { ArrowUpIcon } from './icons'

const MAX = 4000

/**
 * Enter 是否应当发送。中文 IME 合成中的 Enter 只是上屏确认（M-15），
 * 不能当成发送；keyCode 229 是部分浏览器在合成开始前的占位值，同样只忽略。
 */
export function isSubmitKeydown(event: {
  key: string
  shiftKey: boolean
  keyCode?: number
  nativeEvent: { isComposing?: boolean }
}) {
  if (event.nativeEvent.isComposing || event.keyCode === 229) return false
  return event.key === 'Enter' && !event.shiftKey
}

export function Composer({ disabled, onSend }: {
  disabled: boolean
  onSend: (content: string) => void
}) {
  const [content, setContent] = useState('')
  const textarea = useRef<HTMLTextAreaElement>(null)
  const length = content.trim().length
  const canSend = length > 0 && length <= MAX && !disabled

  function update(value: string) {
    setContent(value)
    const element = textarea.current
    if (element) {
      element.style.height = 'auto'
      element.style.height = `${Math.min(element.scrollHeight, 144)}px`
    }
  }

  function submit() {
    if (!canSend) return
    onSend(content.trim())
    update('')
  }

  return (
    <form className="composer" onSubmit={(event) => { event.preventDefault(); submit() }}>
      <label className="sr-only" htmlFor="chat-input">输入问题</label>
      <div className="composer-row">
        <textarea
          id="chat-input"
          ref={textarea}
          value={content}
          rows={1}
          disabled={disabled}
          placeholder="问问经营情况、退款或推广预算…"
          onChange={(event) => update(event.target.value)}
          onKeyDown={(event) => {
            if (!isSubmitKeydown(event)) return
            event.preventDefault()
            submit()
          }}
        />
        <button className="send" type="submit" disabled={!canSend} aria-label="发送问题"><ArrowUpIcon /></button>
      </div>
      <div className="composer-foot">
        <span className="composer-hint"><kbd>Enter</kbd>发送<kbd>⇧</kbd><kbd>Enter</kbd>换行</span>
        {length > 0 && <span className={`composer-count ${length > MAX ? 'over' : ''}`}>{length} / {MAX}</span>}
      </div>
    </form>
  )
}
