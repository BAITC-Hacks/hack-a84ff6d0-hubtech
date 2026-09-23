import assert from 'node:assert/strict'
import test from 'node:test'
import { buildExportPayload, getQuantity, quantityError, safeEktUrl, summarizeLines } from './orderUtils.js'

const line = { line_id: 'supplier1:sku:warehouse1', sku: 'same-sku', recommended_qty: 12, pack_size: 6, min_order_qty: 12, urgency: 'high' }
const otherLine = { ...line, line_id: 'supplier2:sku:warehouse2', recommended_qty: 18, urgency: 'low' }
const result = { calculation_id: 'saved-calculation', groups: [{ lines: [line, otherLine] }] }

test('catalog links only allow HTTPS on the exact ekt.kz host without credentials', () => {
  assert.equal(safeEktUrl('https://ekt.kz/catalog/item-001/'), 'https://ekt.kz/catalog/item-001/')
  for (const value of [undefined, null, '', '/catalog/001', 'http://ekt.kz/001', 'javascript:alert(1)',
    'https://ekt.kz.example.com/001', 'https://example.com/001', 'https://www.ekt.kz/001',
    'https://ekt.kz@evil.example/001', 'https://user:password@ekt.kz/001', 'https://ekt.kz:8443/001']) {
    assert.equal(safeEktUrl(value), null, String(value))
  }
})

test('same SKU in different suppliers or warehouses keeps independent edits and approval', () => {
  const payload = buildExportPayload(result, { [line.line_id]: '24' }, { [otherLine.line_id]: true }, true)
  assert.equal(payload.calculation_id, 'saved-calculation')
  assert.equal(payload.approved_only, true)
  assert.deepEqual(payload.lines, [
    { line_id: line.line_id, quantity: 24, approved: false },
    { line_id: otherLine.line_id, quantity: 18, approved: true },
  ])
})

test('zero edit remains zero, is allowed under MOQ, and is excluded from counts', () => {
  const edits = { [line.line_id]: '0' }
  assert.equal(getQuantity(line, edits), '0')
  assert.equal(quantityError(line, '0'), '')
  assert.equal(buildExportPayload(result, edits, {}, false).lines[0].quantity, 0)
  assert.equal(buildExportPayload(result, edits, { [line.line_id]: true }, false).lines[0].approved, false)
  assert.deepEqual(summarizeLines([line, otherLine], edits, { [line.line_id]: true, [otherLine.line_id]: true }), {
    units: 18, unitsByUnit: { 'ед.': 18 }, positions: 1, high: 1, approved: 1, approvedUnits: 18, approvedByUnit: { 'ед.': 18 }, omitted: 1, invalid: 0,
  })
})

test('blank, nonfinite, negative, under-minimum and wrong-pack quantities block export', () => {
  for (const quantity of ['', ' ', 'bad', NaN, Infinity, -6, 6, 13]) {
    assert.ok(quantityError(line, quantity))
    assert.throws(() => buildExportPayload(result, { [line.line_id]: quantity }, {}, false), /Исправьте/)
  }
})

test('decimal packs tolerate floating point error', () => {
  const fractional = { ...line, pack_size: 0.1, min_order_qty: 0.2 }
  assert.equal(quantityError(fractional, 0.3), '')
  assert.ok(quantityError(fractional, 0.31))
})

test('approved export requires positive approved quantities; empty drafts cannot export', () => {
  assert.throws(() => buildExportPayload(result, {}, {}, true), /Утвердите/)
  const zeroEdits = { [line.line_id]: '0', [otherLine.line_id]: '0' }
  assert.throws(() => buildExportPayload(result, zeroEdits, { [line.line_id]: true }, true), /Утвердите/)
  assert.throws(() => buildExportPayload(result, zeroEdits, {}, false), /Добавьте/)
})

test('totals reflect edited quantities and all rows, independently of visible page', () => {
  const manyLines = Array.from({ length: 125 }, (_, index) => ({ ...line, line_id: `line-${index}` }))
  const edits = { 'line-124': '24' }
  const totals = summarizeLines(manyLines, edits, { 'line-124': true })
  assert.equal(totals.units, 126 * 12)
  assert.equal(totals.positions, 125)
  assert.equal(totals.approved, 1)
  assert.equal(totals.approvedUnits, 24)
})

test('totals keep metres and pieces separate and zero order does not hide deficit risk', () => {
  const metres = { ...line, unit: 'м' }
  const pieces = { ...otherLine, unit: 'шт' }
  const totals = summarizeLines([metres, pieces], {}, { [line.line_id]: true, [otherLine.line_id]: true })
  assert.deepEqual(totals.unitsByUnit, { 'м': 12, 'шт': 18 })
  assert.deepEqual(totals.approvedByUnit, { 'м': 12, 'шт': 18 })
  assert.equal(summarizeLines([metres], { [line.line_id]: '0' }, {}).high, 1)
})
