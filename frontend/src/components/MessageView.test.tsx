import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'

import { RichContent } from './MessageView'

// 直接取自真实模型回复，避免只测理想化的 Markdown。
const REAL_ANSWER = [
  '**结论：最近 7 天（09-04~09-10）暂时拿不到完整汇总，只能给出已完整覆盖的 5 天数据。**',
  '',
  '查询情况（店铺 shop_1）：',
  '- 请求区间 2026-09-04 ~ 09-11，覆盖状态 partial，缺口 `2026-09-09 ~ 09-10`',
  '- 数据截止 **2026-09-09 11:39（UTC）**',
  '',
  '| 日期 | 支付金额（元） |',
  '|:---|---:|',
  '| 09-04 | 4,465.03 |',
  '| 09-05 | 4,743.94 |',
  '',
  '1）2026-09-04 ~ 09-08（5 天，覆盖完整）：支付金额 **22,693.29 元**',
  '2）往前补足 7 个自然日',
].join('\n')

describe('RichContent', () => {
  const html = renderToStaticMarkup(<RichContent text={REAL_ANSWER} />)

  it('turns character tables into a real table', () => {
    expect(html).toContain('<table class="rich-table">')
    expect(html).toContain('<th style="text-align:left">日期</th>')
    expect(html).toContain('<td style="text-align:right">4,465.03</td>')
    expect(html).not.toContain('|---')
    expect(html).not.toContain('| 日期 |')
  })

  it('renders emphasis, code, bullets and Chinese numbered lists', () => {
    expect(html).toContain('<strong>结论：')
    expect(html).toContain('<code>2026-09-09 ~ 09-10</code>')
    expect(html).toContain('<ul><li>')
    expect(html).toContain('<ol start="1"><li>2026-09-04 ~ 09-08')
  })

  it('keeps the shop alias untouched', () => {
    expect(html).toContain('shop_1')
    expect(html).not.toContain('<em>')
  })

  it('renders plain text messages unchanged', () => {
    expect(renderToStaticMarkup(<RichContent text='9月1日–7日退款发生额：5,579.01 元' />))
      .toBe('<div class="rich-content"><p>9月1日–7日退款发生额：5,579.01 元</p></div>')
  })

  it('renders box-drawing tables in a monospace block', () => {
    const boxed = renderToStaticMarkup(
      <RichContent text={'┌──────┬───────┐\n│ 日期 │ 金额  │\n└──────┴───────┘'} />,
    )
    expect(boxed).toContain('<pre class="rich-code ascii-table"><code>')
    expect(boxed).toContain('┌──────┬───────┐')
  })

  it('does not emit raw markup from model text', () => {
    const hostile = renderToStaticMarkup(
      <RichContent text={'<img src=x onerror=alert(1)>\n\n<script>bad()</script>'} />,
    )
    expect(hostile).not.toContain('<img')
    expect(hostile).not.toContain('<script')
    expect(hostile).toContain('&lt;script&gt;')
  })
})
