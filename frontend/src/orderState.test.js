import assert from 'node:assert/strict'
import test from 'node:test'
import {
  draftFromOrder,
  hasSaveableChanges,
  rebaseDraft,
  saveableLines,
} from './orderState.js'

const lines = [
  { line_id: 'a', recommended_qty: 12, pack_size: 6, min_order_qty: 12 },
  { line_id: 'b', recommended_qty: 6, pack_size: 6, min_order_qty: 6 },
]
const base = {
  id: 'order',
  revision: 1,
  calculation: { groups: [{ lines }] },
  decisions: [
    { line_id: 'a', quantity: 12, approved: false },
    { line_id: 'b', quantity: 6, approved: true },
  ],
}

test('partial input remains local while other valid edits can be persisted', () => {
  const draft = draftFromOrder(base)
  draft.quantities.a = '18'
  draft.quantities.b = ''
  assert.deepEqual(saveableLines(base, draft), [
    { line_id: 'a', quantity: 18, approved: false },
    { line_id: 'b', quantity: 6, approved: false },
  ])
  assert.equal(draft.quantities.b, '')
  assert.equal(hasSaveableChanges(base, draft), true)
})

test('explicit rebase preserves unrelated server changes', () => {
  const draft = draftFromOrder(base)
  draft.quantities.a = '24'
  const latest = {
    ...base,
    revision: 2,
    decisions: [
      base.decisions[0],
      { line_id: 'b', quantity: 18, approved: false },
    ],
  }
  const merged = rebaseDraft(base, latest, draft)
  assert.deepEqual(merged.quantities, { a: '24', b: '18' })
  assert.deepEqual(merged.approvals, { a: false, b: false })
})

test('old local approval never applies to a remotely changed quantity', () => {
  const draft = draftFromOrder(base)
  draft.approvals.a = true
  const latest = {
    ...base,
    revision: 2,
    decisions: [
      { line_id: 'a', quantity: 24, approved: false },
      base.decisions[1],
    ],
  }
  const merged = rebaseDraft(base, latest, draft)
  assert.equal(merged.quantities.a, '24')
  assert.equal(merged.approvals.a, false)
})

test('unchanged decisions are not saved repeatedly', () => {
  assert.equal(hasSaveableChanges(base, draftFromOrder(base)), false)
})
