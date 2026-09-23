export const line = {
  line_id: 'supplier:sku:warehouse',
  sku: '00123',
  supplier_sku: 'SUP-123',
  name: 'Кабель силовой',
  warehouse: 'Алматы',
  category: 'Кабель',
  unit: 'м',
  supplier_id: 'supplier',
  supplier_name: 'Поставщик',
  recommended_qty: 12,
  pack_size: 6,
  min_order_qty: 12,
  urgency: 'high',
  days_of_cover: 3,
  explanation: 'Потребность рассчитана по спросу и доступному запасу.',
  warnings: [],
  rationale: {
    horizon_days: 35,
    avg_daily_demand: 2,
    forecast_demand: 70,
    safety_stock: 12,
    on_hand: 70,
    in_transit: 0,
    ignored_in_transit: 0,
    raw_need: 12,
    lost_demand_uplift: 0,
    seasonality_factor: 1.1,
    trend_factor: 1.2,
    excluded_bulk_orders: 0,
    excluded_bulk_units: 0,
  },
}
export const otherLine = {
  ...line,
  line_id: 'supplier:other:warehouse',
  sku: '00456',
  recommended_qty: 18,
}
export const order = {
  id: 'order-1',
  title: 'Заказ 1',
  revision: 1,
  owner_id: 'user-1',
  owner_name: 'manager',
  archived: false,
  created_at: '2026-09-23T08:00:00Z',
  updated_at: '2026-09-23T08:00:00Z',
  calculation: {
    calculation_id: 'snapshot-1',
    generated_at: '2026-09-23T08:00:00Z',
    as_of: '2026-09-01',
    data_source: 'synthetic',
    warnings: [],
    data_quality: {},
    sku_count: 2,
    warehouse: 'Алматы',
    category: 'Кабель',
    service_level: 0.95,
    review_period_days: 14,
    groups: [
      {
        supplier_id: 'supplier',
        supplier_name: 'Поставщик',
        lead_time_days: 21,
        total_units: 30,
        lines: [line, otherLine],
      },
    ],
  },
  decisions: [
    { line_id: line.line_id, quantity: 12, approved: false },
    { line_id: otherLine.line_id, quantity: 18, approved: false },
  ],
}

export function savedOrder(previous, decisions) {
  const before = new Map(previous.decisions.map((row) => [row.line_id, row]))
  return {
    ...previous,
    revision: previous.revision + 1,
    decisions: decisions.map((row) => ({
      ...row,
      approved:
        row.quantity === before.get(row.line_id).quantity && row.approved,
    })),
  }
}
