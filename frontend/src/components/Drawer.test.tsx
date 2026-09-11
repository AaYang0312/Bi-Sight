import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'

import { ChatView } from './ChatView'
import { Sidebar } from './Sidebar'

// 窄屏抽屉的可访问性状态：收起必须真的离开 Tab 序列与无障碍树，
// 展开时轮到背景内容让位——宽屏常驻侧栏两者都不能碰。

function drawer(narrow: boolean, open: boolean) {
  return renderToStaticMarkup(
    <Sidebar chats={[]} selectedId={null} busy={false} open={open} narrow={narrow}
      onClose={() => undefined} onCreate={() => undefined} onSelect={() => undefined}
      onRename={() => undefined} onDelete={() => undefined} />,
  )
}

function stage(narrow: boolean, open: boolean) {
  return renderToStaticMarkup(
    <ChatView messages={[]} status={null} artifacts={[]} error={null} disabled={false}
      title="九月复盘" updatedAt={null} chatCount={1} narrow={narrow} drawerOpen={open}
      onOpenSidebar={() => undefined} onSend={() => undefined} />,
  )
}

function tag(markup: string, selector: RegExp) {
  const found = selector.exec(markup)
  if (!found) throw new Error(`渲染结果里找不到 ${selector}`)
  return found[0]
}

describe('窄屏抽屉', () => {
  it('收起时整个面板 inert + aria-hidden', () => {
    const aside = tag(drawer(true, false), /<aside[^>]*>/)
    expect(aside).toContain('aria-hidden="true"')
    expect(aside).toContain('inert=""')
    expect(aside).toContain('id="chat-nav-drawer"')
  })

  it('展开时面板恢复可达', () => {
    const aside = tag(drawer(true, true), /<aside[^>]*>/)
    expect(aside).not.toContain('aria-hidden')
    expect(aside).not.toContain('inert')
  })

  it('宽屏常驻侧栏不被隐藏，也不带 inert', () => {
    for (const aside of [tag(drawer(false, false), /<aside[^>]*>/),
                         tag(drawer(false, true), /<aside[^>]*>/)]) {
      expect(aside).not.toContain('aria-hidden')
      expect(aside).not.toContain('inert')
    }
  })

  it('抽屉展开时背景内容让位，收起时照常可达', () => {
    expect(tag(stage(true, true), /<main[^>]*>/)).toContain('inert=""')
    expect(tag(stage(true, true), /<main[^>]*>/)).toContain('aria-hidden="true"')
    const idle = tag(stage(true, false), /<main[^>]*>/)
    expect(idle).not.toContain('inert')
    expect(idle).not.toContain('aria-hidden')
    expect(tag(stage(false, false), /<main[^>]*>/)).not.toContain('inert')
  })

  it('menu-toggle 暴露 aria-expanded 并指向抽屉', () => {
    const closed = tag(stage(true, false), /<button class="menu-toggle"[^>]*>/)
    expect(closed).toContain('aria-expanded="false"')
    expect(closed).toContain('aria-controls="chat-nav-drawer"')
    expect(tag(stage(true, true), /<button class="menu-toggle"[^>]*>/))
      .toContain('aria-expanded="true"')
  })
})
