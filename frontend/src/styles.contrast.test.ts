import { describe, expect, it } from 'vitest'

import styles from './styles.css?raw'

// 设计 token 的对比度门禁：小字号标签（10.5–12px）必须达到 WCAG AA 的 4.5:1。
// 值直接解析自 styles.css，避免测试与样式各写一份色值。

function hex(name: string): string {
  const match = new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})\\s*;`).exec(styles)
  if (!match) throw new Error(`styles.css 里找不到 --${name}`)
  return match[1]
}

function rgb(value: string): [number, number, number] {
  return [1, 3, 5].map((offset) => Number.parseInt(value.slice(offset, offset + 2), 16)) as
    [number, number, number]
}

function luminance([red, green, blue]: [number, number, number]): number {
  const channel = (value: number) => {
    const ratio = value / 255
    return ratio <= 0.04045 ? ratio / 12.92 : ((ratio + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)
}

function contrast(foreground: string, background: string): number {
  const first = luminance(rgb(foreground))
  const second = luminance(rgb(background))
  return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05)
}

// 卡片表面是半透明白渐变叠在 --bg-app 上；顶端最亮，也最容易吃掉文字对比度。
function overlay(foregroundAlpha: number, background: string): string {
  const base = rgb(background)
  return base
    .map((value) => Math.round(255 * foregroundAlpha + value * (1 - foregroundAlpha))
      .toString(16).padStart(2, '0'))
    .join('')
    .replace(/^/, '#')
}

describe('深色主题文字对比度', () => {
  const bgApp = hex('bg-app')
  const cardTop = overlay(0.062, bgApp)

  it('保持 --bg-app 为近黑底', () => {
    expect(bgApp).toBe('#0e0c14')
  })

  it('--text-3 在最亮实际底色上仍满足 4.5:1', () => {
    const text3 = hex('text-3')
    expect(contrast(text3, bgApp)).toBeGreaterThanOrEqual(4.5)
    expect(contrast(text3, cardTop)).toBeGreaterThanOrEqual(4.5)
  })

  it('--text-2 满足正文对比度且仍亮于 --text-3', () => {
    const text2 = hex('text-2')
    const text3 = hex('text-3')
    expect(contrast(text2, bgApp)).toBeGreaterThanOrEqual(4.5)
    expect(contrast(text2, cardTop)).toBeGreaterThanOrEqual(4.5)
    expect(luminance(rgb(text2))).toBeGreaterThan(luminance(rgb(text3)))
  })
})
