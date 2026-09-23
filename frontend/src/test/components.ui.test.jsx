import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import SupplierGroup from '../components/SupplierGroup'
import RationaleDialog from '../components/RationaleDialog'
import { line } from './fixtures'

describe('order interactions', () => {
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
