# service/models.py
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional
from datetime import datetime
import uuid


class SliceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    printer_id: Optional[str] = None
    material: Optional[str] = None
    tags: List[str] = []


class SliceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    printer_id: Optional[str] = None
    material: Optional[str] = None
    layer_count: Optional[int] = None
    file_size: Optional[int] = None
    compression: int = 2
    printer_type: Optional[str] = None
    format: Optional[str] = "sls"
    source_file: Optional[str] = None
    hatch_strategy: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    tags: List[str] = []


class VersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slice_id: uuid.UUID
    version_num: int
    message: Optional[str] = None
    author: Optional[str] = None
    file_size: Optional[int] = None
    is_snapshot: bool
    created_at: datetime


class SearchParams(BaseModel):
    query: Optional[str] = None
    printer_id: Optional[str] = None
    material: Optional[str] = None
    tags: List[str] = []
    limit: int = Field(20, ge=1, le=100)
    offset: int = Field(0, ge=0)
