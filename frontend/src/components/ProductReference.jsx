import { fmtDate } from '../format'
import { safeEktUrl } from '../orderUtils'

const textValue = (value) => (typeof value === 'string' ? value.trim() : '')

export default function ProductReference({ line }) {
  const category = textValue(line.product_category)
  const subcategory = textValue(line.product_subcategory)
  const brand = textValue(line.product_brand)
  const productUrl = safeEktUrl(line.product_url)
  const sourceDate = textValue(line.catalog_fetched_at)
  const attributes =
    line.product_attributes &&
    typeof line.product_attributes === 'object' &&
    !Array.isArray(line.product_attributes)
      ? Object.entries(line.product_attributes).filter(
          ([name, value]) => name.trim() && textValue(value),
        )
      : []
  const hasReference = Boolean(
    category || subcategory || brand || productUrl || attributes.length,
  )
  return (
    <section
      className="product-reference"
      aria-label={`Справочные данные ${line.sku}`}
    >
      <div className="product-reference-heading">
        <h3>Сведения о товаре</h3>
        {productUrl && (
          <a href={productUrl} target="_blank" rel="noopener noreferrer">
            Карточка на ekt.kz
            <span className="sr-only"> — откроется в новой вкладке</span>
          </a>
        )}
      </div>
      <dl className="product-details">
        <div>
          <dt>Категория 1С</dt>
          <dd>{textValue(line.category) || 'Не указана'}</dd>
        </div>
        {textValue(line.supplier_sku) && (
          <div>
            <dt>Артикул поставщика из учётных данных</dt>
            <dd>{line.supplier_sku}</dd>
          </div>
        )}
        {category && (
          <div>
            <dt>Товарная группа ekt.kz</dt>
            <dd>{category}</dd>
          </div>
        )}
        {subcategory && (
          <div>
            <dt>Подгруппа ekt.kz</dt>
            <dd>{subcategory}</dd>
          </div>
        )}
        {brand && (
          <div>
            <dt>Бренд ekt.kz</dt>
            <dd>{brand}</dd>
          </div>
        )}
      </dl>
      {attributes.length > 0 ? (
        <>
          <h4>Характеристики из справочника ekt.kz</h4>
          <dl className="product-details product-attributes">
            {attributes.map(([name, value]) => (
              <div key={name}>
                <dt>{name}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        </>
      ) : (
        <p className="product-reference-note">
          Характеристики для этой позиции пока не загружены.
        </p>
      )}
      <p className="product-reference-note">
        {hasReference
          ? `Сведения справочника ekt.kz · получены: ${fmtDate(sourceDate, true)}.`
          : 'Сведения справочника ekt.kz для этого артикула пока не найдены.'}
      </p>
      {hasReference && (
        <p className="product-reference-note">
          Это описательные сведения. Остатки, единицы измерения и условия
          заказа берутся из учётных данных. Дата карточки не подтверждает
          свежесть складского остатка.
        </p>
      )}
    </section>
  )
}
