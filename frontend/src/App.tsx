import { useEffect, useRef, useState } from 'react'

import { ApiError, createChat, deleteChat, listChats, loadMessages, renameChat, sendMessage } from './api'
import { ChatView } from './components/ChatView'
import { Sidebar } from './components/Sidebar'
import type { Artifact, ChatMessage, ChatSummary } from './types'

const stageText: Record<string, string> = {
  thinking: '正在理解问题…',
  querying: '正在查询数据…',
  answering: '正在组织回答…',
}

export default function App() {
  const [chats, setChats] = useState<ChatSummary[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [status, setStatus] = useState<string | null>(null)
  const [artifacts, setArtifacts] = useState<Artifact[]>([])
  const [error, setError] = useState<string | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const controller = useRef<AbortController | null>(null)
  const selectedRef = useRef<string | null>(null)

  useEffect(() => { selectedRef.current = selectedId }, [selectedId])

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSidebarOpen(false)
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [])

  async function refreshChats() {
    const next = await listChats()
    setChats(next)
    return next
  }

  async function selectChat(chatId: string) {
    controller.current?.abort()
    controller.current = null
    setSelectedId(chatId)
    setMessages([])
    setStatus(null)
    setArtifacts([])
    setError(null)
    try {
      setMessages(await loadMessages(chatId))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '加载会话失败')
    }
  }

  async function makeChat() {
    try {
      const chat = await createChat()
      setChats((current) => [chat, ...current])
      await selectChat(chat.id)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '新建会话失败')
    }
  }

  useEffect(() => {
    void (async () => {
      try {
        const next = await refreshChats()
        if (next[0]) await selectChat(next[0].id)
        else await makeChat()
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : '初始化失败')
      }
    })()
    return () => controller.current?.abort()
  }, [])

  async function rename(chatId: string, title: string) {
    try {
      const updated = await renameChat(chatId, title)
      setChats((current) => current.map((chat) => chat.id === chatId ? updated : chat))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '改名失败')
    }
  }

  async function remove(chatId: string) {
    if (!window.confirm('删除这条对话及其消息？')) return
    try {
      await deleteChat(chatId)
      const next = await refreshChats()
      if (chatId === selectedRef.current) {
        if (next[0]) await selectChat(next[0].id)
        else await makeChat()
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '删除失败')
    }
  }

  async function send(content: string) {
    const chatId = selectedRef.current
    if (!chatId || controller.current) return
    const local: ChatMessage = {
      id: `local-${Date.now()}`, role: 'user', content, artifacts: [], status: 'complete',
      created_at: new Date().toISOString(),
    }
    const active = new AbortController()
    controller.current = active
    setMessages((current) => [...current, local])
    setStatus('正在理解问题…')
    setArtifacts([])
    setError(null)
    try {
      await sendMessage(chatId, content, (event) => {
        if (selectedRef.current !== chatId) return
        if (event.event === 'status') setStatus(stageText[event.data.stage])
        if (event.event === 'artifact') setArtifacts((current) => [...current, event.data])
        if (event.event === 'message') setMessages((current) => [...current, event.data])
        if (event.event === 'error') setError(event.data.message)
      }, active.signal)
      if (selectedRef.current === chatId) setMessages(await loadMessages(chatId))
      await refreshChats()
    } catch (caught) {
      if (!(caught instanceof DOMException && caught.name === 'AbortError')) {
        const message = caught instanceof ApiError || caught instanceof Error ? caught.message : '发送失败'
        setError(message)
      }
    } finally {
      if (controller.current === active) controller.current = null
      if (selectedRef.current === chatId) {
        setStatus(null)
        setArtifacts([])
      }
    }
  }

  return (
    <div className="workspace">
      <button className="menu-toggle" onClick={() => setSidebarOpen(true)} aria-label="打开会话列表">☰</button>
      <Sidebar chats={chats} selectedId={selectedId} busy={controller.current !== null}
        open={sidebarOpen} onClose={() => setSidebarOpen(false)} onCreate={makeChat}
        onSelect={(chatId) => { setSidebarOpen(false); void selectChat(chatId) }}
        onRename={rename} onDelete={remove} />
      <ChatView messages={messages} status={status} artifacts={artifacts} error={error}
        disabled={controller.current !== null || !selectedId} onSend={send} />
    </div>
  )
}
