// Same-origin API client. Session secrets stay in HttpOnly cookies; only the CSRF token lives in memory.
let csrfToken = ''
const XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

export class ApiError extends Error {
  constructor(message, status = 0, code = 'http') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)
const isId = (value) => (typeof value === 'string' && value.length > 0) || (Number.isSafeInteger(value) && value > 0)
const isNumber = (value) => typeof value === 'number' && Number.isFinite(value)
const isText = (value) => typeof value === 'string'
const isStrings = (value) => Array.isArray(value) && value.every((item) => typeof item === 'string')
const optional = (value, check) => value === undefined || value === null || check(value)
const isUser = (value) => isObject(value) && isId(value.id) && typeof value.username === 'string'
  && ['admin', 'manager'].includes(value.role) && typeof value.active === 'boolean'
const isSession = (value) => isObject(value) && isUser(value.user) && typeof value.csrf_token === 'string' && value.csrf_token.length > 0
const isDecision = (line) => isObject(line) && isId(line.line_id) && isNumber(line.quantity) && line.quantity >= 0 && typeof line.approved === 'boolean'
const uniqueIds = (items, key) => new Set(items.map((item) => item[key])).size === items.length
const isRationale = (value) => isObject(value) && [
  'avg_daily_demand', 'seasonality_factor', 'trend_factor', 'horizon_days', 'forecast_demand',
  'safety_stock', 'on_hand', 'in_transit', 'lost_demand_uplift', 'excluded_bulk_units', 'excluded_bulk_orders', 'raw_need',
].every((key) => isNumber(value[key])) && optional(value.ignored_in_transit, isNumber)
  && optional(value.stock_as_of, isText)
const isLine = (line) => isObject(line) && typeof line.line_id === 'string' && line.line_id.length > 0
  && ['sku', 'name', 'category', 'unit', 'supplier_id', 'supplier_name', 'explanation'].every((key) => isText(line[key]))
  && optional(line.warehouse, isText) && optional(line.supplier_sku, isText)
  && ['product_category', 'product_subcategory', 'product_brand', 'product_url', 'catalog_fetched_at'].every((key) => optional(line[key], isText))
  && optional(line.product_attributes, (value) => isObject(value) && Object.values(value).every(isText))
  && ['high', 'medium', 'low'].includes(line.urgency) && isNumber(line.days_of_cover)
  && isNumber(line.recommended_qty) && line.recommended_qty >= 0 && isNumber(line.pack_size) && line.pack_size > 0
  && isNumber(line.min_order_qty) && line.min_order_qty >= 0 && isRationale(line.rationale)
  && optional(line.warnings, isStrings)

export function isRecommendation(value) {
  if (!isObject(value) || typeof value.calculation_id !== 'string' || !value.calculation_id
    || !Array.isArray(value.groups) || !isNumber(value.service_level) || !Number.isInteger(value.review_period_days)
    || !optional(value.warnings, isStrings) || !isText(value.generated_at)
    || !optional(value.as_of, isText) || !optional(value.warehouse, isText) || !optional(value.category, isText) || !optional(value.product_category, isText)
    || !isText(value.data_source) || !Number.isSafeInteger(value.sku_count) || value.sku_count < 0) return false
  if (!value.groups.every((group) => isObject(group) && typeof group.supplier_id === 'string'
    && typeof group.supplier_name === 'string' && isNumber(group.lead_time_days)
    && Array.isArray(group.lines) && group.lines.every(isLine))) return false
  return uniqueIds(value.groups, 'supplier_id') && uniqueIds(value.groups.flatMap((group) => group.lines), 'line_id')
}

export function isOrder(value) {
  if (!isObject(value) || !isId(value.id) || !Number.isSafeInteger(value.revision) || value.revision < 1
    || typeof value.title !== 'string' || !isId(value.owner_id) || !isRecommendation(value.calculation)
    || !isText(value.owner_name) || typeof value.archived !== 'boolean' || !isText(value.created_at) || !isText(value.updated_at)
    || !Array.isArray(value.decisions) || !value.decisions.every(isDecision) || !uniqueIds(value.decisions, 'line_id')) return false
  const ids = new Set(value.calculation.groups.flatMap((group) => group.lines.map((line) => line.line_id)))
  return ids.size === value.decisions.length && value.decisions.every((line) => ids.has(line.line_id))
}

