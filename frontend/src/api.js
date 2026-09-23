// Клиент REST API сервиса автозаказов.

async function request(path, options, fallbackMessage) {
  let response
  try {
    response = await fetch(path, options)
  } catch {
    throw new Error('Не удалось связаться с сервисом. Проверьте соединение и повторите попытку.')
  }
  if (!response.ok) {
    let detail
    try {
      detail = (await response.json()).detail
    } catch {
      // HTML, трассировки и другие внутренние ответы сервера не показываем.
    }
    const safeDetail = response.status < 500 && typeof detail === 'string'
      && detail.length <= 500 && /[а-яё]/i.test(detail)
      && !/traceback|exception|\/Users\/|\/home\/|\.py:\d/i.test(detail)
    throw new Error(safeDetail ? detail : fallbackMessage)
  }
  return response
}

export async function fetchMeta() {
  const response = await request('/api/meta', undefined, 'Не удалось загрузить данные. Повторите попытку позже.')
  return response.json()
}

export async function recommend(params) {
  const response = await request('/api/recommend', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  }, 'Не удалось рассчитать заказ. Проверьте параметры и повторите попытку.')
  return response.json()
}

export async function exportExcel(payload) {
  const response = await request('/api/recommend/export', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }, 'Не удалось выгрузить заказ. Повторите попытку или рассчитайте заказ заново.')
  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = payload.approved_only ? 'approved_order.xlsx' : 'order_draft.xlsx'
  document.body.appendChild(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
