# service/routers/search.py
"""Поиск по метаданным."""
from fastapi import APIRouter, Query
from typing import List, Optional

from database import get_pool
from models import SliceResponse

router = APIRouter(prefix="/slices", tags=["search"])


@router.get("/search", response_model=List[SliceResponse])
async def search_slices(
    q: Optional[str] = Query(None, description="Полнотекстовый поиск по имени/материалу"),
    printer_id: Optional[str] = Query(None),
    material: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    pool = await get_pool()

    conditions = ["s.deleted_at IS NULL"]
    params = []
    idx = 1

    if q:
        conditions.append(
            f"""
            to_tsvector('russian', coalesce(s.name,'') || ' ' || coalesce(s.material,'') || ' ' || coalesce(s.printer_id,''))
            @@ plainto_tsquery('russian', ${idx})
        """
        )
        params.append(q)
        idx += 1

    if printer_id:
        conditions.append(f"s.printer_id = ${idx}")
        params.append(printer_id)
        idx += 1

    if material:
        conditions.append(f"s.material ILIKE ${idx}")
        params.append(f"%{material}%")
        idx += 1

    if tag:
        conditions.append(
            f"""
            EXISTS (SELECT 1 FROM slice_tags st WHERE st.slice_id=s.id AND st.tag=${idx})
        """
        )
        params.append(tag)
        idx += 1

    where = " AND ".join(conditions)
    params.extend([limit, offset])

    query = f"""
        SELECT s.*, array_agg(DISTINCT st.tag) FILTER (WHERE st.tag IS NOT NULL) as tags
        FROM slices s
        LEFT JOIN slice_tags st ON st.slice_id = s.id
        WHERE {where}
        GROUP BY s.id
        ORDER BY s.created_at DESC
        LIMIT ${idx} OFFSET ${idx + 1}
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    result = []
    for row in rows:
        d = dict(row)
        d["tags"] = d.get("tags") or []
        result.append(SliceResponse(**d))
    return result
