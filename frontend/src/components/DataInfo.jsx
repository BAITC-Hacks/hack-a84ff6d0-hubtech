import { fmt, fmtDate } from '../format'
import Icon from '../Icons'

const CATALOG_STATUS = {
  disabled: 'Не подключён',
  missing: 'Снимок не загружен',
  invalid: 'Снимок недоступен',
  empty_catalog: 'Учётный каталог пуст',
  partial: 'Частичное покрытие',
  loaded: 'Справочник подключён',
}
const isCount = (value) => Number.isSafeInteger(value) && value >= 0

export default function DataInfo({ data }) {
  if (!data) return null
  const source =
    {
      excel: 'Excel-выгрузки 1С',
      csv: 'CSV-таблицы',
      synthetic: 'Демонстрационные данные',
    }[data.data_source] || 'Данные сервиса'
  const warnings = [
    ...new Set(
      Array.isArray(data.warnings)
        ? data.warnings.filter((warning) => typeof warning === 'string')
        : [],
    ),
  ]
  const rawEnrichment = data.data_quality?.catalog_enrichment
  const enrichment =
    rawEnrichment &&
    typeof rawEnrichment === 'object' &&
    !Array.isArray(rawEnrichment)
      ? rawEnrichment
      : null
  const hasCoverage =
    isCount(enrichment?.matched) && isCount(enrichment?.total_catalog)
  const sourceDate =
    typeof enrichment?.fetched_at === 'string' ? enrichment.fetched_at : ''
  const status = CATALOG_STATUS[enrichment?.status]
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
      {(data.data_source === 'excel' || enrichment) && (
        <div className="catalog-info" aria-label="Покрытие справочника ekt.kz">
          <p className="catalog-info-heading">
            <strong>Справочник ekt.kz</strong>
            {typeof status === 'string' && (
              <span className="catalog-status">{status}</span>
            )}
          </p>
          <p>
            {hasCoverage
              ? `Сопоставлено ${fmt(enrichment.matched)} из ${fmt(enrichment.total_catalog)} товаров учётного каталога.`
              : 'Сведения о покрытии пока недоступны.'}
            {sourceDate && ` Снимок справочника собран: ${fmtDate(sourceDate, true)}.`}
          </p>
          <p>
            Товарные группы и характеристики помогают проверить позицию.
            Продажи, остатки, единицы измерения и условия заказа берутся из
            учётных данных.
          </p>
          {isCount(enrichment?.unmatched) && enrichment.unmatched > 0 && (
            <p>
              Без сопоставленной карточки: {fmt(enrichment.unmatched)}.
              Эти товары участвуют в расчёте при выборе всех товарных групп.
            </p>
          )}
          {isCount(enrichment?.conflicts) && enrichment.conflicts > 0 && (
            <p className="catalog-warning">
              Неоднозначных совпадений: {fmt(enrichment.conflicts)}.
              Справочные сведения для них не применены.
            </p>
          )}
          {isCount(enrichment?.crawl_quarantined) &&
            enrichment.crawl_quarantined > 0 && (
              <p className="catalog-warning">
                При сборе справочника исключено позиций с противоречивыми
                кодами или артикулами: {fmt(enrichment.crawl_quarantined)}.
              </p>
            )}
        </div>
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
