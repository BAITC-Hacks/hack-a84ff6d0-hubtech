import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import { getOrder, getSession, listOrders, login, saveOrder } from '../api'
import { line, order, savedOrder } from './fixtures'

vi.mock('../api', () => ({
  getOrder: vi.fn(),
  getSession: vi.fn(),
  listOrders: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
  saveOrder: vi.fn(),
  listUsers: vi.fn(),
  createUser: vi.fn(),
  updateUser: vi.fn(),
  fetchMeta: vi.fn(),
  listJobs: vi.fn(),
  getJob: vi.fn(),
  cancelJob: vi.fn(),
  recommend: vi.fn(),
  updateOrder: vi.fn(),
  fetchOrderEvents: vi.fn(),
  exportSavedOrder: vi.fn(),
}))
const manager = {
  user: { id: 'user-1', username: 'manager', role: 'manager', active: true },
  csrf_token: 'session',
}
beforeEach(() => {
  vi.resetAllMocks()
  window.history.replaceState(null, '', '#/orders/order-1')
  getSession.mockResolvedValue(manager)
  getOrder.mockResolvedValue(structuredClone(order))
  listOrders.mockResolvedValue({ items: [], total: 0 })
  saveOrder.mockRejectedValueOnce(
    Object.assign(new Error('Сессия завершена'), { status: 401 }),
  )
  saveOrder.mockImplementation(async (_id, _revision, rows) =>
    savedOrder(order, rows),
  )
})

async function expireSession() {
  render(<App />)
  const quantity = await screen.findByRole('spinbutton', {
    name: `Количество к заказу ${line.sku}, Алматы`,
  })
  vi.useFakeTimers()
  fireEvent.change(quantity, { target: { value: '24' } })
  await act(() => vi.advanceTimersByTimeAsync(1000))
  expect(
    screen.getByRole('heading', { name: 'Продолжить работу' }),
  ).toBeTruthy()
  vi.useRealTimers()
}

describe('session recovery', () => {
  it('retains the same employee draft without putting decisions in browser storage', async () => {
    const consoleErrors = vi.spyOn(console, 'error').mockImplementation(() => {})
    login.mockResolvedValue(manager)
    await expireSession()
    expect(screen.getByLabelText('Имя пользователя').value).toBe('manager')
    expect(consoleErrors.mock.calls.flat().join(' ')).not.toContain('same key')
    fireEvent.change(screen.getByLabelText('Пароль'), {
      target: { value: 'valid-password' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Войти' }))
    await waitFor(() =>
      expect(
        screen.getByRole('spinbutton', {
          name: `Количество к заказу ${line.sku}, Алматы`,
        }).value,
      ).toBe('24'),
    )
    expect(sessionStorage.length).toBe(0)
    expect(localStorage.length).toBe(0)
  })

  it('drops the previous employee draft when another employee logs in', async () => {
    login.mockResolvedValue({
      ...manager,
      user: { ...manager.user, id: 'user-2', username: 'other' },
    })
    await expireSession()
    fireEvent.change(screen.getByLabelText('Имя пользователя'), {
      target: { value: 'other' },
    })
    fireEvent.change(screen.getByLabelText('Пароль'), {
      target: { value: 'valid-password' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Войти' }))
    await screen.findByRole('heading', { name: 'Заказы под контролем' })
    expect(
      screen.queryByRole('spinbutton', {
        name: `Количество к заказу ${line.sku}, Алматы`,
      }),
    ).toBeNull()
    expect(window.location.hash).toBe('#/orders')
  })

  it('manager cannot open employee administration through a deep link', async () => {
    window.history.replaceState(null, '', '#/users')
    render(<App />)
    await screen.findByRole('heading', { name: 'Недостаточно прав' })
    expect(screen.queryByRole('link', { name: 'Команда' })).toBeNull()
  })
})
