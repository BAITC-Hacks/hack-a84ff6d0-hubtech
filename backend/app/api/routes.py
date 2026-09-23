"""REST-эндпоинты сервиса автозаказов."""
from __future__ import annotations

from fastapi import APIRouter, Response

from app.core.export import to_excel_bytes
from app.core.recommend import generate_recommendations
from app.data.adapter import get_data_source
from app.schemas import RecommendRequest, RecommendationResponse

router = APIRouter()


def _load():
    return get_data_source().load()


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/meta")
def meta() -> dict:
    """Справочники для фильтров UI: склады, категории, поставщики."""
    ds = _load()
    return {
        "warehouses": sorted(ds.sales["warehouse"].dropna().unique().tolist()),
        "categories": sorted(ds.sales["category"].dropna().unique().tolist()),
        "suppliers": ds.suppliers.to_dict("records"),
        "sku_count": int(ds.sales["sku"].nunique()),
    }


@router.post("/recommend", response_model=RecommendationResponse)
def recommend(req: RecommendRequest) -> RecommendationResponse:
    ds = _load()
    return generate_recommendations(
        ds,
        warehouse=req.warehouse,
        category=req.category,
        service_level=req.service_level,
        review_period_days=req.review_period_days,
        explain=req.explain,
    )


@router.post("/recommend/export")
def recommend_export(req: RecommendRequest) -> Response:
    ds = _load()
    resp = generate_recommendations(
        ds,
        warehouse=req.warehouse,
        category=req.category,
        service_level=req.service_level,
        review_period_days=req.review_period_days,
        explain=req.explain,
    )
    data = to_excel_bytes(resp)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="order_recommendations.xlsx"'},
    )
