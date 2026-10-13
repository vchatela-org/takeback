from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel, ConfigDict

from app.models import ImportStatus


class MediaFileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    relative_path: str
    media_type: str
    original_name: Optional[str] = None


class ItemSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    batch_id: int
    source: str
    category: Optional[str] = None
    item_type: str
    timestamp: Optional[datetime] = None
    title: Optional[str] = None
    author: Optional[str] = None
    context: Optional[str] = None
    media_files: List[MediaFileOut] = []


class ItemOut(ItemSummary):
    content: Optional[str] = None
    fields_json: Optional[str] = None
    raw_json: Optional[str] = None


class CategoryCount(BaseModel):
    category: Optional[str] = None
    item_type: str
    count: int


class SyncRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    status: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    message: Optional[str] = None
    stats_json: Optional[str] = None


class ImportBatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: Optional[str] = None
    original_filename: str
    status: ImportStatus
    error_message: Optional[str] = None
    item_count: int
    created_at: datetime
    completed_at: Optional[datetime] = None
