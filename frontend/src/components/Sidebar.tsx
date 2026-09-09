import { useState } from 'react'

import type { ChatSummary } from '../types'

export function Sidebar({ chats, selectedId, busy, open, onClose, onCreate, onSelect, onRename, onDelete }: {
  chats: ChatSummary[]
  selectedId: string | null
  busy: boolean
  open: boolean
  onClose: () => void
  onCreate: () => void
  onSelect: (id: string) => void
  onRename: (id: string, title: string) => void
  onDelete: (id: string) => void
}) {
  const [editing, setEditing] = useState<string | null>(null)
  const [title, setTitle] = useState('')

  return (
    <aside className={`sidebar ${open ? 'open' : ''}`} aria-label="会话列表">
      <div className="sidebar-head">
        <span className="product-name">经营助手</span>
        <button className="mobile-close" onClick={onClose} aria-label="关闭会话列表">×</button>
        <button onClick={onCreate} disabled={busy}>新建对话</button>
      </div>
      <nav>
        {chats.map((chat) => (
          <div className={`chat-row ${chat.id === selectedId ? 'selected' : ''}`} key={chat.id}>
            {editing === chat.id ? (
              <form onSubmit={(event) => {
                event.preventDefault()
                if (title.trim()) onRename(chat.id, title.trim())
                setEditing(null)
              }}>
                <input autoFocus value={title} maxLength={80} onChange={(event) => setTitle(event.target.value)} />
              </form>
            ) : (
              <button className="chat-title" onClick={() => onSelect(chat.id)}>{chat.title}</button>
            )}
            <div className="chat-actions">
              <button aria-label={`重命名 ${chat.title}`} disabled={busy} onClick={() => { setEditing(chat.id); setTitle(chat.title) }}>⋯</button>
              <button aria-label={`删除 ${chat.title}`} disabled={busy} onClick={() => onDelete(chat.id)}>×</button>
            </div>
          </div>
        ))}
      </nav>
    </aside>
  )
}
