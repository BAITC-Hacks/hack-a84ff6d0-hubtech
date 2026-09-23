import { useEffect, useMemo, useState } from 'react'
import { exportSavedOrder, fetchOrderEvents, updateOrder } from '../api'
import { useOrderEditor } from '../hooks/useOrderEditor'
import {
  fmt,
  fmtDate,
  fmtUnits,
  orderLines,
  safeMessage,
  URGENCY,
} from '../format'
import { summarizeLines } from '../orderUtils'
import { conflictDifferences } from '../orderState'
import DataInfo from '../components/DataInfo'
import SupplierGroup from '../components/SupplierGroup'
import RationaleDialog from '../components/RationaleDialog'
import { Empty, Loading, Status } from '../components/Common'
import Icon from '../Icons'

const EVENT_NAMES = {
  created: 'Заказ создан',
  order_created: 'Заказ создан',
  decisions_saved: 'Решения сохранены',
  updated: 'Заказ обновлён',
  decisions_updated: 'Решения сохранены',
  exported: 'Excel выгружен',
  export: 'Excel выгружен',
  archived: 'Перенесён в архив',
  unarchived: 'Восстановлен из архива',
  renamed: 'Название изменено',
  metadata_updated: 'Свойства заказа изменены',
  order_updated: 'Свойства заказа изменены',
  legacy_imported: 'Расчёт перенесён в историю',
  legacy_exported: 'Excel выгружен',
}

function EventDetails({ event, lines }) {
  const changes = event.changes
  if (!changes || typeof changes !== 'object') return null
  if (Array.isArray(changes))
    return (
      <ul>
        {changes.map((change, index) => {
          const line = lines.find((value) => value.line_id === change.line_id)
          return (
            <li key={change.line_id || index}>
              {line
                ? `${line.sku} · ${line.warehouse || 'без склада'}`
                : 'Позиция заказа'}
              : {fmt(change.before?.quantity)} → {fmt(change.after?.quantity)};{' '}
              {change.after?.approved ? 'утверждено' : 'черновик'}
            </li>
          )
        })}
      </ul>
    )
  if ('approved_only' in changes)
    return (
      <p>
        {changes.approved_only
          ? 'Только утверждённые позиции'
          : 'Черновик со всеми положительными количествами'}
        {changes.revision ? ` · версия ${changes.revision}` : ''}.
      </p>
    )
  if ('positions' in changes)
    return (
      <p>
        Позиций в расчёте: {changes.positions}
        {changes.archived ? '. Сохранён в архиве' : ''}.
      </p>
    )
  if (changes.before && changes.after)
    return (
      <ul>
        {changes.before.title !== changes.after.title && (
          <li>
            Название: «{changes.before.title}» → «{changes.after.title}».
          </li>
        )}
        {changes.before.archived !== changes.after.archived && (
          <li>
            {changes.after.archived
              ? 'Перенесён в архив.'
              : 'Возвращён в работу.'}
          </li>
        )}
      </ul>
    )
  return null
}

