export const URGENCY = {
  high: { label: 'Срочно', cls: 'u-high' },
  medium: { label: 'Внимание', cls: 'u-medium' },
  low: { label: 'Плановый', cls: 'u-low' },
}

export function fmt(value) {
  return value !== null && value !== undefined && Number.isFinite(Number(value))
    ? Number(value).toLocaleString('ru-RU', { maximumFractionDigits: 2 })
    : '—'
}

export function fmtUnits(units) {
  return (
    Object.entries(units)
      .sort(([a], [b]) => a.localeCompare(b, 'ru'))
      .map(([unit, quantity]) => `${fmt(quantity)} ${unit}`)
      .join(' · ') || '0'
  )
}

export function fmtDate(value, withTime = false) {
  if (!value) return 'не указана'
  const parsed = new Date(value.length === 10 ? `${value}T12:00:00` : value)
  if (Number.isNaN(parsed.getTime())) return 'не указана'
  return new Intl.DateTimeFormat('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    ...(withTime ? { hour: '2-digit', minute: '2-digit' } : {}),
  }).format(parsed)
}

export function safeMessage(error) {
  return error?.message && /[а-яё]/i.test(error.message)
    ? error.message
    : 'Не удалось выполнить действие. Повторите попытку.'
}

export function orderLines(order) {
  return order?.calculation?.groups.flatMap((group) => group.lines) || []
}
