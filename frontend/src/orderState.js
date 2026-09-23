import { quantityError } from './orderUtils.js'
import { orderLines } from './format.js'

export function decisionMap(order) {
  const saved = new Map(
    (order.decisions || []).map((row) => [row.line_id, row]),
  )
  return Object.fromEntries(
    orderLines(order).map((line) => [
      line.line_id,
      saved.get(line.line_id) || {
        line_id: line.line_id,
        quantity: line.recommended_qty,
        approved: false,
      },
    ]),
  )
}

export function draftFromOrder(order) {
  const rows = Object.values(decisionMap(order))
  return {
    quantities: Object.fromEntries(
      rows.map((row) => [row.line_id, String(row.quantity)]),
    ),
    approvals: Object.fromEntries(
      rows.map((row) => [row.line_id, Boolean(row.approved)]),
    ),
  }
}

// An incomplete input stays on the page. Other valid decisions may still save.
export function saveableLines(order, draft) {
  const saved = decisionMap(order)
  return orderLines(order).map((line) => {
    const raw = draft.quantities[line.line_id]
    const invalid = Boolean(quantityError(line, raw))
    const quantity = invalid ? saved[line.line_id].quantity : Number(raw)
    return {
      line_id: line.line_id,
      quantity,
      approved:
        !invalid && quantity > 0 && Boolean(draft.approvals[line.line_id]),
    }
  })
}

export function hasSaveableChanges(order, draft) {
  const saved = decisionMap(order)
  return saveableLines(order, draft).some((row) => {
    const before = saved[row.line_id]
    return row.quantity !== before.quantity || row.approved !== before.approved
  })
}

export function invalidLines(order, draft) {
  return orderLines(order).filter((line) =>
    quantityError(line, draft.quantities[line.line_id]),
  )
}

// Explicit conflict resolution reapplies only locally changed fields.
export function rebaseDraft(base, latest, local) {
  const before = decisionMap(base)
  const remote = decisionMap(latest)
  const result = draftFromOrder(latest)
  for (const line of orderLines(latest)) {
    const id = line.line_id
    if (!before[id] || !(id in local.quantities)) continue
    if (String(local.quantities[id]) !== String(before[id].quantity)) {
      result.quantities[id] = local.quantities[id]
      result.approvals[id] = false
    } else if (Boolean(local.approvals[id]) !== before[id].approved) {
      result.approvals[id] =
        remote[id].quantity === before[id].quantity &&
        Boolean(local.approvals[id])
    }
  }
  return result
}

export function conflictDifferences(base, latest, local) {
  const previous = decisionMap(base)
  const current = decisionMap(latest)
  return orderLines(latest).flatMap((line) => {
    const id = line.line_id
    if (!previous[id] || !(id in local.quantities)) return []
    const localChanged =
      String(local.quantities[id]) !== String(previous[id].quantity) ||
      Boolean(local.approvals[id]) !== previous[id].approved
    const remoteChanged =
      current[id].quantity !== previous[id].quantity ||
      current[id].approved !== previous[id].approved
    return localChanged || remoteChanged
      ? [
          {
            line,
            local: {
              quantity: local.quantities[id],
              approved: local.approvals[id],
            },
            server: current[id],
            localChanged,
            remoteChanged,
          },
        ]
      : []
  })
}
