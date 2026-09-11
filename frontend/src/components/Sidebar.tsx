import { useState, type RefObject } from 'react'

import type { ChatSummary } from '../types'
import { BrandMark, ChatIcon, CloseIcon, EditIcon, PlusIcon } from './icons'

export function Sidebar({ chats, selectedId, busy, open, narrow, onClose, onCreate, onSelect, onRename, onDelete, drawerRef }: {
  chats: ChatSummary[]
  selectedId: string | null
  busy: boolean
  open: boolean
  narrow: boolean
  onClose: () => void
  onCreate: () => void
  onSelect: (id: string) => void
  onRename: (id: string, title: string) => void
  onDelete: (id: string) => void
  drawerRef?: RefObject<HTMLElement | null>
}) {
  const [editing, setEditing] = useState<string | null>(null)
  const [title, setTitle] = useState('')
  // 抽屉态下收起的面板对辅助技术也不存在；宽屏常驻侧栏不能被打上 aria-hidden。
  const collapsed = narrow && !open

  return (
    <>
      {/* 抽屉态的遮罩：关闭动作另有 × 按钮与 Esc，这里只做视觉分层与点击落点。 */}
      <div className={`nav-scrim ${open ? 'show' : ''}`} aria-hidden="true" onClick={onClose} />
      <aside ref={drawerRef} id="chat-nav-drawer" className={`sidebar ${open ? 'open' : ''}`}
        aria-label="会话列表" aria-hidden={collapsed || undefined} inert={collapsed}>
        <div className="sidebar-head">
          <span className="brand-mark" aria-hidden="true"><BrandMark /></span>
          <span className="product-name">经营助手</span>
          <button className="mobile-close icon-btn" onClick={onClose} aria-label="关闭会话列表"><CloseIcon size={16} /></button>
        </div>
        <button className="new-chat" onClick={onCreate} disabled={busy}>
          <PlusIcon /> 新建对话
        </button>
        <nav className="chat-nav" aria-label="历史会话">
          <p className="nav-label">会话</p>
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
                <button className="chat-title" onClick={() => onSelect(chat.id)}>
                  <span className="chat-icon" aria-hidden="true"><ChatIcon size={15} /></span>
                  <span className="chat-name">{chat.title}</span>
                </button>
              )}
              <div className="chat-actions">
                <button className="icon-btn" aria-label={`重命名 ${chat.title}`} disabled={busy} onClick={() => { setEditing(chat.id); setTitle(chat.title) }}><EditIcon /></button>
                <button className="icon-btn" aria-label={`删除 ${chat.title}`} disabled={busy} onClick={() => onDelete(chat.id)}><CloseIcon /></button>
              </div>
            </div>
          ))}
        </nav>
      </aside>
    </>
  )
}
