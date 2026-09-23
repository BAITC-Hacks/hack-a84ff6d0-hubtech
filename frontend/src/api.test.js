import assert from 'node:assert/strict'
import test from 'node:test'
import { ApiError, fetchMeta, recommend, getSession, login, getOrder, saveOrder, isOrder, isRecommendation, exportSavedOrder } from './api.js'

const metadata = { warehouses: ['Алматы'], categories: ['Кабель'], suppliers: [{ supplier_id: 'IEK', name: 'IEK' }], sku_count: 1, data_source: 'excel', warnings: [], defaults: { service_level: 0.95, review_period_days: 14 }, capabilities: { llm_available: false } }
const rationale = { avg_daily_demand: 2, seasonality_factor: 1, trend_factor: 1, horizon_days: 14, forecast_demand: 28, safety_stock: 2, on_hand: 0, in_transit: 0, lost_demand_uplift: 0, excluded_bulk_units: 0, excluded_bulk_orders: 0, raw_need: 30 }
const line = { line_id: 'IEK:SKU:WH', sku: '001', name: 'Товар', category: 'Кабель', warehouse: 'Алматы', supplier_id: 'IEK', supplier_name: 'IEK', explanation: 'Расчёт', urgency: 'high', days_of_cover: 0, recommended_qty: 30, pack_size: 5, min_order_qty: 10, rationale, unit: 'м', warnings: [] }
const calculation = { calculation_id: 'snapshot', generated_at: '2026-09-23T10:00:00Z', data_source: 'excel', sku_count: 1, service_level: 0.95, review_period_days: 14, groups: [{ supplier_id: 'IEK', supplier_name: 'IEK', lead_time_days: 21, lines: [line] }], warnings: [] }
const order = { id: 'order', owner_id: 'user', owner_name: 'manager', archived: false, created_at: '2026-09-23T10:00:00Z', updated_at: '2026-09-23T10:00:00Z', title: 'Заказ', revision: 1, calculation, decisions: [{ line_id: line.line_id, quantity: 30, approved: false }] }
const json = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })

for (const [name, value] of [['null', null], ['empty object', {}], ['wrong collections', { ...metadata, warehouses: null }], ['invalid supplier', { ...metadata, suppliers: [null] }]]) {
  test(`metadata rejects ${name} before it can crash rendering`, async (t) => {
    t.mock.method(globalThis, 'fetch', async () => json(value))
    await assert.rejects(fetchMeta, (error) => error instanceof ApiError && error.code === 'invalid_response')
  })
}

test('valid metadata and exact order line set are accepted', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => json(metadata))
  assert.deepEqual(await fetchMeta(), metadata)
  assert.equal(isRecommendation(calculation), true)
  assert.equal(isOrder(order), true)
  assert.equal(isOrder({ ...order, decisions: [] }), false)
  assert.equal(isOrder({ ...order, decisions: [{ ...order.decisions[0], line_id: 'unknown' }] }), false)
  assert.equal(isRecommendation({ ...calculation, groups: [{ ...calculation.groups[0], lines: [line, line] }] }), false)
  assert.equal(isRecommendation({ ...calculation, groups: [{ ...calculation.groups[0], lines: [{ ...line, rationale: null }] }] }), false)
})

test('network failure retries reads once and returns a safe message', async (t) => {
  const fetchMock = t.mock.method(globalThis, 'fetch', async () => { throw new TypeError('internal socket path') })
  await assert.rejects(fetchMeta, /Не удалось связаться с сервисом/)
  assert.equal(fetchMock.mock.callCount(), 2)
})

test('display fields cannot carry objects or malformed server order metadata', () => {
  for (const field of ['owner_name', 'created_at', 'updated_at']) {
    assert.equal(isOrder({ ...order, [field]: {} }), false)
  }
  assert.equal(isOrder({ ...order, archived: 'false' }), false)
  for (const field of ['unit', 'category', 'supplier_sku', 'warehouse']) {
    const changed = { ...line, [field]: {} }
    assert.equal(isRecommendation({ ...calculation, groups: [{ ...calculation.groups[0], lines: [changed] }] }), false)
  }
})

test('POST is never automatically repeated and uses an idempotency key', async (t) => {
  const fetchMock = t.mock.method(globalThis, 'fetch', async (path, options) => {
    assert.equal(path, '/api/recommend')
    assert.equal(options.headers.Prefer, 'respond-async')
    assert.equal(options.headers['Idempotency-Key'], 'stable-key')
    throw new TypeError('Failed to fetch')
  })
  await assert.rejects(() => recommend({ explain: false }, { idempotencyKey: 'stable-key' }), /Не удалось связаться/)
  assert.equal(fetchMock.mock.callCount(), 1)
})

test('safe validation messages and HTTP status are retained', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => json({ detail: 'Выполните расчёт заново.' }, 410))
  await assert.rejects(fetchMeta, (error) => error.status === 410 && error.message === 'Выполните расчёт заново.')
})

