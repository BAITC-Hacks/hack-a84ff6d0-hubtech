import { useEffect, useMemo, useState } from 'react'
import { fetchMeta, recommend, exportExcel } from './api'
import './App.css'

const URGENCY = {
  high: { label: 'Срочно', cls: 'u-high' },
  medium: { label: 'Средне', cls: 'u-medium' },
  low: { label: 'Плановый', cls: 'u-low' },
}

function fmt(n) {
  return Number(n).toLocaleString('ru-RU', { maximumFractionDigits: 2 })
}

function Line({ line, qty, onQty, approved, onApprove }) {
  const [open, setOpen] = useState(false)
  const r = line.rationale
  const u = URGENCY[line.urgency] || URGENCY.low
  return (
    <>
      <tr className={approved ? 'row-approved' : ''}>
        <td>
          <input type="checkbox" checked={approved} onChange={onApprove} />
        </td>
        <td className="mono">{line.sku}</td>
        <td>{line.name}</td>
        <td><span className={`badge ${u.cls}`}>{u.label}</span></td>
        <td className="num">{fmt(line.days_of_cover)}</td>
        <td className="num">
          <input
            className="qty"
            type="number"
            min="0"
            value={qty}
            onChange={(e) => onQty(e.target.value)}
          />
        </td>
        <td>
          <button className="link" onClick={() => setOpen((v) => !v)}>
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
                <span>остаток: <b>{fmt(r.on_hand)}</b></span>
                <span>в пути: <b>{fmt(r.in_transit)}</b></span>
                {r.lost_demand_uplift > 0 && (
                  <span className="chip-warn">упущенный спрос: <b>+{fmt(r.lost_demand_uplift)}</b></span>
                )}
                {r.excluded_bulk_orders > 0 && (
                  <span className="chip-warn">
                    исключён опт: <b>{r.excluded_bulk_orders} шт / {fmt(r.excluded_bulk_units)} ед</b>
                  </span>
                )}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
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
  const [result, setResult] = useState(null)
  const [qtyEdits, setQtyEdits] = useState({})
  const [approved, setApproved] = useState({})
  const [error, setError] = useState('')

  useEffect(() => {
    fetchMeta()
      .then(setMeta)
      .catch(() => setError('Не удалось загрузить справочники. Запущен ли backend на :8017?'))
  }, [])

  const params = () => ({
    warehouse: warehouse || null,
    category: category || null,
    service_level: Number(serviceLevel),
    review_period_days: Number(reviewPeriod),
    explain,
  })

  async function run() {
    setLoading(true)
    setError('')
    try {
      const data = await recommend(params())
      setResult(data)
      setQtyEdits({})
      setApproved({})
    } catch (e) {
      setError('Ошибка расчёта. Проверьте backend.')
    } finally {
      setLoading(false)
    }
  }

  const totals = useMemo(() => {
    if (!result) return { units: 0, high: 0 }
    let units = 0
    let high = 0
    for (const g of result.groups) {
      for (const ln of g.lines) {
        const q = qtyEdits[ln.sku] ?? ln.recommended_qty
        units += Number(q)
        if (ln.urgency === 'high') high += 1
      }
    }
    return { units, high }
  }, [result, qtyEdits])

  const approvedCount = Object.values(approved).filter(Boolean).length

  return (
    <div className="app">
      <header>
        <h1>ЭКТ · Автозаказы поставщикам</h1>
        <p className="sub">Рекомендованное пополнение склада на основе прогноза спроса · HackAlem AI</p>
      </header>

      <section className="controls">
        <label>
          Склад
          <select value={warehouse} onChange={(e) => setWarehouse(e.target.value)}>
            <option value="">Все</option>
            {meta?.warehouses.map((w) => <option key={w} value={w}>{w}</option>)}
          </select>
        </label>
        <label>
          Категория
          <select value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="">Все</option>
            {meta?.categories.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>
        <label>
          Уровень сервиса
          <select value={serviceLevel} onChange={(e) => setServiceLevel(e.target.value)}>
            <option value="0.90">90%</option>
            <option value="0.95">95%</option>
            <option value="0.98">98%</option>
            <option value="0.99">99%</option>
          </select>
        </label>
        <label>
          Период проверки, дн
          <input type="number" min="1" max="120" value={reviewPeriod}
                 onChange={(e) => setReviewPeriod(e.target.value)} />
        </label>
        <label className="chk">
          <input type="checkbox" checked={explain} onChange={(e) => setExplain(e.target.checked)} />
          LLM-обоснования
        </label>
        <button className="primary" onClick={run} disabled={loading || !meta}>
          {loading ? 'Считаю…' : 'Рассчитать заказ'}
        </button>
      </section>

      {error && <div className="error">{error}</div>}

      {result && (
        <>
          <section className="summary">
            <div><span>Поставщиков</span><b>{result.groups.length}</b></div>
            <div><span>Позиций</span><b>{result.sku_count}</b></div>
            <div><span>Всего единиц</span><b>{fmt(totals.units)}</b></div>
            <div><span>Срочных</span><b className="danger">{totals.high}</b></div>
            <div><span>Утверждено</span><b>{approvedCount}</b></div>
            <button className="ghost" onClick={() => exportExcel(params())}>Экспорт в Excel</button>
          </section>

          {result.groups.map((g) => (
            <section className="group" key={g.supplier_id}>
              <div className="group-head">
                <h2>{g.supplier_name}</h2>
                <span className="meta">
                  срок поставки {g.lead_time_days} дн · {fmt(g.total_units)} ед · {g.lines.length} поз.
                </span>
              </div>
              <table>
                <thead>
                  <tr>
                    <th></th><th>Артикул</th><th>Наименование</th><th>Срочность</th>
                    <th className="num">Покрытие, дн</th><th className="num">К заказу</th><th></th>
                  </tr>
                </thead>
                <tbody>
                  {g.lines.map((ln) => (
                    <Line
                      key={ln.sku}
                      line={ln}
                      qty={qtyEdits[ln.sku] ?? ln.recommended_qty}
                      onQty={(v) => setQtyEdits((s) => ({ ...s, [ln.sku]: v }))}
                      approved={!!approved[ln.sku]}
                      onApprove={() => setApproved((s) => ({ ...s, [ln.sku]: !s[ln.sku] }))}
                    />
                  ))}
                </tbody>
              </table>
            </section>
          ))}
        </>
      )}

      {!result && !error && (
        <div className="empty">Задайте параметры и нажмите «Рассчитать заказ».</div>
      )}
    </div>
  )
}
