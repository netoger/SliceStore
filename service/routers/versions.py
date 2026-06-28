# service/routers/versions.py
"""Управление версиями .sls файлов."""
import os
import tempfile
import uuid
import asyncio
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import Response
from typing import List, Optional

from database import get_pool
from storage import upload_file, download_file
from models import VersionResponse

router = APIRouter(prefix="/slices", tags=["versions"])


@router.get("/{slice_id}/versions", response_model=List[VersionResponse])
async def list_versions(slice_id: uuid.UUID):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM slice_versions
            WHERE slice_id = $1
            ORDER BY version_num DESC
            """,
            slice_id,
        )
    return [VersionResponse(**dict(r)) for r in rows]


@router.post("/{slice_id}/versions", response_model=VersionResponse, status_code=201)
async def create_version(
    slice_id: uuid.UUID,
    file: UploadFile = File(...),
    message: Optional[str] = Form(None),
    author: Optional[str] = Form(None),
):
    from storage import download_file as dl

    new_content = await file.read()
    pool = await get_pool()

    async with pool.acquire() as conn:
        slice_row = await conn.fetchrow(
            "SELECT * FROM slices WHERE id = $1 AND deleted_at IS NULL", slice_id
        )
        if not slice_row:
            raise HTTPException(404, detail="Файл не найден")

        last_ver = await conn.fetchrow(
            """
            SELECT * FROM slice_versions
            WHERE slice_id = $1
            ORDER BY version_num DESC LIMIT 1
            """,
            slice_id,
        )

        new_version_num = (last_ver["version_num"] + 1) if last_ver else 1

        is_snapshot = True
        delta_key = None
        storage_key = f"slices/{slice_id}/v{new_version_num}.sls"

        if last_ver and last_ver["storage_key"]:
            try:
                from format.delta import create_delta, delta_stats

                old_content = await dl(last_ver["storage_key"])
                patch = create_delta(old_content, new_content)
                stats = delta_stats(old_content, patch)

                if stats["ratio_percent"] < 50:
                    delta_key = f"slices/{slice_id}/v{new_version_num}.patch"
                    await upload_file(delta_key, patch)
                    is_snapshot = False
                    print(f"[Versions] Дельта: {stats['ratio_percent']}% от оригинала")
                else:
                    print(
                        f"[Versions] Патч слишком большой ({stats['ratio_percent']}%), сохраняем снимок"
                    )
            except Exception as e:
                print(f"[Versions] Не удалось создать дельту: {e}, сохраняем снимок")

        await upload_file(storage_key, new_content)

        await conn.execute(
            """
            UPDATE slices SET storage_key=$1, file_size=$2, updated_at=NOW()
            WHERE id=$3
            """,
            storage_key,
            len(new_content),
            slice_id,
        )

        row = await conn.fetchrow(
            """
            INSERT INTO slice_versions
                (slice_id, version_num, message, author, file_size, storage_key, delta_key, is_snapshot)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
            """,
            slice_id,
            new_version_num,
            message,
            author,
            len(new_content),
            storage_key,
            delta_key,
            is_snapshot,
        )

    return VersionResponse(**dict(row))


@router.get("/{slice_id}/versions/{version_num}", response_class=Response)
async def get_version(slice_id: uuid.UUID, version_num: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM slice_versions
            WHERE slice_id=$1 AND version_num=$2
            """,
            slice_id,
            version_num,
        )

    if not row:
        raise HTTPException(404, detail="Версия не найдена")

    content = await download_file(row["storage_key"])
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="v{version_num}.sls"'},
    )


