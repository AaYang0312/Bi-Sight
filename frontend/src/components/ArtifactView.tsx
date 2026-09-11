import type { Artifact } from '../types'
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

export function ArtifactView({ artifact }: { artifact: Artifact }) {
  const rows = Array.isArray(artifact.data) ? artifact.data.filter(
    (row): row is Record<string, unknown> => typeof row === 'object' && row !== null && !Array.isArray(row),
  ) : []
  const first = rows[0]
  const numericKeys = first ? Object.keys(first).filter((key) =>
    !['day', 'shop_id', 'product_id', 'currency', 'basis'].includes(key),
  ) : []
  const filters = artifact.filters as Record<string, unknown> | undefined
  const limitations = Array.isArray(artifact.limitations) ? artifact.limitations : []
  const rangeStart = typeof filters?.start === 'string' ? filters.start : null
  const rangeEnd = typeof filters?.end === 'string' ? filters.end : null
  const asOf = typeof artifact.data_as_of === 'string' ? artifact.data_as_of : null
  const coverage = isRecord(artifact.coverage) ? artifact.coverage.status : undefined

  return (
    <section className="artifact" aria-label="经营数据结果">
      <header className="artifact-head">
        <span className="artifact-title"><TableIcon size={15} />查询结果</span>
        {rows.length > 0 && <span className="pill">{rows.length} 行</span>}
      </header>
      {first && rows.length === 1 && (
        <div className="metric-grid">
          {numericKeys.map((key) => {
            const value = first[key]
            const shown = text(value)
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
      {rows.length > 1 && (
        <div className="table-wrap">
          <table>
            <thead><tr>{Object.keys(first ?? {}).map((key) => <th key={key}>{key}</th>)}</tr></thead>
            <tbody>{rows.map((row, index) => (
              <tr key={`${row.day ?? ''}-${row.product_id ?? row.shop_id ?? index}`}>
                {Object.keys(first ?? {}).map((key) => <td key={key}>{text(row[key])}</td>)}
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
      <div className="artifact-meta">
        {rangeStart && rangeEnd && <span>期间：{rangeStart} 至 {rangeEnd}</span>}
        {asOf && <span><ClockIcon size={13} />数据截止：{asOf}</span>}
        {coverage !== undefined && <span><GaugeIcon size={13} />覆盖：{text(coverage)}</span>}
      </div>
      {limitations.length > 0 && <p className="limitations">{limitations.map(text).join('；')}</p>}
    </section>
  )
}