export default function OrderPage({
  orderId,
  active,
  onAuthRequired,
  registerGuard,
  navigate,
}) {
  const editor = useOrderEditor(orderId, active, onAuthRequired)
  const { order, draft, loading, saving, dirty, invalid, conflict } = editor
  const flush = editor.flush
  const eventOrderId = order?.id
  const eventOrderRevision = order?.revision
  const [search, setSearch] = useState('')
  const [supplier, setSupplier] = useState('')
  const [urgency, setUrgency] = useState('')
  const [explained, setExplained] = useState(null)
  const [operation, setOperation] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [events, setEvents] = useState(null)
  const [eventsLoading, setEventsLoading] = useState(false)
  const [eventsError, setEventsError] = useState('')
  const [eventsOpen, setEventsOpen] = useState(false)
  const [eventAttempt, setEventAttempt] = useState(0)
  const [title, setTitle] = useState(null)
  const [focusLine, setFocusLine] = useState(null)
  const lines = useMemo(() => orderLines(order), [order])
  const totals = useMemo(
    () => summarizeLines(lines, draft.quantities, draft.approvals),
    [lines, draft],
  )
  const groups = useMemo(() => {
    const query = search.trim().toLocaleLowerCase('ru')
    return (order?.calculation.groups || [])
      .filter((group) => !supplier || group.supplier_id === supplier)
      .map((group) => ({
        group,
        filteredLines: group.lines.filter(
          (line) =>
            (!urgency || line.urgency === urgency) &&
            (!query ||
              [line.sku, line.supplier_sku, line.name, line.warehouse]
                .filter(Boolean)
                .join(' ')
                .toLocaleLowerCase('ru')
                .includes(query)),
        ),
      }))
      .filter((group) => group.filteredLines.length)
  }, [order, search, supplier, urgency])
  const activeSupplierCount = useMemo(
    () =>
      (order?.calculation.groups || []).filter(
        (group) =>
          summarizeLines(group.lines, draft.quantities, draft.approvals)
            .positions > 0,
      ).length,
    [order, draft],
  )

  useEffect(
    () =>
      registerGuard(async () => {
        if (!active) return false
        if (operation) return false
        if (dirty && !conflict) {
          const saved = await flush()
          if (!saved)
            return window.confirm(
              'Изменения пока не сохранены. Покинуть заказ и потерять локальные правки?',
            )
        }
        if (invalid.length || conflict)
          return window.confirm(
            'В заказе есть несохранённые правки. Покинуть его и потерять эти правки?',
          )
        return true
      }),
    [active, operation, dirty, invalid.length, conflict, flush, registerGuard],
  )

  useEffect(() => {
    if (!eventsOpen || !eventOrderId || !active) return
    let current = true
    // The visible audit list is synchronized with a newly saved server revision.
    // oxlint-disable-next-line react/set-state-in-effect
    setEventsLoading(true)
    setEventsError('')
    fetchOrderEvents(eventOrderId)
      .then((data) => {
        if (current) setEvents(data.items)
      })
      .catch((failure) => {
        if (!current) return
        if (failure.status === 401) onAuthRequired()
        else setEventsError(safeMessage(failure))
      })
      .finally(() => {
        if (current) setEventsLoading(false)
      })
    return () => {
      current = false
    }
  }, [
    eventsOpen,
    eventOrderId,
    eventOrderRevision,
    eventAttempt,
    active,
    onAuthRequired,
  ])

  async function approve(ids, approved) {
    setOperation('approval')
    setError('')
    setNotice('')
    try {
      await editor.approve(ids, approved)
    } finally {
      setOperation('')
    }
  }

  async function exportOrder(approvedOnly) {
    setOperation('export')
    setError('')
    setNotice('')
    try {
      const saved = await editor.flush()
      if (!saved) return
      await exportSavedOrder(saved.id, saved.revision, approvedOnly)
      setNotice(
        approvedOnly
          ? 'Excel с утверждёнными позициями подготовлен.'
          : 'Черновик Excel подготовлен.',
      )
      setEventAttempt((value) => value + 1)
    } catch (failure) {
      if (failure.status === 401) onAuthRequired()
      else if (failure.status === 409) {
        await editor.detectConflict()
        setError('Заказ изменился на сервере. Сравните версии перед экспортом.')
      } else setError(safeMessage(failure))
    } finally {
      setOperation('')
    }
  }

  async function changeProperties(values) {
    setOperation('properties')
    setError('')
    setNotice('')
    try {
      const saved = await editor.flush()
      if (!saved) return
      const updated = await updateOrder(saved.id, {
        revision: saved.revision,
        ...values,
      })
      editor.replaceOrder(updated)
      setTitle(null)
      setNotice('Заказ обновлён.')
    } catch (failure) {
      if (failure.status === 401) onAuthRequired()
      else if (failure.status === 409) {
        await editor.detectConflict()
        setError(
          'Другой сотрудник изменил заказ. Сравните версии перед продолжением.',
        )
      } else setError(safeMessage(failure))
    } finally {
      setOperation('')
    }
  }

  function showInvalid() {
    setSearch('')
    setSupplier('')
    setUrgency('')
    setFocusLine({ id: invalid[0].line_id, token: crypto.randomUUID() })
  }

  if (loading) return <Loading>Открываем сохранённый заказ…</Loading>
  if (!order)
    return (
      <>
        <Status error={editor.error} onRetry={editor.retry} />
        <Empty title="Заказ недоступен">
          Проверьте ссылку или откройте историю заказов.
        </Empty>
        <button className="ghost" onClick={() => navigate('#/orders')}>
          К истории
        </button>
      </>
    )
  const disabled = Boolean(operation || conflict || order.archived || !active)
  const visibleCount = groups.reduce(
    (sum, group) => sum + group.filteredLines.length,
    0,
  )
  const differences = conflict?.latest
    ? conflictDifferences(conflict.base, conflict.latest, draft)
    : []
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="step-label">ЗАКАЗ ПОСТАВЩИКАМ</span>
          <h1>{order.title}</h1>
          <p>
            {order.owner_name} · создан {fmtDate(order.created_at, true)} ·
            версия {order.revision}
            {order.archived ? ' · архив' : ''}
          </p>
        </div>
        <button
          className="ghost light-ghost"
          onClick={() => navigate('#/orders')}
        >
          История заказов
        </button>
      </div>
      <div className="order-actions panel">
        <div
          className={`save-indicator ${dirty || invalid.length || conflict ? 'pending-save' : ''}`}
          role="status"
        >
          <Icon
            name={
              saving ? 'clock' : conflict || invalid.length ? 'alert' : 'check'
            }
          />
          {conflict
            ? 'Конфликт изменений'
            : saving
              ? 'Сохраняем…'
              : invalid.length
                ? 'Есть незавершённый ввод'
                : dirty
                  ? 'Есть несохранённые изменения'
                  : `Все изменения сохранены · ${fmtDate(order.updated_at, true)}`}
        </div>
        <div className="inline-actions">
          <button
            className="ghost"
            disabled={disabled || saving}
            onClick={() => setTitle(order.title)}
          >
            Переименовать
          </button>
          <button
            className="ghost"
            disabled={Boolean(
              operation || saving || conflict || invalid.length,
            )}
            onClick={() => changeProperties({ archived: !order.archived })}
          >
            {order.archived ? 'Вернуть в работу' : 'В архив'}
          </button>
        </div>
        {title !== null && (
          <form
            className="rename-form"
            onSubmit={(event) => {
              event.preventDefault()
              if (title.trim()) void changeProperties({ title: title.trim() })
            }}
          >
            <label>
              Название заказа
              <input
                value={title}
                maxLength={160}
                onChange={(event) => setTitle(event.target.value)}
                autoFocus
              />
            </label>
            <button
              className="primary"
              disabled={!title.trim() || Boolean(operation)}
            >
              Сохранить название
            </button>
            <button
              type="button"
              className="ghost"
              onClick={() => setTitle(null)}
            >
              Отмена
            </button>
          </form>
        )}
      </div>
      <Status error={editor.error} onRetry={editor.retry} />
      {conflict && (
        <section
          className="conflict-panel panel"
          aria-labelledby="conflict-heading"
        >
          <h2 id="conflict-heading">Заказ изменён в другой сессии</h2>
          <p>
            Ваши правки остались на этой странице. Автосохранение остановлено.
            Сравните версии перед продолжением.
          </p>
          {!conflict.latest ? (
            <button className="ghost" onClick={editor.refreshConflict}>
              Загрузить серверную версию
            </button>
          ) : (
            <>
              <p>
                Серверная версия: {conflict.latest.revision}. Изменённых
                позиций: {differences.length}.
              </p>
              <div
                className="conflict-table table-scroll"
                tabIndex="0"
                role="region"
                aria-label="Сравнение локальных и серверных изменений"
              >
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Позиция</th>
                      <th scope="col">Ваш ввод</th>
                      <th scope="col">На сервере</th>
                    </tr>
                  </thead>
                  <tbody>
                    {differences.map(
                      ({
                        line,
                        local,
                        server,
                        localChanged,
                        remoteChanged,
                      }) => (
                        <tr key={line.line_id}>
                          <th scope="row">
                            {line.sku}
                            <span className="line-meta">{line.warehouse}</span>
                          </th>
                          <td>
                            {local.quantity === ''
                              ? 'Не завершён'
                              : local.quantity}{' '}
                            · {local.approved ? 'утверждено' : 'черновик'}
                            {localChanged && (
                              <span className="line-meta">Ваше изменение</span>
                            )}
                          </td>
                          <td>
                            {fmt(server.quantity)} ·{' '}
                            {server.approved ? 'утверждено' : 'черновик'}
                            {remoteChanged && (
                              <span className="line-meta">
                                Изменено на сервере
                              </span>
                            )}
                          </td>
                        </tr>
                      ),
                    )}
                  </tbody>
                </table>
              </div>
              <div className="inline-actions">
                <button
                  className="ghost"
                  onClick={() => editor.resolveConflict(false)}
                >
                  Принять серверную версию
                </button>
                <button
                  className="primary"
                  disabled={conflict.latest.archived}
                  onClick={() => editor.resolveConflict(true)}
                >
                  Перенести мои изменения
                </button>
              </div>
              <p className="help">
                Перенос применит только изменённые вами поля к показанной
                серверной версии. Совпадающие изменения будут заменены вашим
                решением; изменение количества снимает утверждение.
              </p>
            </>
          )}
        </section>
      )}
      <DataInfo data={order.calculation} />
      <p className="result-context">
        {order.calculation.warehouse || 'Все склады'} ·{' '}
        {order.calculation.category || 'Все категории'} ·{' '}
        {order.calculation.product_category
          ? `Товарная группа ЭКТ: ${order.calculation.product_category}`
          : 'Все товарные группы'} · сервис{' '}
        {fmt(order.calculation.service_level * 100)} % · проверка каждые{' '}
        {order.calculation.review_period_days} дн
      </p>
      <section className="summary" aria-label="Итоги заказа">
        <div>
          <span>Поставщиков к заказу</span>
          <b>{activeSupplierCount}</b>
          <small>с учётом правок</small>
        </div>
        <div>
          <span>Позиций к заказу</span>
          <b>{fmt(totals.positions)}</b>
          <small>нулевые исключены</small>
        </div>
        <div>
          <span>Количество</span>
          <b className="unit-breakdown">
            {totals.invalid ? 'Проверьте ввод' : fmtUnits(totals.unitsByUnit)}
          </b>
          <small>единицы считаются отдельно</small>
        </div>
        <div className="risk-stat">
          <span>Риск дефицита</span>
          <b>{fmt(totals.high)}</b>
          <small>позиций требуют внимания</small>
        </div>
        <div className="approved-stat">
          <span>Утверждено</span>
          <b>{fmt(totals.approved)}</b>
          <small>{fmtUnits(totals.approvedByUnit)}</small>
        </div>
      </section>
      <div className="order-toolbar">
        <label className="search-field">
          <Icon name="search" />
          <input
            type="search"
            value={search}
            placeholder="Артикул, товар или склад"
            aria-label="Поиск позиций"
            onChange={(event) => {
              setSearch(event.target.value)
              setFocusLine(null)
            }}
          />
        </label>
        <select
          aria-label="Фильтр поставщика"
          value={supplier}
          onChange={(event) => {
            setSupplier(event.target.value)
            setFocusLine(null)
          }}
        >
          <option value="">Все поставщики</option>
          {order.calculation.groups.map((group) => (
            <option key={group.supplier_id} value={group.supplier_id}>
              {group.supplier_name}
            </option>
          ))}
        </select>
        <select
          aria-label="Фильтр срочности"
          value={urgency}
          onChange={(event) => {
            setUrgency(event.target.value)
            setFocusLine(null)
          }}
        >
          <option value="">Любая срочность</option>
          {Object.entries(URGENCY).map(([key, value]) => (
            <option key={key} value={key}>
              {value.label}
            </option>
          ))}
        </select>
      </div>
      <div className="results-caption">
        <span role="status">
          Найдено {visibleCount} из {lines.length} позиций
        </span>
        {(search || supplier || urgency) && (
          <button
            className="link"
            onClick={() => {
              setSearch('')
              setSupplier('')
              setUrgency('')
              setFocusLine(null)
            }}
          >
            Сбросить фильтры
          </button>
        )}
      </div>
      {invalid.length > 0 && (
        <div className="feedback feedback-error" role="status">
          <Icon name="alert" />
          <span>
            Незавершённый ввод: {invalid.length} поз. Другие корректные
            изменения сохраняются.
          </span>
          <button className="ghost" onClick={showInvalid}>
            К первой ошибке
          </button>
        </div>
      )}
      {!visibleCount && (
        <Empty
          title={lines.length ? 'Ничего не найдено' : 'Пополнение не требуется'}
        >
          {lines.length
            ? 'Измените запрос или сбросьте фильтры.'
            : 'На дату расчёта для этих параметров нет позиций к заказу.'}
        </Empty>
      )}
      {groups.map(({ group, filteredLines }) => (
        <SupplierGroup
          key={`${group.supplier_id}:${search}:${supplier}:${urgency}:${focusLine?.token || ''}`}
          group={group}
          filteredLines={filteredLines}
          draft={draft}
          disabled={disabled}
          onQuantity={editor.edit}
          onApprove={approve}
          onExplain={setExplained}
          focusLine={focusLine}
        />
      ))}
      <section className="export-panel" aria-labelledby="export-title">
        <div>
          <span className="step-label">ПРОВЕРКА И ВЫГРУЗКА</span>
          <h2 id="export-title">Готово к работе</h2>
          <p>Excel по сохранённой версии заказа</p>
        </div>
        <div className="export-controls">
          <button
            className="ghost"
            disabled={Boolean(
              operation ||
                saving ||
                dirty ||
                editor.error ||
                conflict ||
                invalid.length ||
                !totals.positions ||
                !active,
            )}
            onClick={() => exportOrder(false)}
          >
            <Icon name="download" />
            Черновик Excel
          </button>
          <button
            className="primary"
            disabled={Boolean(
              operation ||
                saving ||
                dirty ||
                editor.error ||
                conflict ||
                invalid.length ||
                !totals.approved ||
                !active,
            )}
            onClick={() => exportOrder(true)}
          >
            <Icon name="check" />
            Утверждённые · {totals.approved}
          </button>
        </div>
        <p className="export-help">
          Выгружаются все подходящие позиции, включая скрытые поиском и
          страницами. Нулевые количества исключаются. Заказ не отправляется
          поставщику автоматически.
        </p>
        {operation && (
          <Loading>
            {operation === 'export'
              ? 'Сохраняем решения и готовим Excel…'
              : operation === 'approval'
                ? 'Сохраняем утверждение…'
                : 'Обновляем заказ…'}
          </Loading>
        )}
        <Status error={error} success={notice} />
      </section>
      <section className="audit-panel panel">
        <button
          className="audit-toggle"
          aria-expanded={eventsOpen}
          aria-controls="order-events"
          onClick={() => setEventsOpen((value) => !value)}
        >
          <Icon name="clock" />
          История действий<span>{eventsOpen ? '−' : '+'}</span>
        </button>
        {eventsOpen && (
          <div id="order-events">
            {eventsLoading && <Loading />}
            {eventsError && (
              <Status
                error={eventsError}
                onRetry={() => setEventAttempt((value) => value + 1)}
              />
            )}
            {events?.length === 0 && <p>Записей пока нет.</p>}
            <ol className="event-list">
              {events?.map((event) => (
                <li key={event.id}>
                  <span>{EVENT_NAMES[event.action] || 'Изменение заказа'}</span>
                  <strong>{event.actor_name}</strong>
                  <time>{fmtDate(event.created_at, true)}</time>
                  {event.changes && (
                    <details>
                      <summary>Подробности</summary>
                      <EventDetails event={event} lines={lines} />
                    </details>
                  )}
                </li>
              ))}
            </ol>
          </div>
        )}
      </section>
      {explained && (
        <RationaleDialog line={explained} onClose={() => setExplained(null)} />
      )}
    </>
  )
}