@router.post("/{slice_id}/restore/{version_num}", status_code=200)
async def restore_version(slice_id: uuid.UUID, version_num: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM slice_versions WHERE slice_id=$1 AND version_num=$2
            """,
            slice_id,
            version_num,
        )

        if not row:
            raise HTTPException(404, detail="Версия не найдена")

        content = await download_file(row["storage_key"])

        last = await conn.fetchrow(
            "SELECT MAX(version_num) as max_v FROM slice_versions WHERE slice_id=$1",
            slice_id,
        )
        new_num = (last["max_v"] or 0) + 1

        new_key = f"slices/{slice_id}/v{new_num}.sls"
        await upload_file(new_key, content)

        await conn.execute(
            """
            INSERT INTO slice_versions (slice_id, version_num, message, is_snapshot, file_size, storage_key)
            VALUES ($1,$2,$3, TRUE,$4,$5)
            """,
            slice_id,
            new_num,
            f"Restored from version {version_num}",
            len(content),
            new_key,
        )

        await conn.execute(
            "UPDATE slices SET storage_key=$1, updated_at=NOW() WHERE id=$2",
            new_key,
            slice_id,
        )

    return {"message": f"Версия {version_num} восстановлена как v{new_num}"}


@router.post("/{slice_id}/versions/gcode", response_model=VersionResponse, status_code=201)
async def upload_gcode_version(
    slice_id: uuid.UUID,
    file: UploadFile = File(...),
    message: Optional[str] = Form(""),
    author: Optional[str] = Form("unknown"),
    force_snapshot: bool = Form(False),
):
    """Новая версия G-code: конвертация в .sls + дельта при малом изменении."""
    from format.versioning import gcode_delta_or_full
    from gcode_tasks import _stream_gcode_to_sls

    content = await file.read()
    pool = await get_pool()

    async with pool.acquire() as conn:
        slice_row = await conn.fetchrow(
            "SELECT * FROM slices WHERE id = $1 AND deleted_at IS NULL", slice_id
        )
        if not slice_row:
            raise HTTPException(404, detail="Файл не найден")
        last_ver = await conn.fetchrow(
            """
            SELECT * FROM slice_versions
            WHERE slice_id = $1 ORDER BY version_num DESC LIMIT 1
            """,
            slice_id,
        )
        new_version_num = (last_ver["version_num"] + 1) if last_ver else 1

    printer_type = slice_row.get("printer_type") or "fdm"
    sls_data, stats, _ = await asyncio.to_thread(
        _stream_gcode_to_sls,
        file.filename or "new.gcode",
        content,
        printer_type,
        slice_row["name"],
        slice_row.get("material") or "",
    )

    change_type = "full"
    delta_key = None
    is_snapshot = True
    change_summary = message or "G-code update"

    with tempfile.TemporaryDirectory() as tmp:
        new_gcode = os.path.join(tmp, "new.gcode")
        with open(new_gcode, "wb") as f:
            f.write(content)

        if last_ver and last_ver["storage_key"] and not force_snapshot:
            old_sls = await download_file(last_ver["storage_key"])
            old_gcode = os.path.join(tmp, "old.gcode")
            with open(old_gcode, "wb") as f:
                f.write(old_sls)
            decision = await asyncio.to_thread(
                gcode_delta_or_full, old_gcode, new_gcode, force_snapshot
            )
            if decision["type"] == "delta":
                delta_key = f"slices/{slice_id}/v{new_version_num}.patch"
                await upload_file(delta_key, decision["patch"])
                change_type = "delta"
                is_snapshot = False
                change_summary = f"delta {decision['ratio']:.1%}"
            else:
                change_summary = decision.get("reason", "full")

    storage_key = f"slices/{slice_id}/v{new_version_num}.sls"
    await upload_file(storage_key, sls_data)

    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE slices SET storage_key=$1, file_size=$2, layer_count=$3, updated_at=NOW()
            WHERE id=$4
            """,
            storage_key,
            len(sls_data),
            stats.get("layers_indexed"),
            slice_id,
        )
        for layer_num, byte_offset in stats.get("layer_index", {}).items():
            await conn.execute(
                """
                INSERT INTO gcode_layer_index (slice_id, layer_num, byte_offset)
                VALUES ($1, $2, $3)
                ON CONFLICT (slice_id, layer_num) DO UPDATE SET byte_offset = $3
                """,
                slice_id,
                layer_num,
                byte_offset,
            )
        row = await conn.fetchrow(
            """
            INSERT INTO slice_versions (
                slice_id, version_num, message, author, file_size, storage_key,
                delta_key, is_snapshot, change_type, change_summary
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            RETURNING *
            """,
            slice_id,
            new_version_num,
            message,
            author,
            len(sls_data),
            storage_key,
            delta_key,
            is_snapshot,
            change_type,
            change_summary,
        )

    return VersionResponse(**dict(row))
