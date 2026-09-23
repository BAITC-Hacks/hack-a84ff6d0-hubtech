import { useEffect, useRef, useState } from 'react'
import { fmt, fmtDate } from '../format'
import { safeEktUrl } from '../orderUtils'
import Icon from '../Icons'

export default function RationaleDialog({ line, onClose }) {
  const ref = useRef(null)
  const previousFocus = useRef(null)
  const startY = useRef(null)
  const wasDragged = useRef(false)
  const [drag, setDrag] = useState(0)
  useEffect(() => {
    previousFocus.current = document.activeElement
    const dialog = ref.current
    dialog.showModal()
    return () => {
      dialog.close()
      previousFocus.current?.focus()
    }
  }, [])
  const r = line.rationale
  const catalogUrl = safeEktUrl(line.product_url)
  const metrics = [
    ['Горизонт', `${fmt(r.horizon_days)} дн`],
    ['Прогноз спроса', `${fmt(r.forecast_demand)} ${line.unit}`],
    ['Средний спрос в день', `${fmt(r.avg_daily_demand)} ${line.unit}`],
    ['Сезонность', `×${fmt(r.seasonality_factor)}`],
    ['Устойчивый тренд', `×${fmt(r.trend_factor)}`],
    ['Страховой запас', `${fmt(r.safety_stock)} ${line.unit}`],
    [
      r.stock_as_of ? `Остаток на ${fmtDate(r.stock_as_of)}` : 'Остаток',
      `${fmt(r.on_hand)} ${line.unit}`,
    ],
    ['Учтено в пути', `${fmt(r.in_transit)} ${line.unit}`],
    ['Не учтено в пути', `${fmt(r.ignored_in_transit || 0)} ${line.unit}`],
    ['Потребность до округления', `${fmt(r.raw_need)} ${line.unit}`],
    ['Упущенный спрос', `+${fmt(r.lost_demand_uplift || 0)} ${line.unit}`],
    [
      'Исключён разовый опт',
      `${fmt(r.excluded_bulk_orders || 0)} записей · ${fmt(r.excluded_bulk_units || 0)} ${line.unit}`,
    ],
  ]
  return (
    <dialog
      ref={ref}
      className="rationale-dialog"
      aria-labelledby="rationale-title"
      style={{ '--sheet-drag': `${drag}px` }}
      onCancel={(event) => {
        event.preventDefault()
        onClose()
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div className="sheet-content">
        <button
          className="sheet-handle"
          aria-label="Закрыть обоснование свайпом вниз или нажатием"
          onClick={() => {
            if (!wasDragged.current) onClose()
          }}
          onPointerDown={(event) => {
            startY.current = event.clientY
            wasDragged.current = false
            event.currentTarget.setPointerCapture(event.pointerId)
          }}
          onPointerMove={(event) => {
            if (startY.current !== null) {
              const distance = Math.max(0, event.clientY - startY.current)
              if (distance > 10) wasDragged.current = true
              setDrag(distance)
            }
          }}
          onPointerUp={() => {
            startY.current = null
            if (drag > 100) onClose()
            else setDrag(0)
          }}
          onPointerCancel={() => {
            startY.current = null
            setDrag(0)
          }}
        >
          <span />
        </button>
        <div className="dialog-heading">
          <div>
            <span className="step-label">ОБОСНОВАНИЕ</span>
            <h2 id="rationale-title">
              Почему {fmt(line.recommended_qty)} {line.unit}?
            </h2>
          </div>
          <button
            className="icon-button"
            aria-label="Закрыть обоснование"
            onClick={onClose}
          >
            ×
          </button>
        </div>
        <p className="dialog-product">
          <strong>{line.name}</strong>
          <br />
          {line.sku} · {line.warehouse || 'Склад не указан'}
        </p>
        <p className="explain-text">{line.explanation}</p>
        <dl className="rationale-metrics">
          {metrics.map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
        <p className="rationale-rounding">
          <Icon name="box" /> Минимальный заказ {fmt(line.min_order_qty)};
          кратность {fmt(line.pack_size)}. Итог учитывает оба ограничения.
        </p>
        <section className="catalog-details" aria-label="Справочник товара">
          <h3>Справочник товара</h3>
          <p>Категория учёта: {line.category || 'Не указана'}</p>
          {line.product_category ? (
            <>
              <p>Группа EKT: {[line.product_category, line.product_subcategory].filter(Boolean).join(' / ')}</p>
              {line.product_brand && <p>Бренд: {line.product_brand}</p>}
              <dl className="rationale-metrics">
                {Object.entries(line.product_attributes || {}).map(([name, value]) => (
                  <div key={name}><dt>{name}</dt><dd>{value}</dd></div>
                ))}
              </dl>
              {line.catalog_fetched_at && <p>Справочник обновлён {fmtDate(line.catalog_fetched_at)}</p>}
              {catalogUrl && <a href={catalogUrl} target="_blank" rel="noopener noreferrer">Карточка на ekt.kz ↗</a>}
            </>
          ) : <p>Описание EKT не найдено. Количества рассчитаны по учётным данным.</p>}
        </section>
        {line.warnings?.length > 0 && (
          <div className="rationale-warnings">
            <h3>Ограничения данных</h3>
            <ul>
              {line.warnings.map((warning, index) => (
                <li key={index}>{warning}</li>
              ))}
            </ul>
          </div>
        )}
        <button className="primary dialog-close" onClick={onClose}>
          Понятно
        </button>
      </div>
    </dialog>
  )
}
