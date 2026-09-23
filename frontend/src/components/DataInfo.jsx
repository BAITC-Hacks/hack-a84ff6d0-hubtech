import { fmt, fmtDate } from '../format'
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
  const catalog = data.data_quality?.catalog_enrichment
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
      {catalog && Number.isFinite(catalog.matched) && Number.isFinite(catalog.unmatched) && (
        <p className="catalog-coverage">
          Описания EKT: {fmt(catalog.matched)} из {fmt(catalog.matched + catalog.unmatched)} товаров.
          {catalog.fetched_at && <> Обновлены {fmtDate(catalog.fetched_at)}.</>}
          {' '}Справочник не заменяет учётные остатки и условия закупки.
        </p>
      )}
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
