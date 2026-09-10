import { describe, expect, it } from 'vitest'

import { ASCII_TABLE_LANGUAGE, parseInline, parseRichText } from './richText'

function texts(inline: { kind: string; text: string }[]) {
  return inline.map((part) => part.text).join('')
}

describe('parseRichText', () => {
  it('keeps plain prose as one paragraph without markdown noise', () => {
    const [block] = parseRichText('9月1日–7日退款发生额：**¥5,579.01**')
    expect(block.type).toBe('paragraph')
    if (block.type === 'paragraph') {
      expect(block.inline.filter((part) => part.kind === 'strong').length).toBe(1)
      expect(texts(block.inline)).toContain('¥5,579.01')
    }
  })

  it('renders the real model pipe table into header and body cells', () => {
    const source = [
      '**可用的完整窗口**',
      '',
      '| 日期 | 支付金额（元） |',
      '|---|---|',
      '| 09-02 | 4,723.93 |',
      '| 09-03 | 7,123.75 |',
      '',
      '口径说明：按支付时间归属。',
    ].join('\n')
    const blocks = parseRichText(source)
    expect(blocks.map((block) => block.type)).toEqual(['paragraph', 'table', 'paragraph'])
    const table = blocks[1]
    if (table.type !== 'table') throw new Error('expected table')
    expect(table.header.map(texts)).toEqual(['日期', '支付金额（元）'])
    expect(table.rows.map((row) => row.map(texts))).toEqual([
      ['09-02', '4,723.93'],
      ['09-03', '7,123.75'],
    ])
    expect(table.aligns).toEqual([null, null])
  })

  it('accepts tables without outer pipes and honours alignment markers', () => {
    const [table] = parseRichText([
      '指标 | 数值',
      '| :--- | ---: |',
      '| 支付金额 | ¥22,693.29 |',
    ].join('\n'))
    if (table.type !== 'table') throw new Error('expected table')
    expect(table.header.map(texts)).toEqual(['指标', '数值'])
    expect(table.aligns).toEqual(['left', 'right'])
  })

  it('pads and truncates ragged rows so every row matches the header', () => {
    const [table] = parseRichText([
      '| a | b | c |',
      '|---|---|---|',
      '| 1 | 2 |',
      '| 1 | 2 | 3 | 4 |',
    ].join('\n'))
    if (table.type !== 'table') throw new Error('expected table')
    expect(table.rows.map((row) => row.map(texts))).toEqual([
      ['1', '2', ''],
      ['1', '2', '3'],
    ])
  })

  it('keeps escaped pipes inside a cell', () => {
    const [table] = parseRichText('| a | b |\n|---|---|\n| x \\| y | z |')
    if (table.type !== 'table') throw new Error('expected table')
    expect(table.rows[0].map(texts)).toEqual(['x | y', 'z'])
  })

  it('pads a short separator row so every column still gets an alignment', () => {
    const [table] = parseRichText('| a | b | c |\n|---|---|\n| 1 | 2 | 3 |')
    if (table.type !== 'table') throw new Error('expected table')
    expect(table.aligns).toEqual([null, null, null])
  })

  it('does not read mixed punctuation as a horizontal rule', () => {
    expect(parseRichText('-*-').map((block) => block.type)).toEqual(['paragraph'])
    expect(parseRichText('***').map((block) => block.type)).toEqual(['rule'])
  })

  it('parses bullet lists, numbered lists and their starting number', () => {
    const blocks = parseRichText('- 缺口：09-09\n- 口径：支付时间\n\n1）2026-09-04 ~ 09-08\n2）往前补足 7 天')
    const [bullets, numbers] = blocks
    if (bullets.type !== 'list' || numbers.type !== 'list') throw new Error('expected lists')
    expect(bullets.ordered).toBe(false)
    expect(bullets.items.map(texts)).toEqual(['缺口：09-09', '口径：支付时间'])
    expect(numbers.ordered).toBe(true)
    expect(numbers.start).toBe(1)
    expect(numbers.items.length).toBe(2)
  })

  it('does not treat underscores in field names as emphasis', () => {
    const [block] = parseRichText('店铺 shop_1 的 previous_period 环比')
    if (block.type !== 'paragraph') throw new Error('expected paragraph')
    expect(block.inline.every((part) => part.kind !== 'em')).toBe(true)
    expect(texts(block.inline)).toBe('店铺 shop_1 的 previous_period 环比')
  })

  it('parses headings, rules, inline code and fenced code', () => {
    const blocks = parseRichText('### 说明\n\n需要 `resolve_period`。\n\n---\n\n```\nraw\n```')
    expect(blocks.map((block) => block.type)).toEqual(['heading', 'paragraph', 'rule', 'code'])
    if (blocks[0].type === 'heading') expect(blocks[0].level).toBe(3)
    if (blocks[3].type === 'code') expect(blocks[3].text).toBe('raw')
  })

  it('renders box-drawing tables as monospace blocks instead of flowing them', () => {
    const [block] = parseRichText([
      '┌──────┬─────────┐',
      '│ 日期 │ 金额    │',
      '├──────┼─────────┤',
      '│ 09-04│ 4,465.03│',
      '└──────┴─────────┘',
    ].join('\n'))
    expect(block.type).toBe('code')
    if (block.type === 'code') expect(block.language).toBe(ASCII_TABLE_LANGUAGE)
  })

  it('renders space-aligned tables as monospace blocks', () => {
    const [block] = parseRichText('日期      支付金额\n09-04     4,465.03\n09-05     4,743.94')
    expect(block.type).toBe('code')
    if (block.type === 'code') expect(block.text.split('\n').length).toBe(3)
  })

  it('normalises CRLF and survives a trailing incomplete table', () => {
    const blocks = parseRichText('| a | b |\r\n|---|---|\r\n| 1 | 2 |')
    const table = blocks.find((block) => block.type === 'table')
    if (table?.type !== 'table') throw new Error('expected table')
    expect(table.rows.map((row) => row.map(texts))).toEqual([['1', '2']])
  })

  it('refuses non-http links but keeps the visible text', () => {
    const [block] = parseRichText('[点我](javascript:alert(1))')
    if (block.type !== 'paragraph') throw new Error('expected paragraph')
    expect(block.inline.some((part) => part.kind === 'link')).toBe(false)
    expect(texts(block.inline)).toContain('点我')
  })

  it('keeps safe links', () => {
    const [block] = parseRichText('见 [报表](https://example.com/a) 说明')
    if (block.type !== 'paragraph') throw new Error('expected paragraph')
    const link = block.inline.find((part) => part.kind === 'link')
    expect(link).toMatchObject({ href: 'https://example.com/a' })
  })
})

describe('parseInline', () => {
  it('returns a single empty text node for empty input', () => {
    expect(parseInline('')).toEqual([{ kind: 'text', text: '' }])
  })
})
