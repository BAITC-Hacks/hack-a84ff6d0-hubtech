"""REST-эндпоинты сервиса автозаказов."""
from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, HTTPException, Response

from app.core.export import ExportValidationError, to_excel_bytes
from app.core.recommend import generate_recommendations
from app.core.snapshots import SnapshotNotFound, load_snapshot, save_snapshot
from app.data.adapter import get_data_source
from app.schemas import ExportRequest, RecommendRequest, RecommendationResponse

router = APIRouter()


def _load():
    try:
        return get_data_source().load()
    except (OSError, ValueError):
        logging.getLogger(__name__).exception("Не удалось загрузить источник данных")
        raise HTTPException(
            status_code=422,
            detail="Не удалось прочитать данные. Проверьте наличие выгрузок и настройки источника.",
        ) from None


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/meta")
def meta() -> dict:
    """Справочники для фильтров UI: склады, категории, поставщики."""
    ds = _load()
    catalog = ds.catalog if not ds.catalog.empty else ds.sales
    return {
        "warehouses": sorted(ds.sales["warehouse"].dropna().unique().tolist()),
        "categories": sorted(catalog["category"].dropna().unique().tolist()),
        "suppliers": ds.suppliers.to_dict("records"),
        "sku_count": int(catalog["sku"].nunique()),
        "data_source": ds.source,
        "as_of": ds.as_of or date.today(),
        "warnings": ds.warnings,
        "data_quality": ds.metadata,
    }


@router.post("/recommend", response_model=RecommendationResponse)
def recommend(req: RecommendRequest) -> RecommendationResponse:
    ds = _load()
    response = generate_recommendations(
        ds,
        warehouse=req.warehouse,
        category=req.category,
        service_level=req.service_level,
        review_period_days=req.review_period_days,
        explain=req.explain,
    )
    return save_snapshot(response)


@router.post("/recommend/export")
def recommend_export(req: ExportRequest) -> Response:
    try:
        snapshot = load_snapshot(req.calculation_id)
    except SnapshotNotFound:
        raise HTTPException(
            status_code=410,
            detail="Сохранённый расчёт недоступен. Выполните расчёт заново перед экспортом.",
        ) from None
    try:
        data = to_excel_bytes(snapshot, req)
    except ExportValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    filename = "approved_order.xlsx" if req.approved_only else "order_draft.xlsx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
