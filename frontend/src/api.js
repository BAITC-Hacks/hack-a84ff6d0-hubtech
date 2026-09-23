// Клиент REST API сервиса автозаказов

export async function fetchMeta() {
  const r = await fetch('/api/meta')
  if (!r.ok) throw new Error('meta failed')
  return r.json()
}

export async function recommend(params) {
  const r = await fetch('/api/recommend', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!r.ok) throw new Error('recommend failed')
  return r.json()
}

export async function exportExcel(params) {
  const r = await fetch('/api/recommend/export', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!r.ok) throw new Error('export failed')
  const blob = await r.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = 'order_recommendations.xlsx'
  a.click()
  URL.revokeObjectURL(url)
}
