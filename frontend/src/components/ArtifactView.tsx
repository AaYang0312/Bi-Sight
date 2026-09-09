import type { Artifact } from '../types'

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
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
      {first && rows.length === 1 && (
        <div className="metric-grid">
          {numericKeys.map((key) => (
            <div className="metric-card" key={key}>
              <span>{key}</span>
              <strong>{text(first[key])}</strong>
            </div>
          ))}
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
        {asOf && <span>数据截止：{asOf}</span>}
        {coverage !== undefined && <span>覆盖：{text(coverage)}</span>}
      </div>
      {limitations.length > 0 && <p className="limitations">{limitations.map(text).join('；')}</p>}
    </section>
  )
}
