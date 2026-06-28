# service/gcode_tasks.py
"""Фоновые задачи обработки G-code (статус в Redis)."""
import asyncio
import json
import os
import tempfile
import uuid
from typing import List, Optional, Tuple

from cache import get_redis
from database import get_pool
from storage import upload_file

VALID_PRINTER_TYPES = {"fdm", "sls", "slm"}


async def _set_task(task_id: str, data: dict):
    r = await get_redis()
    await r.setex(f"gcode:task:{task_id}", 3600, json.dumps(data, default=str))


async def get_task_status(task_id: str) -> Optional[dict]:
    r = await get_redis()
    val = await r.get(f"gcode:task:{task_id}")
    return json.loads(val) if val else None


def _stream_gcode_to_sls(
    filename: str, content: bytes, printer_type: str, name: str, material: str
) -> Tuple[bytes, dict, str]:
    from format.gcode_streamer import GCodeStreamer

    with tempfile.TemporaryDirectory() as tmp:
        gcode_path = os.path.join(tmp, filename or "upload.gcode")
        sls_path = os.path.join(tmp, "output.sls")
        with open(gcode_path, "wb") as f:
            f.write(content)
        streamer = GCodeStreamer(sls_path)
        stats = streamer.write_gcode(
            gcode_path,
            {
                "printer_type": printer_type,
                "name": name or filename,
                "material": material,
            },
        )
        with open(sls_path, "rb") as f:
            sls_data = f.read()
    return sls_data, stats, filename or "upload.gcode"


async def save_gcode_slice(
    sls_data: bytes,
    stats: dict,
    filename: str,
    printer_type: str,
    name: str,
    material: str,
    tags: List[str],
) -> uuid.UUID:
    import xxhash

    storage_key = f"slices/{uuid.uuid4()}.sls"
    file_size = len(sls_data)
    checksum = xxhash.xxh64(sls_data).hexdigest()
    display_name = name or filename

    await upload_file(storage_key, sls_data)

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO slices (
                name, material, layer_count, file_size, checksum, storage_key,
                printer_type, format, source_file
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'gcode', $8)
            RETURNING id
            """,
            display_name,
            material or None,
            stats.get("layers_indexed"),
            file_size,
            checksum,
            storage_key,
            printer_type,
            filename,
        )
        slice_id = row["id"]
        await conn.execute(
            """
            INSERT INTO slice_versions (
                slice_id, version_num, message, is_snapshot, file_size, storage_key, change_type
            )
            VALUES ($1, 1, 'G-code initial upload', TRUE, $2, $3, 'full')
            """,
            slice_id,
            file_size,
            storage_key,
        )
        for tag in tags:
            await conn.execute(
                "INSERT INTO slice_tags (slice_id, tag) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                slice_id,
                tag,
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
    return slice_id


async def process_gcode_upload(
    filename: str,
    content: bytes,
    printer_type: str,
    name: str,
    material: str,
    tags: List[str],
) -> Tuple[uuid.UUID, dict]:
    sls_data, stats, src_name = await asyncio.to_thread(
        _stream_gcode_to_sls, filename, content, printer_type, name, material
    )
    slice_id = await save_gcode_slice(
        sls_data, stats, src_name, printer_type, name, material, tags
    )
    return slice_id, stats


async def enqueue_gcode_processing(
    filename: str,
    content: bytes,
    printer_type: str,
    name: str,
    material: str,
    tags: List[str],
) -> str:
    task_id = str(uuid.uuid4())
    await _set_task(
        task_id,
        {"status": "queued", "progress": 0, "filename": filename, "slice_id": None, "error": None},
    )

    async def _run():
        try:
            await _set_task(
                task_id, {"status": "processing", "progress": 20, "filename": filename}
            )
            slice_id, stats = await process_gcode_upload(
                filename, content, printer_type, name, material, tags
            )
            await _set_task(
                task_id,
                {
                    "status": "completed",
                    "progress": 100,
                    "filename": filename,
                    "slice_id": str(slice_id),
                    "stats": {k: v for k, v in stats.items() if k != "layer_index"},
                },
            )
        except Exception as e:
            await _set_task(
                task_id,
                {"status": "failed", "progress": 0, "filename": filename, "error": str(e)},
            )

    asyncio.create_task(_run())
    return task_id
