import { useRef, useState } from 'react'

export function Composer({ disabled, onSend }: {
  disabled: boolean
  onSend: (content: string) => void
}) {
  const [content, setContent] = useState('')
  const textarea = useRef<HTMLTextAreaElement>(null)
  const canSend = content.trim().length > 0 && content.trim().length <= 4000 && !disabled

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
      <textarea
        id="chat-input"
        ref={textarea}
        value={content}
        rows={1}
        disabled={disabled}
        placeholder="问问经营情况、退款或推广预算…"
        onChange={(event) => update(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault()
            submit()
          }
        }}
      />
      <button type="submit" disabled={!canSend} aria-label="发送问题">发送</button>
    </form>
  )
}