test('tracebacks, HTML, paths and server details are never exposed', async (t) => {
  for (const [status, detail] of [[500, 'Внутренняя ошибка БД'], [422, '<html>Ошибка</html>'], [422, 'Ошибка C:\\Users\\name\\service.py'], [422, 'Ошибка C:/Users/name/service.py'], [422, [{ message: 'technical' }]]]) {
    t.mock.method(globalThis, 'fetch', async () => json({ detail }, status))
    await assert.rejects(fetchMeta, { message: 'Не удалось загрузить данные. Повторите попытку позже.' })
    t.mock.restoreAll()
  }
})

test('malformed JSON receives a localized failure without retry', async (t) => {
  const fetchMock = t.mock.method(globalThis, 'fetch', async () => new Response('<html>proxy error</html>'))
  await assert.rejects(fetchMeta, (error) => error.code === 'invalid_response')
  assert.equal(fetchMock.mock.callCount(), 1)
})

test('request timeout aborts the underlying connection', async (t) => {
  let aborted = false
  t.mock.method(globalThis, 'fetch', (path, { signal }) => new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => { aborted = true; reject(new DOMException('Aborted', 'AbortError')) }, { once: true })
  }))
  await assert.rejects(() => fetchMeta({ timeoutMs: 5 }), (error) => error.code === 'timeout')
  assert.equal(aborted, true)
})

test('external cancellation prevents dispatch and is not reported as a server failure', async (t) => {
  const controller = new AbortController()
  controller.abort()
  const fetchMock = t.mock.method(globalThis, 'fetch', async () => json(metadata))
  await assert.rejects(() => fetchMeta({ signal: controller.signal }), (error) => error.code === 'aborted')
  assert.equal(fetchMock.mock.callCount(), 0)
})

test('session carries CSRF into writes while credentials remain cookies', async (t) => {
  const session = { user: { id: 'user', username: 'manager', role: 'manager', active: true }, csrf_token: 'csrf-token' }
  t.mock.method(globalThis, 'fetch', async (path, options) => {
    assert.equal(options.credentials, 'same-origin')
    if (path === '/api/auth/login') return json(session)
    assert.equal(options.headers['X-CSRF-Token'], 'csrf-token')
    assert.deepEqual(JSON.parse(options.body), { revision: 1, lines: order.decisions })
    return json({ ...order, revision: 2 })
  })
  assert.deepEqual(await login('manager', 'test-only-password'), session)
  assert.equal((await saveOrder('order', 1, order.decisions)).revision, 2)
})

test('unauthorized and conflict responses stay distinguishable for recovery UI', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => json({}, 401))
  await assert.rejects(getSession, (error) => error.status === 401)
  t.mock.restoreAll()
  t.mock.method(globalThis, 'fetch', async () => json({}, 409))
  await assert.rejects(() => saveOrder('order', 1, order.decisions), (error) => error.status === 409)
})

test('invalid order body is rejected rather than partially restoring saved decisions', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => json({ ...order, decisions: [{ line_id: line.line_id, quantity: '30', approved: true }] }))
  await assert.rejects(() => getOrder('order'), (error) => error.code === 'invalid_response')
})

for (const [name, type, body] of [['HTML', 'text/html', '<html>error</html>'], ['empty file', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', ''], ['JSON disguised as Excel', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '{"error":true}']]) {
  test(`export rejects ${name} before browser download`, async (t) => {
    t.mock.method(globalThis, 'fetch', async () => new Response(body, { headers: { 'Content-Type': type } }))
    await assert.rejects(() => exportSavedOrder('order', 1, false), (error) => error.code === 'invalid_response')
  })
}

test('download uses the saved revision, correct filename and revokes temporary URL', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  let clicked = false
  let removed = false
  let link
  const oldDocument = Object.getOwnPropertyDescriptor(globalThis, 'document')
  Object.defineProperty(globalThis, 'document', { configurable: true, value: {
    createElement: () => (link = { click() { clicked = true }, remove() { removed = true } }),
    body: { appendChild() {} },
  } })
  t.after(() => { if (oldDocument) Object.defineProperty(globalThis, 'document', oldDocument); else delete globalThis.document })
  t.mock.method(globalThis, 'fetch', async (path, options) => {
    assert.equal(path, '/api/orders/order/export')
    assert.deepEqual(JSON.parse(options.body), { revision: 3, approved_only: true })
    return new Response(new Uint8Array([0x50, 0x4b, 3, 4, 0, 0]), { headers: { 'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' } })
  })
  t.mock.method(URL, 'createObjectURL', () => 'blob:test')
  const revoke = t.mock.method(URL, 'revokeObjectURL', () => {})
  await exportSavedOrder('order', 3, true)
  assert.equal(clicked, true)
  assert.equal(removed, true)
  assert.equal(link.download, 'approved_order.xlsx')
  t.mock.timers.tick(1000)
  assert.equal(revoke.mock.calls[0].arguments[0], 'blob:test')
})
