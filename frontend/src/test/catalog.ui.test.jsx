import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import DataInfo from '../components/DataInfo'
import ProductReference from '../components/ProductReference'
import RationaleDialog from '../components/RationaleDialog'
import SupplierGroup from '../components/SupplierGroup'
import { line } from './fixtures'

const referenceLine = {
  ...line,
  product_category: 'Кабеленесущие системы',
  product_subcategory: 'Гофрированные трубы',
  product_brand: 'IEK',
  product_url: 'https://www.ekt.kz/catalog/test-product/',
  product_attributes: { Диаметр: '20 мм', Упаковка: '100 м по карточке' },
  catalog_fetched_at: '2026-09-23T10:25:22Z',
}

describe('catalog reference information', () => {
  it('distinguishes accounting data from partial catalog coverage without rendering internal metadata', () => {
    render(
      <DataInfo
        data={{
          data_source: 'excel',
          as_of: '2026-09-23',
          warnings: [],
          data_quality: {
            catalog_enrichment: {
              status: 'partial', matched: 773, total_catalog: 3907,
              unmatched: 3134, conflicts: 27, crawl_quarantined: 200,
              fetched_at: '2026-09-23T10:25:22Z',
              source: '/private/source/path', error: 'internal trace',
            },
          },
        }}
      />,
    )
    const source = screen.getByRole('region', {
      name: 'Источник и ограничения данных',
    })
    expect(within(source).getByText('Excel-выгрузки 1С')).toBeTruthy()
    expect(within(source).getByText('Частичное покрытие')).toBeTruthy()
    expect(within(source).getByText(/Сопоставлено 773 из 3\D?907/)).toBeTruthy()
    expect(within(source).getByText(/Снимок справочника собран: 23\.09\.2026/)).toBeTruthy()
    expect(within(source).getByText(/Неоднозначных совпадений: 27/)).toBeTruthy()
    expect(source.textContent).not.toContain('/private/source/path')
    expect(source.textContent).not.toContain('internal trace')
  })

  it('old snapshots and absent catalogs keep a usable source block', () => {
    const view = render(<DataInfo data={{ data_source: 'excel', warnings: [] }} />)
    expect(screen.getByText('Сведения о покрытии пока недоступны.')).toBeTruthy()
    view.rerender(
      <DataInfo
        data={{
          data_source: 'excel',
          warnings: [],
          data_quality: { catalog_enrichment: { status: 'missing', matched: 0, total_catalog: 3 } },
        }}
      />,
    )
    expect(screen.getByText('Снимок не загружен')).toBeTruthy()
    expect(screen.getByText(/Сопоставлено 0 из 3/)).toBeTruthy()
  })

  it('shows source categories and attributes separately and opens only a safe external product link', () => {
    render(<ProductReference line={referenceLine} />)
    expect(screen.getByText('Категория 1С')).toBeTruthy()
    expect(screen.getByText(line.category)).toBeTruthy()
    expect(screen.getByText(referenceLine.product_category)).toBeTruthy()
    expect(screen.getByText(referenceLine.product_subcategory)).toBeTruthy()
    expect(screen.getByText('20 мм')).toBeTruthy()
    const link = screen.getByRole('link', { name: /Карточка на ekt.kz/ })
    expect(link.href).toBe(referenceLine.product_url)
    expect(link.target).toBe('_blank')
    expect(link.rel).toBe('noopener noreferrer')
    expect(screen.getByText(/Дата карточки не подтверждает/)).toBeTruthy()
  })

  it('renders catalog text as text and ignores unsafe URLs and malformed attributes', () => {
    const view = render(
      <ProductReference
        line={{
          ...referenceLine,
          product_url: 'javascript:alert(1)',
          product_attributes: {
            Описание: '<img src=x onerror=alert(1)>',
            Неверное: { nested: 'value' },
            Пустое: '',
          },
        }}
      />,
    )
    expect(screen.queryByRole('link')).toBeNull()
    expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeTruthy()
    expect(view.container.querySelector('img')).toBeNull()
    expect(screen.queryByText('Неверное')).toBeNull()
    expect(screen.queryByText('Пустое')).toBeNull()
    view.rerender(<ProductReference line={line} />)
    expect(screen.getByText(/для этого артикула пока не найдены/)).toBeTruthy()
  })

  it('adds product descriptions to the rationale without changing quantity or dialog focus behavior', () => {
    const trigger = document.createElement('button')
    document.body.appendChild(trigger)
    trigger.focus()
    const close = vi.fn()
    const view = render(<RationaleDialog line={referenceLine} onClose={close} />)
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText('Потребность до округления')).toBeTruthy()
    expect(within(dialog).getByText('20 мм')).toBeTruthy()
    expect(within(dialog).getByRole('heading', { name: 'Почему 12 м?' })).toBeTruthy()
    fireEvent(dialog, new Event('cancel', { cancelable: true }))
    expect(close).toHaveBeenCalledOnce()
    view.unmount()
    expect(document.activeElement).toBe(trigger)
    trigger.remove()
  })

  it('shows product groups on rows and explicitly labels a missing accounting unit', () => {
    const missingUnit = { ...referenceLine, unit: '' }
    render(
      <SupplierGroup
        group={{ supplier_id: 'supplier', supplier_name: 'Поставщик', lead_time_days: 21, lines: [missingUnit] }}
        filteredLines={[missingUnit]}
        draft={{ quantities: { [line.line_id]: '12' }, approvals: {} }}
        onQuantity={vi.fn()}
        onApprove={vi.fn()}
        onExplain={vi.fn()}
      />,
    )
    expect(screen.getByText('Кабеленесущие системы / Гофрированные трубы')).toBeTruthy()
    expect(screen.getByText(/Алматы · ед\. \(не указана\)/)).toBeTruthy()
    expect(screen.getByRole('spinbutton').value).toBe('12')
  })
})
