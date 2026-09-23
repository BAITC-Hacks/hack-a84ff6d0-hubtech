import { useEffect, useRef, useState } from 'react'
import { cancelJob, fetchMeta, getJob, listJobs, recommend } from '../api'
import { fmt, fmtDate, safeMessage } from '../format'
import { Empty, Loading, Status } from '../components/Common'
import DataInfo from '../components/DataInfo'
import Icon from '../Icons'

const JOB_LABELS = {
  queued: 'В очереди',
  running: 'Рассчитываем',
  completed: 'Готово',
  failed: 'Не удалось рассчитать',
  cancelled: 'Отменён',
}
const TERMINAL = new Set(['completed', 'failed', 'cancelled'])

export default function CalculatePage({
  active,
  onAuthRequired,
  navigate,
  jobId,
}) {
  const [meta, setMeta] = useState(null)
  const [metaLoading, setMetaLoading] = useState(true)
  const [metaError, setMetaError] = useState('')
  const [metaAttempt, setMetaAttempt] = useState(0)
  const [params, setParams] = useState({
    warehouse: '',
    category: '',
    product_category: '',
    service_level: 0.95,
    review_period_days: '14',
    explain: false,
  })
  const [submitting, setSubmitting] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [job, setJob] = useState(null)
  const [jobError, setJobError] = useState('')
  const [pollAttempt, setPollAttempt] = useState(0)
  const [recentJobs, setRecentJobs] = useState([])
  const [jobsError, setJobsError] = useState('')
  const attempt = useRef(null)
  const initialized = useRef(false)

  useEffect(() => {
    if (!active) return
    const controller = new AbortController()
    // Remote metadata reload starts a new request state after retry or reauthentication.
    // oxlint-disable-next-line react/set-state-in-effect
    setMetaLoading(true)
    setMetaError('')
    fetchMeta({ signal: controller.signal })
      .then((value) => {
        if (controller.signal.aborted) return
        setMeta(value)
        const firstLoad = !initialized.current
        setParams((previous) => ({
          ...previous,
          warehouse: value.warehouses.includes(previous.warehouse) ? previous.warehouse : '',
          category: value.categories.includes(previous.category) ? previous.category : '',
          product_category: value.product_categories?.includes(previous.product_category) ? previous.product_category : '',
          ...(firstLoad ? {
            service_level: value.defaults?.service_level || 0.95,
            review_period_days: String(
              value.defaults?.review_period_days || 14,
            ),
          } : {}),
        }))
        initialized.current = true
      })
      .catch((failure) => {
        if (controller.signal.aborted) return
        if (failure.status === 401) onAuthRequired()
        else setMetaError(safeMessage(failure))
      })
      .finally(() => {
        if (!controller.signal.aborted) setMetaLoading(false)
      })
    return () => controller.abort()
  }, [active, metaAttempt, onAuthRequired])

  useEffect(() => {
    if (!active) return
    let current = true
    listJobs()
      .then((data) => {
        if (current) {
          setRecentJobs(data.items)
          setJobsError('')
        }
      })
      .catch((failure) => {
        if (!current) return
        if (failure.status === 401) onAuthRequired()
        else setJobsError(safeMessage(failure))
      })
    return () => {
      current = false
    }
  }, [active, job?.status, jobId, onAuthRequired])

  useEffect(() => {
    // Switching the job deep link must immediately remove the old job's result.
    // oxlint-disable-next-line react/set-state-in-effect
    setJob(null)
    setJobError('')
    if (!jobId || !active) return
    const controller = new AbortController()
    let timer
    async function poll() {
      try {
        const data = await getJob(jobId, { signal: controller.signal })
        if (controller.signal.aborted) return
        setJob(data)
        if (!TERMINAL.has(data.status)) timer = setTimeout(poll, 2000)
      } catch (failure) {
        if (controller.signal.aborted) return
        if (failure.status === 401) onAuthRequired()
        else setJobError(safeMessage(failure))
      }
    }
    void poll()
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [jobId, active, pollAttempt, onAuthRequired])

  const validPeriod =
    params.review_period_days !== '' &&
    Number.isInteger(Number(params.review_period_days)) &&
    Number(params.review_period_days) >= 1 &&
    Number(params.review_period_days) <= 120
  const pending = jobId && (!job || !TERMINAL.has(job.status))
  const disabled = submitting || Boolean(pending) || metaLoading || Boolean(metaError) || !meta || !active

  async function start() {
    if (disabled || !validPeriod) return
    setSubmitting(true)
    setJobError('')
    const payload = {
      ...params,
      warehouse: params.warehouse || null,
      category: params.category || null,
      product_category: params.product_category || null,
      service_level: Number(params.service_level),
      review_period_days: Number(params.review_period_days),
      explain: Boolean(params.explain && meta.capabilities?.llm_available),
    }
    const serialized = JSON.stringify(payload)
    if (attempt.current?.params !== serialized)
      attempt.current = { params: serialized, key: crypto.randomUUID() }
    try {
      const created = await recommend(payload, {
        idempotencyKey: attempt.current.key,
      })
      attempt.current = null
      navigate(`#/calculate?job=${encodeURIComponent(created.job_id)}`, {
        skipGuard: true,
      })
    } catch (failure) {
      if (failure.status === 401) onAuthRequired()
      else setJobError(safeMessage(failure))
    } finally {
      setSubmitting(false)
    }
  }

  async function cancel() {
    setCancelling(true)
    setJobError('')
    try {
      const cancelled = await cancelJob(jobId)
      setJob(cancelled)
      setPollAttempt((value) => value + 1)
    } catch (failure) {
      if (failure.status === 401) onAuthRequired()
      else setJobError(safeMessage(failure))
    } finally {
      setCancelling(false)
    }
  }

  function resetJob() {
    attempt.current = null
    navigate('#/calculate', { skipGuard: true })
  }

  function change(key, value) {
    setParams((previous) => ({ ...previous, [key]: value }))
  }

  return (
    <>
      <div className="page-heading">
        <div>
          <span className="step-label">НОВЫЙ ЗАКАЗ</span>
          <h1>
            Нужные товары.
            <br />
            <span>В нужный момент.</span>
          </h1>
          <p>Рассчитайте потребность, проверьте позиции и сохраните решение.</p>
        </div>
        <span className="handwritten">всё под контролем</span>
      </div>
      {metaLoading && (
        <Loading>Загружаем справочники и проверяем данные…</Loading>
      )}
      <Status
        error={metaError}
        onRetry={() => setMetaAttempt((value) => value + 1)}
      />
      {meta && (
        <>
          <div className="catalog-overview">
            <span>
              <strong>{fmt(meta.sku_count)}</strong> артикулов
            </span>
            <span>
              <strong>{meta.suppliers.length}</strong> поставщиков
            </span>
            <span>
              Данные на <strong>{fmtDate(meta.as_of)}</strong>
            </span>
          </div>
          <DataInfo data={meta} />
        </>
      )}
      <section className="parameters-panel" aria-labelledby="parameters-title">
        <div className="section-heading">
          <div>
            <span className="step-label">01 / ПАРАМЕТРЫ</span>
            <h2 id="parameters-title">Настройте расчёт</h2>
          </div>
        </div>
        <form
          onSubmit={(event) => {
            event.preventDefault()
            void start()
          }}
        >
          <fieldset className="controls" disabled={disabled}>
            <legend className="sr-only">Параметры расчёта</legend>
            <label>
              Склад
              <select
                value={params.warehouse}
                onChange={(event) => change('warehouse', event.target.value)}
              >
                <option value="">Все склады</option>
                {meta?.warehouses.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Категория 1С
              <select
                value={params.category}
                onChange={(event) => change('category', event.target.value)}
              >
                <option value="">Все категории</option>
                {meta?.categories.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Товарная группа ЭКТ
              <select
                value={params.product_category}
                disabled={!meta?.product_categories?.length}
                aria-describedby="product-category-help"
                onChange={(event) => change('product_category', event.target.value)}
              >
                <option value="">Все товарные группы</option>
                {(meta?.product_categories || []).map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Уровень сервиса
              <select
                value={params.service_level}
                onChange={(event) =>
                  change('service_level', Number(event.target.value))
                }
              >
                {[0.9, 0.95, 0.98, 0.99].map((value) => (
                  <option key={value} value={value}>
                    {fmt(value * 100)} %
                  </option>
                ))}
              </select>
            </label>
            <label>
              Период проверки, дней
              <input
                type="number"
                inputMode="numeric"
                min="1"
                max="120"
                step="1"
                value={params.review_period_days}
                aria-invalid={!validPeriod}
                aria-describedby={!validPeriod ? 'period-error' : 'period-help'}
                onChange={(event) =>
                  change('review_period_days', event.target.value)
                }
              />
            </label>
            <div className="controls-bottom">
              <label className="toggle-label">
                <input
                  type="checkbox"
                  role="switch"
                  checked={params.explain}
                  disabled={!meta?.capabilities?.llm_available}
                  onChange={(event) => change('explain', event.target.checked)}
                />
                <span className="toggle-track" />
                <span>
                  AI-обоснования
                  <small>
                    {meta?.capabilities?.llm_available
                      ? 'Числовая раскладка доступна всегда'
                      : 'LLM не подключён. Доступны шаблонные обоснования и числовая раскладка.'}
                  </small>
                </span>
              </label>
              <button
                className="primary calculate-button"
                disabled={disabled || !validPeriod}
              >
                {submitting ? (
                  <span className="spinner" />
                ) : (
                  <Icon name="spark" />
                )}
                Рассчитать заказ
              </button>
            </div>
          </fieldset>
        </form>
        <p id="period-help" className="help">
          Период проверки — интервал между пересмотрами запаса; срок поставки
          учитывается дополнительно.
        </p>
        <p id="product-category-help" className="help">
          {meta?.product_categories?.length
            ? 'Товарная группа ЭКТ — дополнительный фильтр, отдельный от категории 1С. При его выборе в расчёт попадут только товары, сопоставленные со справочником. Без фильтра учитывается весь выбранный ассортимент.'
            : 'Товарные группы ЭКТ пока недоступны. Расчёт работает по исходным данным без этого дополнительного фильтра.'}
        </p>
        {!validPeriod && (
          <p className="validation-message" id="period-error">
            Укажите целое число дней от 1 до 120.
          </p>
        )}
        {!jobId && <Status error={jobError} />}
      </section>
      {jobId && (
        <section
          className="job-panel panel"
          aria-labelledby="job-title"
          aria-busy={Boolean(pending && !jobError)}
        >
          <div className="section-heading">
            <div>
              <span className="step-label">02 / РАСЧЁТ</span>
              <h2 id="job-title">
                {job ? JOB_LABELS[job.status] : 'Проверяем состояние расчёта'}
              </h2>
            </div>
            {pending && <span className="spinner" />}
          </div>
          <p role="status">
            {job?.status === 'queued'
              ? 'Задача ожидает свободного обработчика.'
              : job?.status === 'running'
                ? 'Учитываем спрос, сезонность, остатки и товары в пути. Большой каталог может обрабатываться несколько минут.'
                : job?.status === 'completed'
                  ? 'Заказ сохранён на сервере и доступен в истории.'
                  : job?.status === 'cancelled'
                    ? 'Расчёт отменён. Заказ не создан.'
                    : job?.status === 'failed'
                      ? 'Заказ не создан. Можно запустить новый расчёт.'
                      : 'Загружаем статус…'}
          </p>
          {pending && (
            <>
              <p className="help">
                Можно перейти в историю и вернуться по этой ссылке. Закрытие
                страницы не останавливает серверный расчёт.
              </p>
              <div className="skeleton-lines" aria-hidden="true">
                <i />
                <i />
                <i />
              </div>
            </>
          )}
          <Status
            error={jobError || (job?.status === 'failed' ? job.error : '')}
            onRetry={
              jobError ? () => setPollAttempt((value) => value + 1) : undefined
            }
          />
          <div className="inline-actions">
            {pending && (
              <button className="ghost" disabled={cancelling} onClick={cancel}>
                {cancelling ? 'Отменяем…' : 'Отменить расчёт'}
              </button>
            )}
            {job?.status === 'completed' && job.order_id && (
              <button
                className="primary"
                onClick={() =>
                  navigate(`#/orders/${encodeURIComponent(job.order_id)}`)
                }
              >
                Открыть заказ
                <Icon name="arrow" />
              </button>
            )}
            {job && TERMINAL.has(job.status) && (
              <button className="ghost" onClick={resetJob}>
                Новый расчёт
              </button>
            )}
          </div>
        </section>
      )}
      <section className="recent-jobs" aria-labelledby="recent-title">
        <div className="section-heading inverse">
          <h2 id="recent-title">Последние расчёты</h2>
        </div>
        <Status error={jobsError} />
        {!recentJobs.length && !jobsError && (
          <Empty title="Начните с первого расчёта">
            Каждый готовый расчёт становится сохранённым заказом.
          </Empty>
        )}
        <div className="job-list">
          {recentJobs.slice(0, 10).map((item) => (
            <button
              key={item.id}
              className="job-link"
              onClick={() =>
                navigate(
                  item.status === 'completed' && item.order_id
                    ? `#/orders/${encodeURIComponent(item.order_id)}`
                    : `#/calculate?job=${encodeURIComponent(item.id)}`,
                )
              }
            >
              <span>
                <strong>{JOB_LABELS[item.status]}</strong>
                <time>{fmtDate(item.created_at, true)}</time>
              </span>
              <Icon name="arrow" />
            </button>
          ))}
        </div>
      </section>
    </>
  )
}
