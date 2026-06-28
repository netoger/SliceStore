# service/routers/gcode.py
"""Загрузка и обработка G-code."""
import json
import uuid
from typing import List

from fastapi import APIRouter, UploadFile, File, Form, HTTPException

from models import SliceResponse
from database import get_pool
from gcode_tasks import (
    VALID_PRINTER_TYPES,
    process_gcode_upload,
    enqueue_gcode_processing,
    get_task_status,
)

router = APIRouter(prefix="/slices", tags=["gcode"])


def _validate_printer_type(printer_type: str):
    if printer_type not in VALID_PRINTER_TYPES:
        raise HTTPException(
            400, detail=f"printer_type должен быть одним из: {', '.join(VALID_PRINTER_TYPES)}"
        )


@router.post("/gcode", status_code=201)
async def upload_gcode(
    file: UploadFile = File(...),
    printer_type: str = Form(...),
    name: str = Form(""),
    material: str = Form(""),
    tags: str = Form("[]"),
):
    _validate_printer_type(printer_type)
    content = await file.read()
    tag_list = json.loads(tags) if tags else []
    slice_id, stats = await process_gcode_upload(
        file.filename or "upload.gcode",
        content,
        printer_type,
        name,
        material,
        tag_list,
    )

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM slices WHERE id = $1", slice_id)
        db_tags = await conn.fetch(
            "SELECT tag FROM slice_tags WHERE slice_id = $1", slice_id
        )

    result = dict(row)
    result["tags"] = [t["tag"] for t in db_tags]
    result["gcode_stats"] = {k: v for k, v in stats.items() if k != "layer_index"}
    return result


@router.post("/gcode/batch", status_code=202)
async def upload_gcode_batch(
    files: List[UploadFile] = File(...),
    printer_type: str = Form(...),
    material: str = Form(""),
):
    _validate_printer_type(printer_type)
    tasks = []
    for f in files:
        content = await f.read()
        task_id = await enqueue_gcode_processing(
            f.filename or "upload.gcode",
            content,
            printer_type,
            f.filename or "",
            material,
            [],
        )
        tasks.append({"filename": f.filename, "task_id": task_id})
    return {"tasks": tasks, "total": len(tasks)}


@router.get("/gcode/task/{task_id}")
async def gcode_task_status(task_id: str):
    status = await get_task_status(task_id)
    if not status:
        raise HTTPException(404, detail="Задача не найдена")
    return status
