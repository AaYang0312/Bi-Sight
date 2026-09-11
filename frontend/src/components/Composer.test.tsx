import { describe, expect, it } from 'vitest'

import { isSubmitKeydown } from './Composer'

describe('isSubmitKeydown', () => {
  it('sends on a bare Enter', () => {
    expect(isSubmitKeydown({
      key: 'Enter', shiftKey: false, nativeEvent: { isComposing: false },
    })).toBe(true)
  })

  it('keeps Shift+Enter as a newline', () => {
    expect(isSubmitKeydown({
      key: 'Enter', shiftKey: true, nativeEvent: { isComposing: false },
    })).toBe(false)
  })

  it('ignores Enter that only commits an IME composition', () => {
    // “推广费率12%” 上屏确认按下的就是 Enter，不能把半截问题发出去。
    expect(isSubmitKeydown({
      key: 'Enter', shiftKey: false, nativeEvent: { isComposing: true },
    })).toBe(false)
  })

  it('ignores the keyCode 229 composition placeholder', () => {
    expect(isSubmitKeydown({
      key: 'Enter', shiftKey: false, keyCode: 229, nativeEvent: {},
    })).toBe(false)
  })

  it('ignores ordinary characters', () => {
    expect(isSubmitKeydown({
      key: 'a', shiftKey: false, nativeEvent: { isComposing: false },
    })).toBe(false)
  })
})
