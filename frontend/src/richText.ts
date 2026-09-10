/**
 * 模型正文的轻量 Markdown 解析：只做纯函数与数据结构，不生成 HTML 字符串。
 *
 * 模型经常用字符拼出表格（`| 日期 | 金额 |`、框线表、对齐列），也常用
 * `**加粗**`、`- 列表`、`` `代码` `` 组织结论。渲染层只吃这里产出的块结构，
 * 再用 React 文本节点输出，因此不需要 innerHTML，也不会执行模型给出的标签。
 */

export type Inline =
  | { kind: 'text'; text: string }
  | { kind: 'strong'; text: string }
  | { kind: 'em'; text: string }
  | { kind: 'code'; text: string }
  | { kind: 'strike'; text: string }
  | { kind: 'link'; text: string; href: string }

export type Align = 'left' | 'center' | 'right' | null

export type Block =
  | { type: 'paragraph'; inline: Inline[] }
  | { type: 'heading'; level: number; inline: Inline[] }
  | { type: 'list'; ordered: boolean; start: number; items: Inline[][] }
  | { type: 'table'; header: Inline[][]; rows: Inline[][][]; aligns: Align[] }
  | { type: 'code'; text: string; language: string | null }
  | { type: 'rule' }

export const ASCII_TABLE_LANGUAGE = 'ascii-table'

