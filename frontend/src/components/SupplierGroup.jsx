import { useEffect, useMemo, useState, useSyncExternalStore } from 'react'
import { fmt, fmtUnits, URGENCY } from '../format'
import { canApproveLine, quantityError, summarizeLines } from '../orderUtils'
import Icon from '../Icons'

const MOBILE_QUERY = '(max-width: 767px)'

function subscribeToViewport(onChange) {
  const query = window.matchMedia?.(MOBILE_QUERY)
  if (!query) return () => {}
  query.addEventListener('change', onChange)
  return () => query.removeEventListener('change', onChange)
}

function viewportPageSize() {
  return window.matchMedia?.(MOBILE_QUERY).matches ? 5 : 20
}

export default function SupplierGroup({
  group,
  filteredLines,
  draft,
  onQuantity,
  onApprove,
  onExplain,
  disabled,
  focusLine,
}) {
  const pageSize = useSyncExternalStore(subscribeToViewport, viewportPageSize, () => 20)
  const focusIndex = focusLine
    ? filteredLines.findIndex((line) => line.line_id === focusLine.id)
    : -1
  // Keep an item anchor, so changing the page size preserves its page.
  const [pageAnchor, setPageAnchor] = useState(() => Math.max(0, focusIndex))
  const totals = useMemo(
    () => summarizeLines(group.lines, draft.quantities, draft.approvals),
    [group.lines, draft],
  )
  const pageCount = Math.ceil(filteredLines.length / pageSize)
  const currentPage = Math.min(Math.floor(pageAnchor / pageSize), Math.max(0, pageCount - 1))
  const lines = filteredLines.slice(
    currentPage * pageSize,
    (currentPage + 1) * pageSize,
  )
  const approvalIds = group.lines
    .filter(
      (line) =>
        !quantityError(line, draft.quantities[line.line_id]) &&
        canApproveLine(line) &&
        Number(draft.quantities[line.line_id]) > 0,
    )
    .map((line) => line.line_id)
  const allApproved = approvalIds.length > 0 && approvalIds.every((id) => draft.approvals[id])
  const headingId = `supplier-${encodeURIComponent(group.supplier_id)}`
  useEffect(() => {
    if (focusIndex < 0 || !focusLine) return
    const timer = requestAnimationFrame(() =>
      document
        .getElementById(`qty-${encodeURIComponent(focusLine.id)}`)
        ?.focus(),
    )
    return () => cancelAnimationFrame(timer)
  }, [focusLine, focusIndex, pageSize])
  return (
    <section className="group" aria-labelledby={headingId}>
      <div className="group-head">
        <span className="supplier-mark">
          <Icon name="truck" size={24} />
        </span>
        <div className="supplier-title">
          <h3 id={headingId}>{group.supplier_name}</h3>
          <span>Поставка ~{group.lead_time_days} дн</span>
        </div>
        <span className="meta">
          {totals.invalid
            ? 'Проверьте количество'
            : fmtUnits(totals.unitsByUnit)}{' '}
          · {totals.positions} поз. к заказу
        </span>
        <button
          className="ghost group-approve"
          disabled={
            disabled ||
            (!allApproved && (totals.invalid > 0 || !approvalIds.length))
          }
          onClick={() => onApprove(approvalIds, !allApproved)}
        >
          {allApproved
            ? 'Снять утверждение'
            : `Утвердить поставщика · ${approvalIds.length}`}
        </button>
      </div>
      <p className="scroll-help">
        На телефоне позиции показаны карточками. Утверждение поставщика
        учитывает все его позиции, включая скрытые поиском и другими страницами.
      </p>
      <div
        className="table-scroll"
        tabIndex="0"
        role="region"
        aria-label={`Позиции ${group.supplier_name}`}
      >
        <table>
          <caption className="sr-only">
            Позиции поставщика {group.supplier_name}
          </caption>
          <thead>
            <tr>
              <th scope="col">
                <span className="sr-only">Утверждено</span>
              </th>
              <th scope="col">Код 1С / артикул</th>
              <th scope="col">Наименование / склад</th>
              <th scope="col">Срочность</th>
              <th scope="col" className="num">
                Покрытие
              </th>
              <th scope="col" className="num">
                К заказу
              </th>
              <th scope="col">
                <span className="sr-only">Обоснование</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {lines.map((line) => {
              const qty = draft.quantities[line.line_id]
              const error = quantityError(line, qty)
              const urgency = URGENCY[line.urgency] || URGENCY.low
              const id = encodeURIComponent(line.line_id)
              const label = `${line.sku}, ${line.warehouse || 'без склада'}`
              return (
                <tr
                  key={line.line_id}
                  className={
                    draft.approvals[line.line_id] ? 'row-approved' : ''
                  }
                >
                  <td>
                    <label className="checkbox-target">
                      <input
                        type="checkbox"
                        checked={Boolean(draft.approvals[line.line_id])}
                        aria-label={`Утвердить ${label}`}
                        disabled={
                          disabled || Boolean(error) || Number(qty) === 0 || !canApproveLine(line)
                        }
                        onChange={(event) =>
                          onApprove([line.line_id], event.target.checked)
                        }
                      />
                    </label>
                  </td>
                  <td className="mono" data-label="Код 1С / артикул">
                    {line.sku}
                    {line.supplier_sku && line.supplier_sku !== line.sku && (
                      <span className="line-meta">
                        У поставщика: {line.supplier_sku}
                      </span>
                    )}
                  </td>
                  <td>
                    <span className="product-name">{line.name}</span>
                    <span className="line-meta">
                      {line.warehouse || 'Склад не указан'} ·{' '}
                      {line.unit || 'ед.'}
                    </span>
                    {line.product_category && <span className="line-meta">{line.product_category}</span>}
                    {!canApproveLine(line) && <span className="line-warning">Уточните единицу измерения перед утверждением.</span>}
                    {line.warnings?.length > 0 && (
                      <span className="line-warning">
                        Есть ограничения — см. «Почему?»
                      </span>
                    )}
                  </td>
                  <td>
                    <span className={`badge ${urgency.cls}`}>
                      {urgency.label}
                    </span>
                  </td>
                  <td className="num" data-label="Покрытие">
                    <span className={`cover-value ${urgency.cls}`}>
                      {fmt(line.days_of_cover)}
                    </span>
                    <span className="line-meta">дней</span>
                  </td>
                  <td className="num quantity-cell" data-label="К заказу">
                    <input
                      id={`qty-${id}`}
                      className={`qty ${error ? 'qty-invalid' : ''}`}
                      type="number"
                      min="0"
                      inputMode="decimal"
                      step={line.pack_size || 'any'}
                      value={qty}
                      aria-label={`Количество к заказу ${label}`}
                      aria-invalid={Boolean(error)}
                      aria-describedby={error ? `error-${id}` : `rule-${id}`}
                      disabled={disabled}
                      onChange={(event) =>
                        onQuantity(line.line_id, event.target.value)
                      }
                    />
                    <span className="quantity-rule" id={`rule-${id}`}>
                      Мин. {fmt(line.min_order_qty || 0)} · кратно{' '}
                      {fmt(line.pack_size || 1)}
                    </span>
                    {error && (
                      <span id={`error-${id}`} className="quantity-error">
                        {error}
                      </span>
                    )}
                    {!error && Number(qty) !== line.recommended_qty && (
                      <span className="quantity-rule">
                        Рекомендовано {fmt(line.recommended_qty)}
                      </span>
                    )}
                    {!error && Number(qty) === 0 && (
                      <span className="quantity-rule">Исключено из заказа</span>
                    )}
                  </td>
                  <td>
                    <button
                      className="link"
                      aria-haspopup="dialog"
                      aria-label={`Обоснование ${label}`}
                      onClick={() => onExplain(line)}
                    >
                      Почему?
                    </button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {pageCount > 1 && (
        <nav
          className="pagination"
          aria-label={`Страницы: ${group.supplier_name}`}
        >
          <span>
            {currentPage * pageSize + 1}–
            {Math.min((currentPage + 1) * pageSize, filteredLines.length)} из{' '}
            {filteredLines.length}
          </span>
          <button
            className="ghost"
            disabled={currentPage === 0}
            onClick={() => setPageAnchor((currentPage - 1) * pageSize)}
            aria-label={`Предыдущая страница: ${group.supplier_name}`}
          >
            Назад
          </button>
          <span aria-live="polite">
            {currentPage + 1} / {pageCount}
          </span>
          <button
            className="ghost"
            disabled={currentPage + 1 === pageCount}
            onClick={() => setPageAnchor((currentPage + 1) * pageSize)}
            aria-label={`Следующая страница: ${group.supplier_name}`}
          >
            Далее
          </button>
        </nav>
      )}
    </section>
  )
}