const isOrderSummary = (value) => isObject(value) && isId(value.id) && typeof value.title === 'string'
  && typeof value.created_at === 'string' && typeof value.updated_at === 'string'
  && isId(value.owner_id) && isText(value.owner_name) && Number.isSafeInteger(value.revision) && value.revision >= 1
  && Number.isSafeInteger(value.positions) && value.positions >= 0 && Number.isSafeInteger(value.approved) && value.approved >= 0
  && typeof value.archived === 'boolean'
const isJob = (value) => isObject(value) && isId(value.id)
  && ['queued', 'running', 'completed', 'failed', 'cancelled'].includes(value.status)
  && optional(value.order_id, isId) && (value.status !== 'completed' || isId(value.order_id))
  && isText(value.created_at) && isText(value.updated_at) && optional(value.error, isText)
const isMeta = (value) => isObject(value) && isStrings(value.warehouses) && isStrings(value.categories)
  && optional(value.product_categories, isStrings)
  && Array.isArray(value.suppliers) && value.suppliers.every((item) => isObject(item) && typeof item.name === 'string' && isId(item.supplier_id))
  && Number.isSafeInteger(value.sku_count) && value.sku_count >= 0 && typeof value.data_source === 'string'
  && optional(value.as_of, isText) && optional(value.warnings, isStrings)
  && optional(value.defaults, (defaults) => isObject(defaults) && isNumber(defaults.service_level) && Number.isInteger(defaults.review_period_days))
  && optional(value.capabilities, (capabilities) => isObject(capabilities) && typeof capabilities.llm_available === 'boolean')

function validate(value, check) {
  if (!check(value)) throw new ApiError('Сервис вернул некорректный ответ. Повторите попытку позже.', 0, 'invalid_response')
  return value
}

function safeDetail(detail) {
  return typeof detail === 'string' && detail.length <= 500 && /[а-яё]/i.test(detail)
    && !/traceback|exception|\/Users\/|\/home\/|[a-z]:[\\/]|<[^>]+>|\.py:\d/i.test(detail)
}

async function request(path, options = {}, parser) {
  const { method = 'GET', body, signal, timeoutMs = 30_000, headers = {}, fallback = 'Не удалось выполнить действие. Повторите попытку.' } = options
  const attempts = method === 'GET' ? 2 : 1
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const controller = new AbortController()
    let timedOut = false
    const abort = () => controller.abort()
    if (signal?.aborted) throw new ApiError('Ожидание отменено.', 0, 'aborted')
    signal?.addEventListener('abort', abort, { once: true })
    const timeout = setTimeout(() => { timedOut = true; controller.abort() }, timeoutMs)
    try {
      const response = await fetch(path, {
        method, credentials: 'same-origin', signal: controller.signal,
        headers: {
          ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
          ...(method !== 'GET' && csrfToken ? { 'X-CSRF-Token': csrfToken } : {}),
          ...headers,
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      })
      if (!response.ok) {
        let detail
        try { detail = (await response.json()).detail } catch { /* Internal errors are never rendered. */ }
        const standard = {
          401: 'Сессия завершена. Войдите снова, чтобы продолжить работу.',
          403: 'Недостаточно прав для этого действия.',
          409: 'Заказ изменён в другой вкладке. Сравните версии перед сохранением.',
          410: 'Сохранённый расчёт недоступен. Выполните расчёт заново.',
          429: 'Слишком много запросов. Подождите и повторите попытку.',
        }[response.status]
        throw new ApiError(response.status < 500 && safeDetail(detail) ? detail : standard || fallback, response.status)
      }
      return parser ? await parser(response) : undefined
    } catch (error) {
      if (signal?.aborted) throw new ApiError('Ожидание отменено.', 0, 'aborted')
      if (timedOut) throw new ApiError('Сервис отвечает слишком долго. Повторите попытку. Начатый расчёт можно найти в списке задач.', 0, 'timeout')
      const failure = error instanceof ApiError ? error : new ApiError('Не удалось связаться с сервисом. Проверьте соединение и повторите попытку.', 0, 'network')
      if (attempt + 1 < attempts && (failure.code === 'network' || [502, 503, 504].includes(failure.status))) continue
      throw failure
    } finally {
      clearTimeout(timeout)
      signal?.removeEventListener('abort', abort)
    }
  }
}

function json(path, options, check) {
  return request(path, options, async (response) => {
    let value
    try { value = await response.json() } catch { throw new ApiError('Сервис вернул некорректный ответ. Повторите попытку позже.', 0, 'invalid_response') }
    return validate(value, check)
  })
}

