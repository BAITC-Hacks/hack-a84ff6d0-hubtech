import { fmtDate } from '../format'
import Icon from '../Icons'

export default function DataInfo({ data }) {
  if (!data) return null
  const source =
    {
      excel: 'Excel-выгрузки 1С',
      csv: 'CSV-таблицы',
      synthetic: 'Демонстрационные данные',
    }[data.data_source] || 'Данные сервиса'
  const warnings = [...new Set(data.warnings || [])]
  return (
    <section className="data-info" aria-label="Источник и ограничения данных">
      <div className="source-heading">
        <Icon name="data" />
        <div>
          <strong>{source}</strong>
          <p>Данные на {fmtDate(data.as_of)}</p>
        </div>
        {data.data_source === 'synthetic' && (
          <span className="source-tag">Демо</span>
        )}
      </div>
      {warnings.length > 0 && (
        <details>
          <summary>Ограничения и допущения · {warnings.length}</summary>
          <ul>
            {warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </details>
      )}
    </section>
  )
}
