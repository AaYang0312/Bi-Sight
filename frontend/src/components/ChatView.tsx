import type { Artifact, ChatMessage } from '../types'
import { ArtifactView } from './ArtifactView'
import { Composer } from './Composer'
import { MessageView } from './MessageView'

const examples = [
  '最近7天支付金额如何？',
  '9月1日至7日退款发生多少？',
  '假设下月销售10万元，推广费率12%',
]

export function ChatView({
  messages, status, artifacts, error, disabled, onSend,
}: {
  messages: ChatMessage[]
  status: string | null
  artifacts: Artifact[]
  error: string | null
  disabled: boolean
  onSend: (content: string) => void
}) {
  return (
    <main className="chat-main">
      <div className="message-list" aria-live="polite">
        {messages.length === 0 ? (
          <div className="empty-state">
            <h1>经营数据，直接问。</h1>
            <p>可查询已覆盖期间的经营情况，或基于明确假设测算推广预算。</p>
            <div className="example-list">
              {examples.map((example) => <button key={example} onClick={() => onSend(example)}>{example}</button>)}
            </div>
          </div>
        ) : messages.map((message) => <MessageView key={message.id} message={message} />)}
        {status && <p className="stream-status" role="status">{status}</p>}
        {artifacts.map((artifact, index) => <ArtifactView artifact={artifact} key={index} />)}
        {error && <p className="stream-error" role="alert">{error}</p>}
      </div>
      <Composer disabled={disabled} onSend={onSend} />
    </main>
  )
}
