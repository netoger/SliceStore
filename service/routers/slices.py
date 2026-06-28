# service/routers/slices.py
"""CRUD операции с .sls файлами."""
import json
import struct
import uuid
import xxhash
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import Response
from typing import Optional

from database import get_pool
from storage import upload_file, download_file
from cache import cache_get, cache_set, cache_delete
from models import SliceResponse

router = APIRouter(prefix="/slices", tags=["slices"])


@router.post("/", response_model=SliceResponse, status_code=201)
async def upload_slice(
    file: UploadFile = File(..., description="Файл в формате .sls"),
    name: str = Form(...),
    printer_id: Optional[str] = Form(None),
    material: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
):
    content = await file.read()
    if len(content) < 9 or content[:9] != b"SLSSTORE!":
        raise HTTPException(
            400, detail="Файл не является валидным .sls файлом (неверная сигнатура)"
        )

    file_size = len(content)
    checksum = xxhash.xxh64(content).hexdigest()
    storage_key = f"slices/{uuid.uuid4()}.sls"
    tag_list = json.loads(tags) if tags else []

    try:
        block_count = struct.unpack_from("<I", content, 12)[0]
        layer_count = block_count - 1
    except Exception:
        layer_count = None

    await upload_file(storage_key, content)

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO slices (name, printer_id, material, layer_count, file_size, checksum, storage_key)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING *
            """,
            name,
            printer_id,
            material,
            layer_count,
            file_size,
            checksum,
            storage_key,
        )

        slice_id = row["id"]

        await conn.execute(
            """
            INSERT INTO slice_versions (slice_id, version_num, message, is_snapshot, file_size, storage_key)
            VALUES ($1, 1, 'Initial upload', TRUE, $2, $3)
            """,
            slice_id,
            file_size,
            storage_key,
        )

        for tag in tag_list:
            await conn.execute(
                "INSERT INTO slice_tags (slice_id, tag) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                slice_id,
                tag,
            )

    result = dict(row)
    result["tags"] = tag_list
    return SliceResponse(**result)


@router.get("/{slice_id}", response_class=Response)
async def download_slice(slice_id: uuid.UUID):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT storage_key FROM slices WHERE id = $1 AND deleted_at IS NULL",
            slice_id,
        )
    if not row:
        raise HTTPException(404, detail="Файл не найден")

    content = await download_file(row["storage_key"])
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{slice_id}.sls"'},
    )


@router.get("/{slice_id}/metadata", response_model=SliceResponse)
async def get_metadata(slice_id: uuid.UUID):
    cached = await cache_get(f"slice:{slice_id}")
    if cached:
        return SliceResponse(**cached)

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM slices WHERE id = $1 AND deleted_at IS NULL", slice_id
        )
        if not row:
            raise HTTPException(404, detail="Файл не найден")
        tags = await conn.fetch(
            "SELECT tag FROM slice_tags WHERE slice_id = $1", slice_id
        )

    result = dict(row)
    result["tags"] = [t["tag"] for t in tags]

    await cache_set(f"slice:{slice_id}", result)
    return SliceResponse(**result)


@router.delete("/{slice_id}", status_code=204)
async def delete_slice(slice_id: uuid.UUID):
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE slices SET deleted_at = NOW() WHERE id = $1 AND deleted_at IS NULL",
            slice_id,
        )
    if result == "UPDATE 0":
        raise HTTPException(404, detail="Файл не найден")

    await cache_delete(f"slice:{slice_id}")
    return None
