export type ChatSummary = {
  id: string
  title: string
  created_at: string
  updated_at: string
}

export type Artifact = Record<string, unknown>

export type ChatMessage = {
  id: string
  role: 'user' | 'assistant'
  content: string
  artifacts: Artifact[]
  status: 'complete' | 'error'
  created_at: string
}

export type ChatEvent =
  | { event: 'status'; data: { stage: 'thinking' | 'querying' | 'answering' } }
  | { event: 'artifact'; data: Artifact }
  | { event: 'message'; data: ChatMessage }
  | { event: 'error'; data: { code: string; message: string } }
  | { event: 'done'; data: { status: 'complete' | 'error' } }