const FENCE = /^\s*(`{3,}|~{3,})\s*([^`\s]*)\s*$/
const HEADING = /^(#{1,6})\s+(.*)$/
const THEMATIC = /^\s*(?:([-*_])\s*)(?:\1\s*){2,}$/
const LIST_ITEM = /^\s*(?:([-*+])|(\d{1,3})([.)、）]))\s+(.*)$/
// 中文标号常不带空格（「1）2026-09-04」），但 ASCII 的「1.5 万元」不能被误读成列表。
const TIGHT_LIST_ITEM = /^\s*(\d{1,3}[）])(.*)$/
const CODE_TICK = /`([^`\n]+)`/
const STRONG = /\*\*([^*\n]+)\*\*|__([^_\n]+)__/
const EMPHASIS = /\*([^*\n]+)\*/
const STRIKETHROUGH = /~~([^~\n]+)~~/
const LINK = /\[([^\]\n]+)\]\(([^)\s]+)\)/
const BOX_DRAWING = /[┌┐└┘├┤┬┴┼─│═║╔╗╚╝╠╣╦╩╬╭╮╰╯]/
const COLUMNAR = /[^\s|].*[ \t]{2,}.*[^\s|]/

/** 行内标记按同一行内匹配，避免 `shop_1`、`previous_period` 被当成斜体。 */
const INLINE_PATTERN = new RegExp(
  [CODE_TICK.source, STRONG.source, EMPHASIS.source, STRIKETHROUGH.source, LINK.source].join('|'),
  'g',
)

const SAFE_HREF = /^(?:https?:\/\/|mailto:)\S+$/i

export function parseInline(text: string): Inline[] {
  const inline: Inline[] = []
  let cursor = 0
  for (const match of text.matchAll(INLINE_PATTERN)) {
    const start = match.index ?? 0
    if (start > cursor) inline.push({ kind: 'text', text: text.slice(cursor, start) })
    const [, code, strongBold, strongUnder, emphasis, strike, linkText, linkHref] = match
    if (code !== undefined) inline.push({ kind: 'code', text: code })
    else if (strongBold !== undefined || strongUnder !== undefined)
      inline.push({ kind: 'strong', text: strongBold ?? strongUnder })
    else if (emphasis !== undefined) inline.push({ kind: 'em', text: emphasis })
    else if (strike !== undefined) inline.push({ kind: 'strike', text: strike })
    else if (linkText !== undefined && linkHref !== undefined)
      inline.push(
        SAFE_HREF.test(linkHref)
          ? { kind: 'link', text: linkText, href: linkHref }
          : { kind: 'text', text: `${linkText}(${linkHref})` },
      )
    cursor = start + match[0].length
  }
  if (cursor < text.length) inline.push({ kind: 'text', text: text.slice(cursor) })
  return inline.length ? inline : [{ kind: 'text', text: '' }]
}

function splitCells(line: string): string[] {
  let text = line.trim()
  if (text.startsWith('|')) text = text.slice(1)
  if (text.endsWith('|') && !text.endsWith('\\|')) text = text.slice(0, -1)
  return text.split(/(?<!\\)\|/).map((cell) => cell.replace(/\\\|/g, '|').trim())
}

function isPipeRow(line: string): boolean {
  return line.includes('|') && splitCells(line).length >= 2
}

function parseAligns(line: string): Align[] | null {
  const cells = splitCells(line)
  if (cells.length < 2) return null
  const aligns: Align[] = []
  for (const cell of cells) {
    const match = /^(:?-{1,}:?)$/.exec(cell)
    if (!match) return null
    const spec = match[1]
    const left = spec.startsWith(':')
    const right = spec.endsWith(':')
    aligns.push(left && right ? 'center' : right ? 'right' : left ? 'left' : null)
  }
  return aligns
}

function listMarker(line: string): { marker: string; text: string } | null {
  const spaced = LIST_ITEM.exec(line)
  if (spaced) {
    const marker = spaced[1] ?? `${spaced[2]}${spaced[3]}`
    return { marker, text: spaced[4] }
  }
  const tight = TIGHT_LIST_ITEM.exec(line)
  return tight ? { marker: tight[1], text: tight[2] } : null
}

function isListLine(line: string): boolean {
  return listMarker(line) !== null
}

/** 没有竖线的字符表格：框线表，或每行都用两空格以上分列的对齐表。 */
function isAsciiTableRun(lines: string[], start: number): number {
  if (lines[start].trim() === '' || start + 1 >= lines.length) return 0
  const boxed = BOX_DRAWING.test(lines[start])
  const columnar = !boxed && COLUMNAR.test(lines[start]) && !isListLine(lines[start])
  if (!boxed && !columnar) return 0
  let end = start
  while (end < lines.length && lines[end].trim() !== '') {
    const line = lines[end]
    const matches = boxed ? BOX_DRAWING.test(line) : COLUMNAR.test(line) && !isListLine(line)
    if (!matches) break
    end += 1
  }
  return end - start >= 2 ? end - start : 0
}

function startsBlock(lines: string[], index: number): boolean {
  const line = lines[index] ?? ''
  if (!line.trim()) return true
  if (FENCE.test(line) || HEADING.test(line) || THEMATIC.test(line) || isListLine(line)) return true
  if (index + 1 < lines.length && isPipeRow(line) && parseAligns(lines[index + 1])) return true
  return isAsciiTableRun(lines, index) > 0
}

export function parseRichText(source: string): Block[] {
  const lines = source.replace(/\r\n?/g, '\n').split('\n')
  const blocks: Block[] = []
  let index = 0

  while (index < lines.length) {
    const line = lines[index]

    if (!line.trim()) {
      index += 1
      continue
    }

    const fence = FENCE.exec(line)
    if (fence) {
      const marker = fence[1]
      const language = fence[2] || null
      const body: string[] = []
      index += 1
      while (index < lines.length && !lines[index].trim().startsWith(marker[0].repeat(3))) {
        body.push(lines[index])
        index += 1
      }
      if (index < lines.length) index += 1
      blocks.push({ type: 'code', text: body.join('\n'), language })
      continue
    }

    if (THEMATIC.test(line)) {
      index += 1
      blocks.push({ type: 'rule' })
      continue
    }

    const heading = HEADING.exec(line)
    if (heading) {
      index += 1
      blocks.push({ type: 'heading', level: heading[1].length, inline: parseInline(heading[2].trim()) })
      continue
    }

    if (index + 1 < lines.length && isPipeRow(line)) {
      const declared = parseAligns(lines[index + 1])
      if (declared) {
        const columns = splitCells(line).length
        // 分隔行少于表头时不能留下空洞，按列数补齐。
        const aligns = Array.from({ length: columns }, (_, at) => declared[at] ?? null)
        const pad = (cells: string[]) =>
          Array.from({ length: columns }, (_, at) => parseInline(cells[at] ?? ''))
        const header = pad(splitCells(line))
        const rows: Inline[][][] = []
        index += 2
        while (index < lines.length && isPipeRow(lines[index]) && !parseAligns(lines[index])) {
          rows.push(pad(splitCells(lines[index]).slice(0, columns)))
          index += 1
        }
        blocks.push({ type: 'table', header, rows, aligns })
        continue
      }
    }

    const first = listMarker(line)
    if (first) {
      const ordered = /^\d/.test(first.marker)
      const items: Inline[][] = []
      let start = 1
      while (index < lines.length) {
        const item = listMarker(lines[index])
        if (!item || /^\d/.test(item.marker) !== ordered) break
        if (ordered && items.length === 0) start = parseInt(item.marker, 10) || 1
        items.push(parseInline(item.text.trim()))
        index += 1
      }
      blocks.push({ type: 'list', ordered, start, items })
      continue
    }

    const asciiSpan = isAsciiTableRun(lines, index)
    if (asciiSpan) {
      const text = lines.slice(index, index + asciiSpan).join('\n')
      index += asciiSpan
      blocks.push({ type: 'code', text, language: ASCII_TABLE_LANGUAGE })
      continue
    }

    const paragraph: string[] = []
    while (index < lines.length && !startsBlock(lines, index)) {
      paragraph.push(lines[index])
      index += 1
    }
    blocks.push({ type: 'paragraph', inline: parseInline(paragraph.join('\n')) })
  }

  return blocks
}
