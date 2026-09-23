import { useEffect, useState } from 'react'
import { listOrders } from '../api'
import { fmtDate, safeMessage } from '../format'
import { Empty, Loading, Status } from '../components/Common'
import Icon from '../Icons'

export default function HistoryPage({
  active,
  user,
  onAuthRequired,
  navigate,
}) {
  const [archived, setArchived] = useState(false)
  const [offset, setOffset] = useState(0)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const limit = 20
  useEffect(() => {
    if (!active) return
    const controller = new AbortController()
    // A different server page must not display the previous page while it loads.
    // oxlint-disable-next-line react/set-state-in-effect
    setLoading(true)
    setError('')
    setData(null)
    listOrders({ offset, limit, archived, signal: controller.signal })
      .then((result) => {
        if (controller.signal.aborted) return
        if (offset > 0 && offset >= result.total)
          setOffset(Math.max(0, Math.ceil(result.total / limit) - 1) * limit)
        else setData(result)
      })
      .catch((failure) => {
        if (controller.signal.aborted) return
        if (failure.status === 401) onAuthRequired()
        else setError(safeMessage(failure))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [active, archived, offset, attempt, onAuthRequired])
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="step-label">ИСТОРИЯ</span>
          <h1>Заказы под контролем</h1>
          <p>
            {user.role === 'admin'
              ? 'Все заказы команды, сохранённые решения и история изменений.'
              : 'Ваши сохранённые расчёты и решения.'}
          </p>
        </div>
        <button className="primary" onClick={() => navigate('#/calculate')}>
          <Icon name="spark" />
          Новый расчёт
        </button>
      </div>
      <div className="history-toolbar">
        <div className="segmented" role="group" aria-label="Состояние заказов">
          <button
            aria-pressed={!archived}
            onClick={() => {
              setArchived(false)
              setOffset(0)
            }}
          >
            В работе
          </button>
          <button
            aria-pressed={archived}
            onClick={() => {
              setArchived(true)
              setOffset(0)
            }}
          >
            Архив
          </button>
        </div>
        <button
          className="ghost light-ghost"
          disabled={loading}
          onClick={() => setAttempt((value) => value + 1)}
        >
          Обновить
        </button>
      </div>
      <Status error={error} onRetry={() => setAttempt((value) => value + 1)} />
      {loading && <Loading>Загружаем заказы…</Loading>}
      {data?.items.length === 0 && (
        <Empty
          title={archived ? 'Архив пока пуст' : 'Сохранённых заказов пока нет'}
        >
          {archived
            ? 'Завершённый заказ можно перенести в архив на его странице.'
            : 'Запустите расчёт. Готовый заказ сохранится автоматически.'}
        </Empty>
      )}
      <div className="history-list">
        {data?.items.map((order) => (
          <article key={order.id} className="history-card">
            <div>
              <span className="order-owner">{order.owner_name}</span>
              <h2>
                <a
                  href={`#/orders/${encodeURIComponent(order.id)}`}
                  onClick={(event) => {
                    event.preventDefault()
                    navigate(event.currentTarget.hash)
                  }}
                >
                  {order.title}
                </a>
              </h2>
              <p>
                Создан {fmtDate(order.created_at, true)} · изменён{' '}
                {fmtDate(order.updated_at, true)}
              </p>
            </div>
            <dl>
              <div>
                <dt>Позиций</dt>
                <dd>{order.positions}</dd>
              </div>
              <div>
                <dt>Утверждено</dt>
                <dd>{order.approved}</dd>
              </div>
              <div>
                <dt>Версия</dt>
                <dd>{order.revision}</dd>
              </div>
            </dl>
            <button
              className="ghost"
              aria-label={`Открыть заказ ${order.title}`}
              onClick={() =>
                navigate(`#/orders/${encodeURIComponent(order.id)}`)
              }
            >
              Открыть
              <Icon name="arrow" />
            </button>
          </article>
        ))}
      </div>
      {data && data.total > limit && (
        <nav
          className="pagination history-pagination"
          aria-label="Страницы истории"
        >
          <span role="status">
            {offset + 1}–{Math.min(offset + limit, data.total)} из {data.total}
          </span>
          <button
            className="ghost"
            disabled={offset === 0 || loading}
            onClick={() => setOffset((value) => Math.max(0, value - limit))}
          >
            Назад
          </button>
          <button
            className="ghost"
            disabled={offset + limit >= data.total || loading}
            onClick={() => setOffset((value) => value + limit)}
          >
            Далее
          </button>
        </nav>
      )}
    </>
  )
}
