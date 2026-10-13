from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app import crud
from app.database import get_db
from app.schemas import ItemOut, ItemSummary

router = APIRouter(prefix="/api/items", tags=["items"])


@router.get("/", response_model=List[ItemSummary])
def list_items(
    source: Optional[str] = None,
    category: Optional[str] = None,
    item_type: Optional[str] = None,
    batch_id: Optional[int] = None,
    q: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=200),
    db: Session = Depends(get_db),
):
    items, _ = crud.search_items(
        db, source=source, category=category, item_type=item_type, batch_id=batch_id, q=q,
        date_from=date_from, date_to=date_to, page=page, page_size=page_size,
    )
    return items


@router.get("/{item_id}", response_model=ItemOut)
def get_item(item_id: int, db: Session = Depends(get_db)):
    item = crud.get_item(db, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item introuvable")
    return item
