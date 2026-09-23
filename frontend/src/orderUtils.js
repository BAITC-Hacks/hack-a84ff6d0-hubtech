export function getQuantity(line, edits) {
  return edits[line.line_id] ?? line.recommended_qty
}

export function safeEktUrl(value) {
  if (typeof value !== 'string' || !value.trim()) return null
  try {
    const url = new URL(value)
    if (url.protocol !== 'https:' || url.hostname !== 'ekt.kz' || url.port || url.username || url.password) return null
    return url.href
  } catch {
    return null
  }
}

export function quantityError(line, rawQuantity) {
  if (rawQuantity === '' || String(rawQuantity).trim() === '') return 'Введите количество или 0, чтобы исключить позицию.'
  const quantity = Number(rawQuantity)
  if (!Number.isFinite(quantity) || quantity < 0) return 'Количество должно быть числом не меньше 0.'
  if (quantity === 0) return ''
  const minimum = Number(line.min_order_qty) || 0
  const pack = Number(line.pack_size) || 1
  if (quantity + 1e-8 < minimum) return `Минимальный заказ: ${minimum}. Для исключения укажите 0.`
  if (Math.abs(quantity / pack - Math.round(quantity / pack)) > 1e-8) return `Количество должно быть кратно ${pack}.`
  return ''
}

export function summarizeLines(lines, edits, approvals) {
  return lines.reduce((totals, line) => {
    if (line.urgency === 'high') totals.high += 1
    const rawQuantity = getQuantity(line, edits)
    const error = quantityError(line, rawQuantity)
    if (error) {
      totals.invalid += 1
      return totals
    }
    const quantity = Number(rawQuantity)
    totals.units += quantity
    if (quantity > 0) {
      const unit = line.unit || 'ед.'
      totals.unitsByUnit[unit] = (totals.unitsByUnit[unit] || 0) + quantity
      totals.positions += 1
      if (approvals[line.line_id]) {
        totals.approved += 1
        totals.approvedUnits += quantity
        totals.approvedByUnit[unit] = (totals.approvedByUnit[unit] || 0) + quantity
      }
    } else {
      totals.omitted += 1
    }
    return totals
  }, { units: 0, unitsByUnit: {}, positions: 0, high: 0, approved: 0, approvedUnits: 0, approvedByUnit: {}, omitted: 0, invalid: 0 })
}

export function buildExportPayload(result, edits, approvals, approvedOnly) {
  const lines = result.groups.flatMap((group) => group.lines)
  const totals = summarizeLines(lines, edits, approvals)
  if (totals.invalid) throw new Error('Исправьте отмеченные количества перед экспортом.')
  if (approvedOnly && !totals.approved) throw new Error('Утвердите хотя бы одну позицию с количеством больше 0.')
  if (!approvedOnly && !totals.positions) throw new Error('Добавьте хотя бы одну позицию с количеством больше 0.')
  return {
    calculation_id: result.calculation_id,
    approved_only: approvedOnly,
    lines: lines.map((line) => {
      const quantity = Number(getQuantity(line, edits))
      return { line_id: line.line_id, quantity, approved: quantity > 0 && Boolean(approvals[line.line_id]) }
    }),
  }
}
