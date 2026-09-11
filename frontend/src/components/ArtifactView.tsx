import type { Artifact, DisplayEntity } from '../types'
import { ClockIcon, GaugeIcon, TableIcon } from './icons'

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/* 字号只随字符长度收缩，数值本身保持后端原样。 */
function lengthClass(shown: string) {
  if (shown.length > 22) return 'num-xs'
  if (shown.length > 15) return 'num-sm'
  if (shown.length > 10) return 'num-md'
  return ''
}

function text(value: unknown) {
  return value === null || value === undefined ? '不可计算' : String(value)
}

/** 非数值列：这些列进不了单行指标卡，只能当表头维度读。 */
const DIMENSION_COLUMNS = new Set([
  'day', 'shop_ref', 'product_ref', 'sku_ref', 'currency', 'basis', 'line_kind', 'mode',
  'notice',
])

export function artifactEntities(artifact: Artifact): Map<string, DisplayEntity> {
  const byRef = new Map<string, DisplayEntity>()
  const entities = Array.isArray(artifact.entities) ? artifact.entities : []
  for (const item of entities) {
    if (!isRecord(item) || typeof item.ref !== 'string') continue
    byRef.set(item.ref, {
      ref: item.ref,
      kind: item.kind === 'product' || item.kind === 'sku' ? item.kind : 'shop',
      display_name: typeof item.display_name === 'string' ? item.display_name : null,
      sku_label: typeof item.sku_label === 'string' ? item.sku_label : null,
      name_source: typeof item.name_source === 'string'
        ? item.name_source as DisplayEntity['name_source'] : 'unresolved',
    })
  }
  return byRef
}

/**
 * 单元格取值：引用一律换成授权展示名。
 * 名称未取得就显示占位——宁可不显示，也不拿引用或猜一个名字当数据。
 * 引用仍然保留在单元格的 data-ref 上（见 ArtifactView），便于按稳定引用追查。
 */
export function cellText(value: unknown, entities: Map<string, DisplayEntity>) {
  if (isRef(value)) {
    const entity = entities.get(value)
    if (!entity || !entity.display_name) return '名称未取得'
    const source = entity.name_source === 'trade_snapshot' ? '（成交名）' : ''
    const spec = entity.sku_label ? ` ${entity.sku_label}` : ''
    return `${entity.display_name}${source}${spec}`
  }
  return text(value)
}

function isRef(value: unknown): value is string {
  return typeof value === 'string' && /^ent-[0-9a-z]{8}$/.test(value)
}

export function ArtifactView({ artifact }: { artifact: Artifact }) {
  const entities = artifactEntities(artifact)
  const rows = Array.isArray(artifact.data) ? artifact.data.filter(
    (row): row is Record<string, unknown> => typeof row === 'object' && row !== null && !Array.isArray(row),
  ) : []
  // 提示行只带 notice：它不是数据行，不能挤进表格里当空行。
  const notices = rows.filter((row) => typeof row.notice === 'string').map((row) => String(row.notice))
  const dataRows = rows.filter((row) => typeof row.notice !== 'string')
  const first = dataRows[0]
  const columns = first ? [...new Set(dataRows.flatMap((row) => Object.keys(row)))] : []
  const numericKeys = first ? columns.filter((key) => !DIMENSION_COLUMNS.has(key)) : []
  const filters = isRecord(artifact.filters) ? artifact.filters : undefined
  const shopRefs = Array.isArray(filters?.shop_refs)
    ? filters.shop_refs.filter((ref): ref is string => typeof ref === 'string') : []
  const limitations = Array.isArray(artifact.limitations) ? artifact.limitations : []
  const rangeStart = typeof filters?.start === 'string' ? filters.start : null
  const rangeEnd = typeof filters?.end === 'string' ? filters.end : null
  const asOf = typeof artifact.data_as_of === 'string' ? artifact.data_as_of : null
  const coverage = isRecord(artifact.coverage) ? artifact.coverage.status : undefined
  const notes = [...limitations.map(text), ...notices]

  return (
    <section className="artifact" aria-label="经营数据结果">
      <header className="artifact-head">
        <span className="artifact-title"><TableIcon size={15} />查询结果</span>
        {dataRows.length > 0 && <span className="pill">{dataRows.length} 行</span>}
      </header>
      {first && dataRows.length === 1 && (
        <div className="metric-grid">
          {numericKeys.map((key) => {
            const value = first[key]
            const shown = cellText(value, entities)
            const tone = value === null || value === undefined ? 'missing' : lengthClass(shown)
            return (
              <div className={`metric-card ${tone}`} key={key}>
                <span>{key}</span>
                <strong>{shown}</strong>
              </div>
            )
          })}
        </div>
      )}
      {dataRows.length > 1 && (
        <div className="table-wrap">
          <table>
            <thead><tr>{columns.map((key) => <th key={key}>{key}</th>)}</tr></thead>
            <tbody>{dataRows.map((row, index) => (
              <tr key={`${row.day ?? ''}-${row.product_ref ?? row.shop_ref ?? index}`}>
                {columns.map((key) => (
                  <td key={key} data-ref={isRef(row[key]) ? row[key] : undefined}>
                    {cellText(row[key], entities)}
                  </td>
                ))}
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
      <div className="artifact-meta">
        {rangeStart && rangeEnd && <span>期间：{rangeStart} 至 {rangeEnd}</span>}
        {shopRefs.length > 0 && <span>范围：{shopRefs.map((ref) => cellText(ref, entities)).join('、')}</span>}
        {asOf && <span><ClockIcon size={13} />数据截止：{asOf}</span>}
        {coverage !== undefined && <span><GaugeIcon size={13} />覆盖：{text(coverage)}</span>}
      </div>
      {notes.length > 0 && <p className="limitations">{notes.join('；')}</p>}
    </section>
  )
}
