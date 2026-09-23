import { useEffect, useMemo, useState } from 'react'
import { fetchMeta, recommend, exportExcel } from './api'
import { buildExportPayload, getQuantity, quantityError, summarizeLines } from './orderUtils'
import './App.css'

const PAGE_SIZE = 50
const URGENCY = {
  high: { label: 'Срочно', cls: 'u-high' },
  medium: { label: 'Средне', cls: 'u-medium' },
  low: { label: 'Плановый', cls: 'u-low' },
}

function fmt(n) {
  return Number.isFinite(Number(n)) ? Number(n).toLocaleString('ru-RU', { maximumFractionDigits: 2 }) : '—'
}

function fmtUnits(units) {
  return Object.entries(units).sort(([left], [right]) => left.localeCompare(right, 'ru'))
    .map(([unit, quantity]) => `${fmt(quantity)} ${unit}`).join(' · ') || '0'
}

function fmtDate(value) {
  if (!value) return 'не указана'
  const parts = String(value).slice(0, 10).split('-')
  return parts.length === 3 ? `${parts[2]}.${parts[1]}.${parts[0]}` : String(value)
}

function DataInfo({ data }) {
  if (!data) return null
  const source = { excel: 'Excel-выгрузки поставщиков', synthetic: 'Демонстрационные данные', csv: 'CSV-таблицы' }[data.data_source] || 'Данные сервиса'
  const warnings = [...new Set(data.warnings || [])]
  return (
    <section className="data-info" aria-label="Источник данных">
      <p><strong>{source}</strong> · Дата расчёта: {fmtDate(data.as_of)}</p>
      {warnings.length > 0 && (
        <details>
          <summary>Ограничения данных и допущения ({warnings.length})</summary>
          <ul>{warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
        </details>
      )}
    </section>
  )
}

function Line({ line, qty, onQty, approved, onApprove, disabled }) {
  const [open, setOpen] = useState(false)
  const r = line.rationale
  const u = URGENCY[line.urgency] || URGENCY.low
  const error = quantityError(line, qty)
  const errorId = `quantity-error-${encodeURIComponent(line.line_id)}`
  const label = `${line.sku}${line.warehouse ? `, ${line.warehouse}` : ''}`
  return (
    <>
      <tr className={approved ? 'row-approved' : ''}>
        <td>
          <input type="checkbox" checked={approved} onChange={onApprove}
            aria-label={`Утвердить ${label}`} disabled={disabled || Boolean(error) || Number(qty) === 0} />
        </td>
        <td className="mono">{line.sku}</td>
        <td>
          {line.name}
          <span className="line-meta">{line.warehouse || 'Склад не указан'} · {line.unit || 'ед.'}</span>
          {line.warnings?.length > 0 && <span className="line-warning">Есть ограничения данных — см. обоснование</span>}
        </td>
        <td><span className={`badge ${u.cls}`}>{u.label}</span></td>
        <td className="num">{fmt(line.days_of_cover)}</td>
        <td className="num quantity-cell">
          <input className={`qty ${error ? 'qty-invalid' : ''}`} type="number" min="0"
            step={line.pack_size || 'any'} value={qty} onChange={(event) => onQty(event.target.value)}
            aria-label={`Количество к заказу ${label}`} aria-invalid={Boolean(error)}
            aria-describedby={error ? errorId : undefined} disabled={disabled} />
          <span className="quantity-rule">мин. {fmt(line.min_order_qty || 0)} · кратно {fmt(line.pack_size || 1)}</span>
          {error && <span id={errorId} className="quantity-error">{error}</span>}
          {!error && Number(qty) === 0 && <span className="quantity-rule">Исключено из заказа</span>}
        </td>
        <td>
          <button className="link" onClick={() => setOpen((value) => !value)}
            aria-expanded={open} aria-label={`${open ? 'Скрыть' : 'Показать'} обоснование ${label}`}>
            {open ? 'скрыть' : 'почему?'}
          </button>
        </td>
      </tr>
      {open && (
        <tr className="explain-row">
          <td colSpan={7}>
            <div className="explain">
              <p className="explain-text">{line.explanation}</p>
              <div className="chips">
                <span>прогноз спроса: <b>{fmt(r.forecast_demand)}</b></span>
                <span>спрос/день: <b>{fmt(r.avg_daily_demand)}</b></span>
                <span>сезонность: <b>×{fmt(r.seasonality_factor)}</b></span>
                <span>тренд: <b>×{fmt(r.trend_factor)}</b></span>
                <span>страховой запас: <b>{fmt(r.safety_stock)}</b></span>
                <span>остаток: <b>{fmt(r.on_hand)}</b>{r.stock_as_of ? ` на ${fmtDate(r.stock_as_of)}` : ''}</span>
                <span>учтено в пути: <b>{fmt(r.in_transit)}</b></span>
                {r.ignored_in_transit > 0 && <span className="chip-warn">не учтено в пути: <b>{fmt(r.ignored_in_transit)}</b></span>}
                {r.lost_demand_uplift > 0 && <span className="chip-warn">упущенный спрос: <b>+{fmt(r.lost_demand_uplift)}</b></span>}
                {r.excluded_bulk_orders > 0 && (
                  <span className="chip-warn">исключён опт: <b>{r.excluded_bulk_orders} записей / {fmt(r.excluded_bulk_units)} ед.</b></span>
                )}
              </div>
              {line.warnings?.length > 0 && <ul className="line-warnings">{line.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

function SupplierGroup({ group, qtyEdits, approved, onQty, onApprove, onApproveGroup, disabled }) {
  const [page, setPage] = useState(0)
  const totals = useMemo(() => summarizeLines(group.lines, qtyEdits, approved), [group.lines, qtyEdits, approved])
  const pageCount = Math.ceil(group.lines.length / PAGE_SIZE)
  const visibleLines = group.lines.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)
  const allApproved = totals.positions > 0 && totals.approved === totals.positions
  return (
    <section className="group">
      <div className="group-head">
        <h2>{group.supplier_name}</h2>
        <span className="meta">срок поставки {group.lead_time_days} дн · {totals.invalid ? 'сумма после исправления' : fmtUnits(totals.unitsByUnit)} · {group.lines.length} поз.</span>
        <button className="link group-approve" disabled={disabled || (!allApproved && (totals.invalid > 0 || !totals.positions))}
          onClick={() => onApproveGroup(group.lines, !allApproved)}>
          {allApproved ? 'Снять утверждение' : `Утвердить все ${totals.positions} поз.`}
        </button>
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th><span className="sr-only">Утверждено</span></th><th>Артикул</th><th>Наименование / склад</th><th>Срочность</th>
              <th className="num">Покрытие, дн</th><th className="num">К заказу</th><th><span className="sr-only">Обоснование</span></th>
            </tr>
          </thead>
          <tbody>
            {visibleLines.map((line) => (
              <Line key={line.line_id} line={line} qty={getQuantity(line, qtyEdits)}
                onQty={(value) => onQty(line.line_id, value)} approved={Boolean(approved[line.line_id])}
                onApprove={() => onApprove(line.line_id)} disabled={disabled} />
            ))}
          </tbody>
        </table>
      </div>
      {pageCount > 1 && (
        <nav className="pagination" aria-label={`Страницы: ${group.supplier_name}`}>
          <span>{page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, group.lines.length)} из {group.lines.length} · утверждения и экспорт учитывают все страницы</span>
          <button className="ghost" disabled={page === 0} onClick={() => setPage((value) => value - 1)} aria-label={`Предыдущая страница: ${group.supplier_name}`}>Назад</button>
          <span>{page + 1} / {pageCount}</span>
          <button className="ghost" disabled={page + 1 === pageCount} onClick={() => setPage((value) => value + 1)} aria-label={`Следующая страница: ${group.supplier_name}`}>Далее</button>
        </nav>
      )}
    </section>
  )
}

export default function App() {
  const [meta, setMeta] = useState(null)
  const [warehouse, setWarehouse] = useState('')
  const [category, setCategory] = useState('')
  const [serviceLevel, setServiceLevel] = useState(0.95)
  const [reviewPeriod, setReviewPeriod] = useState(14)
  const [explain, setExplain] = useState(false)
  const [loading, setLoading] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [result, setResult] = useState(null)
  const [calculatedParams, setCalculatedParams] = useState(null)
  const [qtyEdits, setQtyEdits] = useState({})
  const [approved, setApproved] = useState({})
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  useEffect(() => {
    let active = true
    fetchMeta().then((data) => { if (active) setMeta(data) })
      .catch((failure) => { if (active) setError(failure.message) })
    return () => { active = false }
  }, [])

  const params = {
    warehouse: warehouse || null,
    category: category || null,
    service_level: Number(serviceLevel),
    review_period_days: Number(reviewPeriod),
    explain,
  }
  const validPeriod = reviewPeriod !== '' && Number.isInteger(Number(reviewPeriod)) && Number(reviewPeriod) >= 1 && Number(reviewPeriod) <= 120
  const pendingSettings = result && JSON.stringify(params) !== JSON.stringify(calculatedParams)
  const lines = useMemo(() => result?.groups.flatMap((group) => group.lines) || [], [result])
  const totals = useMemo(() => summarizeLines(lines, qtyEdits, approved), [lines, qtyEdits, approved])
  const busy = loading || exporting

  async function run() {
    if (!validPeriod || busy) return
    setLoading(true)
    setError('')
    setNotice('')
    try {
      const data = await recommend(params)
      setResult(data)
      setCalculatedParams(params)
      setQtyEdits({})
      setApproved({})
    } catch (failure) {
      setError(failure.message)
    } finally {
      setLoading(false)
    }
  }

  function editQuantity(lineId, value) {
    setQtyEdits((previous) => ({ ...previous, [lineId]: value }))
    setApproved((previous) => ({ ...previous, [lineId]: false }))
    setNotice('')
  }

  function approveGroup(groupLines, value) {
    setApproved((previous) => {
      const next = { ...previous }
      for (const line of groupLines) {
        const quantity = getQuantity(line, qtyEdits)
        next[line.line_id] = value && !quantityError(line, quantity) && Number(quantity) > 0
      }
      return next
    })
    setNotice('')
  }

  async function exportOrder(approvedOnly) {
    if (!result || busy) return
    setExporting(true)
    setError('')
    setNotice('')
    try {
      await exportExcel(buildExportPayload(result, qtyEdits, approved, approvedOnly))
      setNotice(approvedOnly ? 'Excel с утверждёнными позициями подготовлен.' : 'Excel с текущими количествами подготовлен. Статус утверждения указан для каждой позиции.')
    } catch (failure) {
      setError(failure.message)
    } finally {
      setExporting(false)
    }
  }

  return (
    <div className="app">
      <header>
        <h1>ЭКТ · Автозаказы поставщикам</h1>
        <p className="sub">Рекомендованное пополнение склада на основе прогноза спроса · HackAlem AI</p>
      </header>
      <DataInfo data={result || meta} />
      <section className="controls" aria-label="Параметры расчёта">
        <label>Склад
          <select value={warehouse} onChange={(event) => setWarehouse(event.target.value)}>
            <option value="">Все</option>
            {meta?.warehouses.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <label>Категория
          <select value={category} onChange={(event) => setCategory(event.target.value)}>
            <option value="">Все</option>
            {meta?.categories.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <label>Уровень сервиса
          <select value={serviceLevel} onChange={(event) => setServiceLevel(event.target.value)}>
            <option value="0.90">90%</option><option value="0.95">95%</option><option value="0.98">98%</option><option value="0.99">99%</option>
          </select>
        </label>
        <label>Период проверки, дн
          <input type="number" min="1" max="120" step="1" value={reviewPeriod}
            aria-invalid={!validPeriod} aria-describedby={!validPeriod ? 'period-error' : undefined}
            onChange={(event) => setReviewPeriod(event.target.value)} />
        </label>
        <label className="chk"><input type="checkbox" checked={explain} onChange={(event) => setExplain(event.target.checked)} />LLM-обоснования</label>
        <button className="primary" onClick={run} disabled={busy || !meta || !validPeriod}>{loading ? 'Считаю…' : 'Рассчитать заказ'}</button>
      </section>
      {!validPeriod && <p className="validation-message" id="period-error">Укажите целое число дней от 1 до 120.</p>}
      {error && <div className="error" role="alert">{error}</div>}
      {notice && <p className="success" role="status">{notice}</p>}

      {result && (
        <>
          <section className="result-context" aria-label="Параметры отображённого расчёта">
            <p>Текущий расчёт: <strong>{calculatedParams.warehouse || 'все склады'}</strong> · {calculatedParams.category || 'все категории'} · сервис {fmt(calculatedParams.service_level * 100)}% · период проверки {calculatedParams.review_period_days} дн · дата расчёта {fmtDate(result.as_of)}</p>
            {pendingSettings && <p className="pending" role="status">Параметры изменены. Нажмите «Рассчитать заказ», чтобы применить их. Экспорт использует текущий расчёт и ваши правки.</p>}
          </section>
          <section className="summary" aria-label="Итоги заказа">
            <div><span>Поставщиков</span><b>{result.groups.length}</b></div>
            <div><span>Позиций в заказе</span><b>{totals.positions}</b></div>
            <div><span>Количество по единицам</span><b className="unit-breakdown">{totals.invalid ? '—' : fmtUnits(totals.unitsByUnit)}</b></div>
            <div><span>Риск дефицита, поз.</span><b className="danger">{totals.high}</b></div>
            <div><span>Утверждено позиций</span><b>{totals.approved}</b></div>
            <div><span>Утверждено по единицам</span><b className="unit-breakdown">{fmtUnits(totals.approvedByUnit)}</b></div>
          </section>
          <div className="export-controls">
            <button className="ghost" onClick={() => exportOrder(false)} disabled={busy || totals.invalid > 0 || !totals.positions}>Excel: все позиции с правками</button>
            <button className="primary" onClick={() => exportOrder(true)} disabled={busy || totals.invalid > 0 || !totals.approved}>Excel: только утверждённые ({totals.approved})</button>
            {exporting && <span role="status">Готовлю Excel…</span>}
          </div>
          <p className="export-help">Нулевые количества исключаются из заказа. Изменение количества снимает утверждение. При новом расчёте правки и утверждения сбрасываются.</p>
          {totals.invalid > 0 && <p className="validation-message" role="status">Исправьте количества: {totals.invalid} поз. Экспорт станет доступен после исправления.</p>}
          {!lines.length && <div className="empty">Для выбранных параметров нет позиций к заказу.</div>}
          {result.groups.map((group) => (
            <SupplierGroup key={`${result.calculation_id}:${group.supplier_id}`} group={group} qtyEdits={qtyEdits} approved={approved}
              onQty={editQuantity} onApprove={(lineId) => {
                setApproved((previous) => ({ ...previous, [lineId]: !previous[lineId] }))
                setNotice('')
              }} onApproveGroup={approveGroup} disabled={busy} />
          ))}
        </>
      )}
      {!result && !error && <div className="empty">{loading ? 'Рассчитываю рекомендации по данным поставщиков…' : 'Задайте параметры и нажмите «Рассчитать заказ».'}</div>}
    </div>
  )
}