export async function getSession(options = {}) {
  const session = await json('/api/auth/session', options, isSession)
  csrfToken = session.csrf_token
  return session
}
export async function login(username, password) {
  const session = await json('/api/auth/login', { method: 'POST', body: { username, password } }, isSession)
  csrfToken = session.csrf_token
  return session
}
export async function logout() {
  await request('/api/auth/logout', { method: 'POST' })
  csrfToken = ''
}
export function fetchMeta(options = {}) {
  return json('/api/meta', { timeoutMs: 120_000, fallback: 'Не удалось загрузить данные. Повторите попытку позже.', ...options }, isMeta)
}
export function recommend(params, { idempotencyKey = crypto.randomUUID(), ...options } = {}) {
  return json('/api/recommend', {
    ...options, method: 'POST', body: params,
    headers: { Prefer: 'respond-async', 'Idempotency-Key': idempotencyKey },
    fallback: 'Не удалось запустить расчёт. Проверьте доступность сервиса и повторите попытку.',
  }, (value) => isObject(value) && isId(value.job_id) && ['queued', 'running', 'completed', 'failed', 'cancelled'].includes(value.status))
}
export const getJob = (id, options = {}) => json(`/api/jobs/${encodeURIComponent(id)}`, options, isJob)
export const cancelJob = (id) => json(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: 'POST' }, isJob)
export const listJobs = (options = {}) => json('/api/jobs', options, (value) => isObject(value) && Array.isArray(value.items) && value.items.every(isJob))
export function listOrders({ offset = 0, limit = 20, archived = false, signal } = {}) {
  const query = new URLSearchParams({ offset: String(offset), limit: String(limit), archived: String(archived) })
  return json(`/api/orders?${query}`, { signal }, (value) => isObject(value) && Array.isArray(value.items)
    && value.items.every(isOrderSummary) && Number.isSafeInteger(value.total) && value.total >= 0)
}
export const getOrder = (id, options = {}) => json(`/api/orders/${encodeURIComponent(id)}`, options, isOrder)
export const saveOrder = (id, revision, lines, options = {}) => json(`/api/orders/${encodeURIComponent(id)}`, { ...options, method: 'PUT', body: { revision, lines } }, isOrder)
export const updateOrder = (id, changes) => json(`/api/orders/${encodeURIComponent(id)}`, { method: 'PATCH', body: changes }, isOrder)
export const fetchOrderEvents = (id, options = {}) => json(`/api/orders/${encodeURIComponent(id)}/events`, options,
  (value) => isObject(value) && Array.isArray(value.items) && value.items.every((event) => isObject(event)
    && isId(event.id) && typeof event.actor_name === 'string' && typeof event.created_at === 'string' && typeof event.action === 'string'))
export const listUsers = () => json('/api/users', {}, (value) => isObject(value) && Array.isArray(value.items) && value.items.every(isUser))
export const createUser = (user) => json('/api/users', { method: 'POST', body: user }, isUser)
export const updateUser = (id, changes) => json(`/api/users/${encodeURIComponent(id)}`, { method: 'PATCH', body: changes }, isUser)

async function download(path, body, filename) {
  const blob = await request(path, { method: 'POST', body, timeoutMs: 60_000, fallback: 'Не удалось выгрузить заказ. Повторите попытку.' }, async (response) => {
    if (response.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase() !== XLSX_MIME)
      throw new ApiError('Сервис вернул файл неизвестного формата. Заказ не скачан.', 0, 'invalid_response')
    const file = await response.blob()
    const signature = new Uint8Array(await file.slice(0, 4).arrayBuffer())
    if (file.size < 4 || signature[0] !== 0x50 || signature[1] !== 0x4b || signature[2] !== 3 || signature[3] !== 4)
      throw new ApiError('Сервис вернул повреждённый Excel-файл. Повторите выгрузку.', 0, 'invalid_response')
    return file
  })
  const url = URL.createObjectURL(blob)
  let link
  try {
    link = document.createElement('a')
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    link.click()
  } catch {
    throw new ApiError('Не удалось начать скачивание. Разрешите загрузку файлов в браузере и повторите попытку.', 0, 'download')
  } finally {
    link?.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
}
export const exportSavedOrder = (id, revision, approvedOnly) => download(`/api/orders/${encodeURIComponent(id)}/export`, { revision, approved_only: approvedOnly }, approvedOnly ? 'approved_order.xlsx' : 'order_draft.xlsx')
export const exportExcel = (payload) => download('/api/recommend/export', payload, payload.approved_only ? 'approved_order.xlsx' : 'order_draft.xlsx')
