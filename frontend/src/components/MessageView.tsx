import type { ChatMessage } from '../types'
import { ArtifactView } from './ArtifactView'

export function MessageView({ message }: { message: ChatMessage }) {
  return (
    <article className={`message ${message.role}`}>
      <div className="message-label">{message.role === 'user' ? '你' : '经营助手'}</div>
      <div className="message-content">{message.content}</div>
      {message.artifacts.map((artifact, index) => <ArtifactView artifact={artifact} key={index} />)}
    </article>
  )
}
