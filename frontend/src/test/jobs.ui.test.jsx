import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import CalculatePage from '../pages/CalculatePage'
import { cancelJob, fetchMeta, getJob, listJobs, recommend } from '../api'

vi.mock('../api', () => ({
  cancelJob: vi.fn(),
  fetchMeta: vi.fn(),
  getJob: vi.fn(),
  listJobs: vi.fn(),
  recommend: vi.fn(),
}))
const meta = {
  warehouses: ['Алматы'],
  categories: ['Кабель'],
  suppliers: [{ supplier_id: 'supplier', name: 'Поставщик' }],
  sku_count: 200,
  data_source: 'synthetic',
  as_of: '2026-09-01',
  warnings: [],
  defaults: { service_level: 0.98, review_period_days: 21 },
  capabilities: { llm_available: false },
}
const queued = {
  id: 'job-1',
  status: 'queued',
  created_at: '2026-09-23T08:00:00Z',
  order_id: null,
  error: null,
}
beforeEach(() => {
  vi.resetAllMocks()
  fetchMeta.mockResolvedValue(meta)
  listJobs.mockResolvedValue({ items: [] })
  getJob.mockResolvedValue(queued)
})

describe('calculation jobs', () => {
  it('clears unavailable filters after reauthentication and waits for refreshed metadata', async () => {
    fetchMeta.mockResolvedValueOnce({ ...meta, product_categories: ['Трубы'] })
    recommend.mockResolvedValue({ job_id: 'job-1', status: 'queued' })
    const props = { onAuthRequired: vi.fn(), navigate: vi.fn() }
    const { rerender } = render(<CalculatePage active {...props} />)
    const submit = await screen.findByRole('button', { name: 'Рассчитать заказ' })
    await waitFor(() => expect(submit.disabled).toBe(false))
    fireEvent.change(screen.getByLabelText('Склад'), { target: { value: 'Алматы' } })
    fireEvent.change(screen.getByLabelText('Категория 1С'), { target: { value: 'Кабель' } })
    fireEvent.change(screen.getByLabelText('Товарная группа ЭКТ'), { target: { value: 'Трубы' } })
    let resolveMetadata
    fetchMeta.mockReturnValueOnce(new Promise((resolve) => { resolveMetadata = resolve }))
    rerender(<CalculatePage active={false} {...props} />)
    rerender(<CalculatePage active {...props} />)
    await waitFor(() => expect(submit.disabled).toBe(true))
    await act(async () => resolveMetadata({ ...meta, warehouses: ['Астана'], product_categories: [] }))
    await waitFor(() => expect(submit.disabled).toBe(false))
    expect(screen.getByLabelText('Склад').value).toBe('')
    expect(screen.getByLabelText('Товарная группа ЭКТ').value).toBe('')
    expect(screen.getByLabelText('Товарная группа ЭКТ').disabled).toBe(true)
    expect(screen.getByLabelText('Категория 1С').value).toBe('Кабель')
    fireEvent.click(submit)
    await waitFor(() => expect(recommend).toHaveBeenCalledOnce())
    expect(recommend.mock.calls[0][0]).toMatchObject({ warehouse: null, category: 'Кабель', product_category: null })
  })

  it('keeps warehouse and two category filters separate and rotates the key for changed parameters', async () => {
    fetchMeta.mockResolvedValue({ ...meta, warehouses: [' Алматы '], product_categories: ['Трубы', 'Свет'] })
    recommend.mockRejectedValue(new Error('Не удалось связаться с сервисом'))
    render(<CalculatePage active onAuthRequired={vi.fn()} navigate={vi.fn()} />)
    const submit = await screen.findByRole('button', { name: 'Рассчитать заказ' })
    await waitFor(() => expect(submit.disabled).toBe(false))
    fireEvent.change(screen.getByLabelText('Склад'), { target: { value: ' Алматы ' } })
    fireEvent.change(screen.getByLabelText('Категория 1С'), { target: { value: 'Кабель' } })
    fireEvent.change(screen.getByLabelText('Товарная группа ЭКТ'), { target: { value: 'Трубы' } })
    expect(screen.getByText(/в расчёт попадут только товары, сопоставленные/)).toBeTruthy()
    fireEvent.click(submit)
    await screen.findByRole('alert')
    expect(recommend.mock.calls[0][0]).toMatchObject({ warehouse: ' Алматы ', category: 'Кабель', product_category: 'Трубы' })
    fireEvent.change(screen.getByLabelText('Товарная группа ЭКТ'), { target: { value: 'Свет' } })
    fireEvent.click(submit)
    await waitFor(() => expect(recommend).toHaveBeenCalledTimes(2))
    expect(recommend.mock.calls[1][1].idempotencyKey).not.toBe(recommend.mock.calls[0][1].idempotencyKey)
  })

  it('supports old metadata or an unavailable catalog without blocking normal calculations', async () => {
    recommend.mockResolvedValue({ job_id: 'job-1', status: 'queued' })
    render(<CalculatePage active onAuthRequired={vi.fn()} navigate={vi.fn()} />)
    const submit = await screen.findByRole('button', { name: 'Рассчитать заказ' })
    await waitFor(() => expect(submit.disabled).toBe(false))
    expect(screen.getByLabelText('Товарная группа ЭКТ').disabled).toBe(true)
    expect(screen.getByText(/Товарные группы ЭКТ пока недоступны/)).toBeTruthy()
    fireEvent.click(submit)
    await waitFor(() => expect(recommend).toHaveBeenCalledOnce())
    expect(recommend.mock.calls[0][0]).toMatchObject({ category: null, product_category: null })
  })

  it('uses server defaults, disables absent LLM, and reuses one idempotency key after uncertain creation', async () => {
    const navigate = vi.fn()
    recommend
      .mockRejectedValueOnce(new Error('Не удалось связаться с сервисом'))
      .mockResolvedValue({ job_id: 'job-1', status: 'queued' })
    render(
      <CalculatePage active onAuthRequired={vi.fn()} navigate={navigate} />,
    )
    const submit = await screen.findByRole('button', {
      name: 'Рассчитать заказ',
    })
    await waitFor(() => expect(submit.disabled).toBe(false))
    expect(screen.getByLabelText('Период проверки, дней').value).toBe('21')
    expect(screen.getByRole('switch').disabled).toBe(true)
    fireEvent.click(submit)
    await screen.findByRole('alert')
    fireEvent.click(submit)
    await waitFor(() => expect(navigate).toHaveBeenCalled())
    expect(recommend).toHaveBeenCalledTimes(2)
    expect(recommend.mock.calls[0][1].idempotencyKey).toBe(
      recommend.mock.calls[1][1].idempotencyKey,
    )
    expect(recommend.mock.calls[1][0]).toMatchObject({
      service_level: 0.98,
      review_period_days: 21,
      explain: false,
    })
  })

  it('polls queued and running jobs but stops after completion', async () => {
    getJob
      .mockResolvedValueOnce(queued)
      .mockResolvedValueOnce({ ...queued, status: 'running' })
      .mockResolvedValue({
        ...queued,
        status: 'completed',
        order_id: 'order-1',
      })
    const navigate = vi.fn()
    vi.useFakeTimers()
    await act(async () => {
      render(
        <CalculatePage
          active
          onAuthRequired={vi.fn()}
          navigate={navigate}
          jobId="job-1"
        />,
      )
    })
    expect(screen.getByRole('heading', { name: 'В очереди' })).toBeTruthy()
    await act(() => vi.advanceTimersByTimeAsync(2000))
    expect(screen.getByRole('heading', { name: 'Рассчитываем' })).toBeTruthy()
    await act(() => vi.advanceTimersByTimeAsync(2000))
    expect(screen.getByRole('heading', { name: 'Готово' })).toBeTruthy()
    expect(getJob).toHaveBeenCalledTimes(3)
    fireEvent.click(screen.getByRole('button', { name: 'Открыть заказ' }))
    expect(navigate).toHaveBeenCalledWith('#/orders/order-1')
    await act(() => vi.advanceTimersByTimeAsync(10_000))
    expect(getJob).toHaveBeenCalledTimes(3)
  })

  it('cancels a server job and shows the terminal state', async () => {
    cancelJob.mockResolvedValue({ ...queued, status: 'cancelled' })
    render(
      <CalculatePage
        active
        onAuthRequired={vi.fn()}
        navigate={vi.fn()}
        jobId="job-1"
      />,
    )
    await screen.findByRole('heading', { name: 'В очереди' })
    getJob.mockResolvedValue({ ...queued, status: 'cancelled' })
    fireEvent.click(screen.getByRole('button', { name: 'Отменить расчёт' }))
    await screen.findByRole('heading', { name: 'Отменён' })
    expect(cancelJob).toHaveBeenCalledWith('job-1')
    expect(screen.queryByRole('button', { name: 'Отменить расчёт' })).toBeNull()
  })
})
