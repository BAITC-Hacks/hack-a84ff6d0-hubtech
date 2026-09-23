import assert from 'node:assert/strict'
import test from 'node:test'
import { fmtDate } from './format.js'

test('event timestamps use the company timezone even across the UTC day boundary', () => {
  assert.equal(fmtDate('2026-09-22T21:30:00Z', true), '23.09.2026, 02:30 · Алматы')
})

test('calendar dates remain dates and missing times are never invented', () => {
  assert.equal(fmtDate('2026-09-23', true), '23.09.2026')
  for (const value of [null, {}, 'invalid']) assert.equal(fmtDate(value), 'не указана')
})
