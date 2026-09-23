import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { getOrder, saveOrder } from '../api'
import { useOrderEditor } from '../hooks/useOrderEditor'
import { line, order, otherLine, savedOrder } from './fixtures'

vi.mock('../api', () => ({ getOrder: vi.fn(), saveOrder: vi.fn() }))

let server
beforeEach(() => {
  server = structuredClone(order)
  getOrder.mockReset().mockImplementation(async () => structuredClone(server))
  saveOrder.mockReset().mockImplementation(async (_id, revision, decisions) => {
    expect(revision).toBe(server.revision)
    server = savedOrder(server, decisions)
    return structuredClone(server)
  })
})

async function editor() {
  const onAuth = vi.fn()
  const hook = renderHook(
    ({ active }) => useOrderEditor(order.id, active, onAuth),
    { initialProps: { active: true } },
  )
  await waitFor(() => expect(hook.result.current.loading).toBe(false))
  return { ...hook, onAuth }
}

describe('durable order editor', () => {
  it('debounces rapid edits for one second and persists the last value', async () => {
    const hook = await editor()
    vi.useFakeTimers()
    act(() => {
      hook.result.current.edit(line.line_id, '18')
      hook.result.current.edit(line.line_id, '24')
    })
    await act(() => vi.advanceTimersByTimeAsync(999))
    expect(saveOrder).not.toHaveBeenCalled()
    await act(() => vi.advanceTimersByTimeAsync(1))
    expect(saveOrder).toHaveBeenCalledTimes(1)
    expect(server.decisions[0].quantity).toBe(24)
    expect(hook.result.current.dirty).toBe(false)
  })

  it('serializes a newer edit behind an in-flight save with the new revision', async () => {
    const hook = await editor()
    let release
    saveOrder.mockImplementationOnce(
      (_id, revision, rows) =>
        new Promise((resolve) => {
          release = () => {
            server = savedOrder(server, rows)
            resolve(structuredClone(server))
          }
        }),
    )
    act(() => hook.result.current.edit(line.line_id, '18'))
    let pending
    act(() => {
      pending = hook.result.current.flush()
    })
    act(() => hook.result.current.edit(line.line_id, '24'))
    expect(saveOrder).toHaveBeenCalledTimes(1)
    await act(async () => {
      release()
      await pending
    })
    expect(saveOrder).toHaveBeenCalledTimes(2)
    expect(saveOrder.mock.calls[1][1]).toBe(2)
    expect(server.decisions[0].quantity).toBe(24)
  })

  it('keeps invalid input local and saves a valid edit on another line', async () => {
    const hook = await editor()
    act(() => {
      hook.result.current.edit(line.line_id, '')
      hook.result.current.edit(otherLine.line_id, '24')
    })
    await act(() => hook.result.current.flush())
    expect(server.decisions[0].quantity).toBe(12)
    expect(server.decisions[1].quantity).toBe(24)
    expect(hook.result.current.draft.quantities[line.line_id]).toBe('')
    expect(hook.result.current.invalid).toHaveLength(1)
  })

  it('saves changed quantity before the separate approval request', async () => {
    const hook = await editor()
    act(() => hook.result.current.edit(line.line_id, '24'))
    await act(() => hook.result.current.approve([line.line_id], true))
    expect(saveOrder).toHaveBeenCalledTimes(2)
    expect(saveOrder.mock.calls[0][2][0]).toEqual({
      line_id: line.line_id,
      quantity: 24,
      approved: false,
    })
    expect(saveOrder.mock.calls[1][2][0]).toEqual({
      line_id: line.line_id,
      quantity: 24,
      approved: true,
    })
    expect(server.decisions[0].approved).toBe(true)
  })

  it('freezes on conflict until the user explicitly chooses a version', async () => {
    const hook = await editor()
    server = {
      ...server,
      revision: 2,
      decisions: [
        server.decisions[0],
        { ...server.decisions[1], quantity: 30 },
      ],
    }
    saveOrder.mockRejectedValueOnce(
      Object.assign(new Error('Заказ изменён'), { status: 409 }),
    )
    act(() => hook.result.current.edit(line.line_id, '24'))
    await act(() => hook.result.current.flush())
    expect(hook.result.current.conflict.latest.revision).toBe(2)
    expect(hook.result.current.draft.quantities[line.line_id]).toBe('24')
    await act(() => hook.result.current.flush())
    expect(saveOrder).toHaveBeenCalledTimes(1)
    act(() => hook.result.current.resolveConflict(true))
    await act(() => hook.result.current.flush())
    expect(server.decisions.map((row) => row.quantity)).toEqual([24, 30])
  })

  it('retains local edits through session expiration and saves after reauthentication', async () => {
    const hook = await editor()
    saveOrder.mockRejectedValueOnce(
      Object.assign(new Error('Сессия завершена'), { status: 401 }),
    )
    act(() => hook.result.current.edit(line.line_id, '24'))
    await act(() => hook.result.current.flush())
    expect(hook.onAuth).toHaveBeenCalledOnce()
    hook.rerender({ active: false })
    expect(hook.result.current.draft.quantities[line.line_id]).toBe('24')
    hook.rerender({ active: true })
    await act(() => hook.result.current.flush())
    expect(server.decisions[0].quantity).toBe(24)
  })

  it('network save failures remain local and do not retry indefinitely', async () => {
    const hook = await editor()
    saveOrder.mockRejectedValue(
      Object.assign(new Error('Не удалось связаться с сервисом'), {
        status: 0,
      }),
    )
    act(() => hook.result.current.edit(line.line_id, '24'))
    await act(async () => {
      await Promise.all([
        hook.result.current.flush(),
        hook.result.current.flush(),
      ])
    })
    expect(saveOrder).toHaveBeenCalledOnce()
    expect(hook.result.current.dirty).toBe(true)
    expect(hook.result.current.error).toContain('Не удалось')
  })
})
