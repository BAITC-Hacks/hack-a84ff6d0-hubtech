import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import SupplierGroup from '../components/SupplierGroup'
import RationaleDialog from '../components/RationaleDialog'
import { line } from './fixtures'

afterEach(() => vi.unstubAllGlobals())

function mockViewport(initialMobile) {
  let mobile = initialMobile
  const listeners = new Set()
  vi.stubGlobal('matchMedia', vi.fn(() => ({
    get matches() { return mobile },
    addEventListener: (_, listener) => listeners.add(listener),
    removeEventListener: (_, listener) => listeners.delete(listener),
  })))
  return (nextMobile) => act(() => {
    mobile = nextMobile
    listeners.forEach((listener) => listener())
  })
}

function manyLinesProps() {
  const rows = Array.from({ length: 25 }, (_, index) => ({
    ...line, line_id: `line-${index}`, sku: `SKU-${index}`,
  }))
  return {
    group: { supplier_id: 'supplier', supplier_name: 'Поставщик', lead_time_days: 21, lines: rows },
    filteredLines: rows,
    draft: { quantities: Object.fromEntries(rows.map((row) => [row.line_id, '12'])), approvals: {} },
    onApprove: vi.fn(), onQuantity: vi.fn(), onExplain: vi.fn(),
  }
}

describe('order interactions', () => {
  it('renders five mobile rows, reacts to viewport changes and keeps supplier totals/actions complete', () => {
    const resize = mockViewport(true)
    const props = manyLinesProps()
    render(<SupplierGroup {...props} />)
    expect(screen.getAllByRole('spinbutton')).toHaveLength(5)
    expect(screen.getByText('1–5 из 25')).toBeTruthy()
    expect(screen.getByText(/300 м.*25 поз\. к заказу/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Утвердить поставщика · 25' }))
    expect(props.onApprove).toHaveBeenCalledWith(props.group.lines.map((row) => row.line_id), true)
    fireEvent.click(screen.getByRole('button', { name: 'Следующая страница: Поставщик' }))
    expect(screen.getByText('6–10 из 25')).toBeTruthy()
    resize(false)
    expect(screen.getAllByRole('spinbutton')).toHaveLength(20)
    resize(true)
    expect(screen.getAllByRole('spinbutton')).toHaveLength(5)
    expect(screen.getByText('6–10 из 25')).toBeTruthy()
  })

  it('opens and focuses the error on the correct mobile page without locking navigation after resizing', async () => {
    const resize = mockViewport(true)
    const props = manyLinesProps()
    props.draft.quantities['line-24'] = ''
    render(<SupplierGroup {...props} focusLine={{ id: 'line-24', token: 'mobile-error' }} />)
    const field = () => screen.getByRole('spinbutton', { name: 'Количество к заказу SKU-24, Алматы' })
    expect(screen.getByText('21–25 из 25')).toBeTruthy()
    await waitFor(() => expect(document.activeElement).toBe(field()))
    resize(false)
    expect(screen.getByText('21–25 из 25')).toBeTruthy()
    await waitFor(() => expect(document.activeElement).toBe(field()))
    fireEvent.click(screen.getByRole('button', { name: 'Предыдущая страница: Поставщик' }))
    expect(screen.getAllByRole('spinbutton')).toHaveLength(20)
    resize(true)
    expect(screen.getByText('1–5 из 25')).toBeTruthy()
    expect(screen.queryByRole('spinbutton', { name: 'Количество к заказу SKU-24, Алматы' })).toBeNull()
  })
  it('keeps accounting and catalogue categories separate and rejects unsafe product links', () => {
    const enriched = {
      ...line,
      category: 'Учётная категория',
      product_category: 'Кабель / Провод',
      product_attributes: { 'Напряжение': '220 В' },
      product_url: 'https://ekt.kz/catalog/example/',
    }
    const view = render(<RationaleDialog line={enriched} onClose={vi.fn()} />)
    expect(screen.getByText('Категория учёта: Учётная категория')).toBeTruthy()
    expect(screen.getByText('Группа EKT: Кабель / Провод')).toBeTruthy()
    expect(screen.getByText('220 В')).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Карточка на ekt.kz ↗' }).getAttribute('rel')).toBe('noopener noreferrer')
    view.rerender(<RationaleDialog line={{ ...enriched, product_url: 'javascript:alert(1)' }} onClose={vi.fn()} />)
    expect(screen.queryByRole('link')).toBeNull()
  })
  it('group approval includes lines hidden on other pages', () => {
    const rows = Array.from({ length: 25 }, (_, index) => ({
      ...line,
      line_id: `line-${index}`,
      sku: `SKU-${index}`,
    }))
    const group = {
      supplier_id: 'supplier',
      supplier_name: 'Поставщик',
      lead_time_days: 21,
      lines: rows,
    }
    const draft = {
      quantities: Object.fromEntries(rows.map((row) => [row.line_id, '12'])),
      approvals: {},
    }
    const approve = vi.fn()
    render(
      <SupplierGroup
        group={group}
        filteredLines={rows}
        draft={draft}
        onApprove={approve}
        onQuantity={vi.fn()}
        onExplain={vi.fn()}
      />,
    )
    expect(screen.getAllByRole('spinbutton')).toHaveLength(20)
    fireEvent.click(
      screen.getByRole('button', { name: 'Утвердить поставщика · 25' }),
    )
    expect(approve).toHaveBeenCalledWith(
      rows.map((row) => row.line_id),
      true,
    )
    fireEvent.click(
      screen.getByRole('button', { name: 'Следующая страница: Поставщик' }),
    )
    expect(screen.getAllByRole('spinbutton')).toHaveLength(5)
  })

  it('navigating to a validation error does not permanently lock pagination', async () => {
    const rows = Array.from({ length: 25 }, (_, index) => ({
      ...line,
      line_id: `line-${index}`,
      sku: `SKU-${index}`,
    }))
    const draft = {
      quantities: Object.fromEntries(
        rows.map((row) => [row.line_id, row.line_id === 'line-24' ? '' : '12']),
      ),
      approvals: {},
    }
    render(
      <SupplierGroup
        group={{
          supplier_id: 'supplier',
          supplier_name: 'Поставщик',
          lead_time_days: 21,
          lines: rows,
        }}
        filteredLines={rows}
        draft={draft}
        onApprove={vi.fn()}
        onQuantity={vi.fn()}
        onExplain={vi.fn()}
        focusLine={{ id: 'line-24', token: 'focus' }}
      />,
    )
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole('spinbutton', {
          name: 'Количество к заказу SKU-24, Алматы',
        }),
      ),
    )
    fireEvent.click(
      screen.getByRole('button', { name: 'Предыдущая страница: Поставщик' }),
    )
    expect(screen.getAllByRole('spinbutton')).toHaveLength(20)
  })

  it('rationale contains numeric evidence and closes on Escape while restoring focus', async () => {
    const button = document.createElement('button')
    document.body.appendChild(button)
    button.focus()
    const close = vi.fn()
    const view = render(<RationaleDialog line={line} onClose={close} />)
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText('Потребность до округления')).toBeTruthy()
    expect(within(dialog).getByText('Средний спрос в день')).toBeTruthy()
    fireEvent(dialog, new Event('cancel', { bubbles: false, cancelable: true }))
    expect(close).toHaveBeenCalledOnce()
    view.unmount()
    expect(document.activeElement).toBe(button)
    button.remove()
  })
})
