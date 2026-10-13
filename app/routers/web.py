import json
import logging
import math
import uuid
from datetime import datetime
from typing import List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request, UploadFile, File, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from sqlalchemy.orm import Session

from app import crud, dedup, processing, anonymize, limits, platforms, scheduler, stats
from app.importers.meta_common import find_instagram_post_urls
from app.database import get_db, uploads_dir, imports_dir, anonymized_dir
from app.models import ImportStatus
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(tags=["web"])


def parse_date(value: Optional[str], end_of_day: bool = False) -> Optional[datetime]:
    """Accept plain "YYYY-MM-DD" from an <input type="date"> as well as a
    full ISO datetime (what a click on a chart bucket sends) -- date_to is
    treated as end-of-day so a range like from=to=today includes everything
    that happened that day, not none of it (a bare date parses to midnight,
    which would exclude the day)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if end_of_day and len(value) <= 10:  # a bare "YYYY-MM-DD", no time part
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt


# ────────────────────────────────────────────────
# Home: one card per supported platform
# ────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    summaries = crud.get_platform_summaries(db)
    types_by_source = crud.get_item_types_by_source(db)
    cards = []
    for platform in platforms.supported():
        summary = summaries.get(platform.key)
        cards.append({
            "platform": platform,
            "summary": summary,
            "views": platforms.available_views(types_by_source.get(platform.key, ()))[1:],
        })
    # Platforms with data first, biggest first; the not-yet-imported ones after.
    cards.sort(key=lambda c: -(c["summary"]["count"] if c["summary"] else -1))

    batches = crud.get_batches(db)
    in_progress = [b for b in batches if b.status.value in ("pending", "extracting", "parsing")]
    return templates.TemplateResponse(
        request, "home.html",
        {
            "cards": cards,
            "planned": platforms.planned(),
            "total_items": sum(s["count"] for s in summaries.values()),
            "in_progress": in_progress,
            "recent_batches": batches[:5],
        },
    )


# ────────────────────────────────────────────────
# Imports: upload + management of the imported files
# ────────────────────────────────────────────────

@router.get("/imports", response_class=HTMLResponse)
def imports_page(request: Request, db: Session = Depends(get_db)):
    # Sinks: configured ones, and any that ran before (so a removed
    # configuration still shows its last state).
    syncs = []
    for job in scheduler.all_jobs():
        runs = crud.get_sync_runs(db, job.kind, limit=5)
        if job.enabled() or runs:
            syncs.append({"job": job, "configured": job.enabled(), "runs": runs})
    return templates.TemplateResponse(
        request, "imports.html",
        {
            "batches": crud.get_batches(db),
            "supported": platforms.supported(),
            "syncs": syncs,
        },
    )


@router.post("/sync/{kind}")
def request_sync(kind: str, request: Request, rebuild: bool = False):
    """The "Envoyer maintenant" / "Reconstruire" buttons: queue the run,
    back to the page."""
    job = scheduler.get(kind)
    if job is None:
        raise HTTPException(status_code=404, detail="Synchronisation inconnue")
    options = {"rebuild": True} if rebuild and "rebuild" in job.options else {}
    if not scheduler.request(kind, **options):
        raise HTTPException(status_code=409, detail="Synchronisation non configurée")
    # Back to the page the button was on -- its path only, so never out of
    # this app, whatever host the proxy in front of it answers on. "//host"
    # and "/\\host" are paths a browser reads as another site.
    referer = urlparse(request.headers.get("referer") or "")
    back = (referer.path or "/imports") + (f"?{referer.query}" if referer.query else "")
    if not back.startswith("/") or back.startswith("//") or "\\" in back:
        back = "/imports"
    return RedirectResponse(back, status_code=303)


def _batch_label(filenames: List[str]) -> str:
    if len(filenames) == 1:
        return filenames[0]
    preview = ", ".join(filenames[:3])
    if len(filenames) > 3:
        preview += ", ..."
    return f"{len(filenames)} fichiers ({preview})"


@router.post("/imports")
async def upload_import(
    request: Request,
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    """Legacy single-request path: every file in one multipart POST. Kept for
    the API and small exports, but a large multi-zip export (several GB total
    in one request) is exactly the shape that a reverse proxy default limit
    or a long-upload timeout tends to cut short partway through -- see the
    /imports/start + /imports/{id}/upload-file + /imports/{id}/finalize
    sequence below, which the imports page's own form uses instead, one
    smaller request per file."""
    if not files or any(not f.filename.lower().endswith(".zip") for f in files):
        raise HTTPException(status_code=400, detail="Seules les archives .zip sont acceptées")

    logger.info("Upload request received: %d file(s): %s", len(files), [f.filename for f in files])

    batch = crud.create_batch(db, original_filename=_batch_label([f.filename for f in files]))
    # Read once, before anything can delete the row: an attribute read on a
    # deleted instance re-loads it and raises ObjectDeletedError.
    batch_id = batch.id

    upload_paths = []
    for file in files:
        upload_path = uploads_dir() / f"{batch_id}-{uuid.uuid4().hex}.zip"
        try:
            written = await limits.stream_upload(file, upload_path, file.filename)
            limits.check_batch_upload_size([*upload_paths, upload_path])
        except limits.UploadTooLarge as exc:
            # Nothing usable was received: drop the batch rather than leave an
            # empty import listed on the page.
            crud.delete_batch(db, batch_id)
            processing.delete_batch_files(batch_id)
            raise HTTPException(status_code=413, detail=str(exc))
        upload_paths.append(upload_path)
        logger.info("Saved upload %r -> %s (%d bytes on disk)", file.filename, upload_path.name, written)

    logger.info("Batch %s: %d file(s) saved, handing off to background processing", batch_id, len(upload_paths))
    background_tasks.add_task(processing.process_import, batch_id, upload_paths)
    return RedirectResponse(f"/imports/{batch_id}", status_code=303)


# ────────────────────────────────────────────────
# Chunked upload: one small(er) request per file, so an export split into
# many large zips never depends on a single multi-GB request surviving a
# proxy body-size limit or a timeout on a slow connection.
# ────────────────────────────────────────────────

@router.post("/imports/start")
async def start_import(request: Request, db: Session = Depends(get_db)):
    """Create an empty batch to attach files to one at a time. Accepts the
    selected filenames (JSON body: {"filenames": [...]}) just to build a
    readable label up front -- no file content yet."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    filenames = payload.get("filenames") or []
    if not filenames or any(not str(f).lower().endswith(".zip") for f in filenames):
        raise HTTPException(status_code=400, detail="Seules les archives .zip sont acceptées")

    batch = crud.create_batch(db, original_filename=_batch_label([str(f) for f in filenames]))
    logger.info("Batch %s: started (chunked upload), expecting %d file(s): %s", batch.id, len(filenames), filenames)
    return {"batch_id": batch.id}


@router.post("/imports/{batch_id}/upload-file")
async def upload_one_file(batch_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """One file, one request. Called once per selected zip by the imports
    page's upload script, sequentially, after /imports/start."""
    batch = crud.get_batch(db, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Import introuvable")
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Seules les archives .zip sont acceptées")

    upload_path = uploads_dir() / f"{batch_id}-{uuid.uuid4().hex}.zip"
    try:
        written = await limits.stream_upload(file, upload_path, file.filename)
        # The file just written is already on disk under the batch's prefix, so
        # the glob is the whole batch -- this file included.
        limits.check_batch_upload_size(uploads_dir().glob(f"{batch_id}-*.zip"))
    except limits.UploadTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    logger.info("Batch %s: saved upload %r -> %s (%d bytes on disk)", batch_id, file.filename, upload_path.name, written)
    return {"filename": file.filename, "bytes": written}


@router.post("/imports/{batch_id}/finalize")
def finalize_import(batch_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """All files for this batch have been uploaded one by one -- pick them up
    from disk by their batch-id prefix and start extraction/parsing."""
    batch = crud.get_batch(db, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Import introuvable")

    upload_paths = sorted(uploads_dir().glob(f"{batch_id}-*.zip"))
    if not upload_paths:
        raise HTTPException(status_code=400, detail="Aucun fichier reçu pour cet import")

    logger.info("Batch %s: finalizing with %d file(s) on disk", batch_id, len(upload_paths))
    background_tasks.add_task(processing.process_import, batch_id, upload_paths)
    return {"status": "started", "batch_id": batch_id}


@router.get("/imports/{batch_id}", response_class=HTMLResponse)
def batch_detail(batch_id: int, request: Request, db: Session = Depends(get_db)):
    batch = crud.get_batch(db, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Import introuvable")

    categories: dict = {}
    batch_stats = None
    if batch.status == ImportStatus.done:
        for category, item_type, count in crud.get_category_counts(db, batch_id=batch_id):
            bucket = categories.setdefault(category, {"total": 0, "item_types": []})
            bucket["total"] += count
            bucket["item_types"].append((item_type, count))
        batch_stats = crud.get_batch_stats(db, batch_id)

    extract_dir = imports_dir() / str(batch_id)
    has_extracted_files = extract_dir.exists() and any(extract_dir.iterdir())

    return templates.TemplateResponse(
        request, "batch_detail.html",
        {
            "batch": batch, "batch_id": batch.id, "batch_stats": batch_stats,
            "state": anonymize.get_state(batch.id), "categories": categories,
            "has_extracted_files": has_extracted_files,
            "anonymize_error": anonymize.get_error_detail(batch.id),
            "anonymize_warnings": anonymize.get_warnings(batch.id),
        },
    )


@router.post("/imports/{batch_id}/reanalyze")
def reanalyze(batch_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Re-run detection+parsing on the files already extracted from a
    previous upload -- no re-upload needed. Useful after a parser fix, on an
    import whose zips are large/slow to push again."""
    batch = crud.get_batch(db, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Import introuvable")
    extract_dir = imports_dir() / str(batch_id)
    if not extract_dir.exists() or not any(extract_dir.iterdir()):
        raise HTTPException(status_code=400, detail="Aucun fichier extrait à réanalyser pour cet import")
    # Flip the status now, not in the background task: the redirect below can
    # be served before the task starts, and a page rendered as "done" never
    # polls, so it would stay stale until a manual refresh.
    batch.status = ImportStatus.parsing
    batch.error_message = None
    db.commit()
    background_tasks.add_task(processing.reanalyze_import, batch_id)
    return RedirectResponse(f"/imports/{batch_id}", status_code=303)


@router.get("/imports/{batch_id}/status", response_class=HTMLResponse)
def batch_status_fragment(batch_id: int, request: Request, db: Session = Depends(get_db)):
    batch = crud.get_batch(db, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Import introuvable")
    response = templates.TemplateResponse(request, "_batch_status.html", {"batch": batch})
    # The polling fragment only covers the status line: once the import is
    # finished, reload the whole page so its content summary appears too.
    if request.headers.get("HX-Request") and batch.status.value not in ("pending", "extracting", "parsing"):
        response.headers["HX-Refresh"] = "true"
    return response


@router.post("/imports/{batch_id}/delete")
def batch_delete(batch_id: int, db: Session = Depends(get_db)):
    source = crud.delete_batch(db, batch_id)
    if source is not None:
        processing.delete_batch_files(batch_id)
        # The copies this import held may have been the ones its platform
        # showed -- hand that over to the remaining imports' copies.
        dedup.refresh_duplicates(db, source)
        processing.notify_platform_changed(source)
    return RedirectResponse("/imports", status_code=303)


# ────────────────────────────────────────────────
# Anonymized re-export
# ────────────────────────────────────────────────

@router.post("/imports/{batch_id}/anonymize")
def start_anonymize(batch_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    batch = crud.get_batch(db, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Import introuvable")
    extract_dir = imports_dir() / str(batch_id)
    if not extract_dir.exists():
        raise HTTPException(status_code=400, detail="Fichiers de l'import introuvables")
    background_tasks.add_task(anonymize.run_anonymize_job, batch_id, extract_dir, batch.source)
    return RedirectResponse(f"/imports/{batch_id}", status_code=303)


@router.get("/imports/{batch_id}/anonymize/status", response_class=HTMLResponse)
def anonymize_status_fragment(batch_id: int, request: Request):
    return templates.TemplateResponse(
        request, "_anonymize_status.html",
        {
            "batch_id": batch_id, "state": anonymize.get_state(batch_id),
            "anonymize_error": anonymize.get_error_detail(batch_id),
            "anonymize_warnings": anonymize.get_warnings(batch_id),
        },
    )


@router.get("/imports/{batch_id}/anonymize/download")
def download_anonymized(batch_id: int):
    zip_path = anonymized_dir() / f"{batch_id}.zip"
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Archive anonymisée introuvable")
    return FileResponse(zip_path, filename=f"takeback_anonymized_{batch_id}.zip", media_type="application/zip")


# ────────────────────────────────────────────────
# Former per-import analysis pages: the analysis now lives on the platform
# pages (every import of a platform together) -- old links and bookmarks
# land on the platform equivalent.
# ────────────────────────────────────────────────

_LEGACY_VIEWS = {
    "stats": "/activity",
    "reading-stats": "/reading",
    "purchase-stats": "/purchases",
    "purchases": "/purchases/list",
    "conversations": "/conversations",
}


def _legacy_redirect(batch_id: int, view: str, request: Request, db: Session):
    batch = crud.get_batch(db, batch_id)
    if not batch or not batch.source:
        raise HTTPException(status_code=404, detail="Import introuvable")
    query = request.url.query
    target = f"/p/{batch.source}{_LEGACY_VIEWS[view]}" + (f"?{query}" if query else "")
    return RedirectResponse(target, status_code=301)


@router.get("/imports/{batch_id}/stats")
def legacy_stats(batch_id: int, request: Request, db: Session = Depends(get_db)):
    return _legacy_redirect(batch_id, "stats", request, db)


@router.get("/imports/{batch_id}/reading-stats")
def legacy_reading_stats(batch_id: int, request: Request, db: Session = Depends(get_db)):
    return _legacy_redirect(batch_id, "reading-stats", request, db)


@router.get("/imports/{batch_id}/purchase-stats")
def legacy_purchase_stats(batch_id: int, request: Request, db: Session = Depends(get_db)):
    return _legacy_redirect(batch_id, "purchase-stats", request, db)


@router.get("/imports/{batch_id}/purchases")
def legacy_purchases(batch_id: int, request: Request, db: Session = Depends(get_db)):
    return _legacy_redirect(batch_id, "purchases", request, db)


@router.get("/imports/{batch_id}/conversations")
def legacy_conversations(batch_id: int, request: Request, db: Session = Depends(get_db)):
    return _legacy_redirect(batch_id, "conversations", request, db)


# ────────────────────────────────────────────────
# Browse / search
# ────────────────────────────────────────────────

# Above this many matching rows, sorting/column-picking would mean loading
# too much into memory at once -- browse falls back to the plain paginated,
# fixed-column view instead (still fast, just not user-customizable).
BROWSE_DYNAMIC_TABLE_LIMIT = 5000


def _browse_sort_value(item, items_fields: dict, sort: str, has_content: bool, has_titles: bool):
    """Returns a sortable value for one row, or None if this row has nothing
    for that column (pushed to the end regardless of sort direction)."""
    if sort == "date":
        return item.timestamp
    if sort == "content" and has_content:
        return (item.content or "").lower() or None
    if sort == "title" and has_titles:
        return (item.title or "").lower() or None
    text = str(items_fields.get(item.id, {}).get(sort, "") or "")
    if not text:
        return None
    num = stats.extract_leading_number(text)
    if num is not None:
        return num
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return text.lower()


def _sort_browse_items(items: list, items_fields: dict, sort: str, direction: str, has_content: bool, has_titles: bool) -> list:
    keyed = [(it, _browse_sort_value(it, items_fields, sort, has_content, has_titles)) for it in items]
    present = [pair for pair in keyed if pair[1] is not None]
    missing = [it for it, val in keyed if val is None]
    reverse = direction == "desc"
    try:
        present.sort(key=lambda pair: pair[1], reverse=reverse)
    except TypeError:
        # A column whose values don't all parse the same way (numbers here,
        # plain text there) -- fall back to comparing everything as text
        # rather than crashing the page.
        present.sort(key=lambda pair: str(pair[1]), reverse=reverse)
    return [it for it, _ in present] + missing


@router.get("/browse", response_class=HTMLResponse)
def browse(
    request: Request,
    source: Optional[str] = None,
    category: Optional[str] = None,
    item_type: Optional[str] = None,
    batch_id: Optional[int] = None,
    context: Optional[str] = None,
    q: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    sort: Optional[str] = None,
    direction: str = "desc",
    cols: Optional[str] = None,
    page: int = 1,
    db: Session = Depends(get_db),
):
    """Free search over every platform (deduplicated across imports), or
    over one import's own items when batch_id is given."""
    page_size = 50
    page = max(1, page)
    direction = direction if direction in ("asc", "desc") else "desc"
    filters = dict(
        source=source, category=category, item_type=item_type, batch_id=batch_id, context=context, q=q,
        date_from=parse_date(date_from), date_to=parse_date(date_to, end_of_day=True),
    )

    field_columns: list = []
    available_columns: list = []
    has_titles = False
    has_content = False
    dynamic = False

    if item_type:
        all_items, total = crud.get_items_for_column_table(db, limit=BROWSE_DYNAMIC_TABLE_LIMIT, **filters)
        dynamic = total <= BROWSE_DYNAMIC_TABLE_LIMIT

    if item_type and dynamic:
        items_fields = {it.id: json.loads(it.fields_json) for it in all_items if it.fields_json}
        has_titles = any(i.title for i in all_items)
        has_content = any(i.content for i in all_items)
        for fields in items_fields.values():
            for key in fields.keys():
                if key not in available_columns:
                    available_columns.append(key)

        if cols is not None:
            requested = [c for c in cols.split(",") if c]
            field_columns = [c for c in requested if c in available_columns]
        else:
            # Addresses are captured for completeness but clutter this table
            # by default; still selectable via "Colonnes" (and always on
            # /items/{id}).
            field_columns = [c for c in available_columns if "Adresse" not in c]

        sort = sort or "date"
        sortable_columns = ["date"] + (["content"] if has_content else []) + (["title"] if has_titles else []) + field_columns
        if sort not in sortable_columns:
            sort = "date"
        all_items = _sort_browse_items(all_items, items_fields, sort, direction, has_content, has_titles)

        total = len(all_items)
        items = all_items[(page - 1) * page_size: page * page_size]
    else:
        items, total = crud.search_items(db, page=page, page_size=page_size, **filters)
        items_fields = {item.id: json.loads(item.fields_json) for item in items if item.fields_json}
        if item_type and items_fields:
            has_titles = any(i.title for i in items)
            has_content = any(i.content for i in items)
            for fields in items_fields.values():
                for key in fields.keys():
                    if "Adresse" in key:
                        continue
                    if key not in field_columns:
                        field_columns.append(key)
            field_columns = field_columns[:12]
        sort = sort or "date"

    batch = crud.get_batch(db, batch_id) if batch_id else None
    return templates.TemplateResponse(
        request, "browse.html",
        {
            "items": items,
            "items_fields": items_fields,
            "field_columns": field_columns,
            "available_columns": available_columns,
            "dynamic": dynamic,
            "over_dynamic_limit": bool(item_type) and not dynamic,
            "dynamic_limit": BROWSE_DYNAMIC_TABLE_LIMIT,
            "sort": sort,
            "direction": direction,
            "has_titles": has_titles,
            "has_content": has_content,
            "total": total,
            "page": page,
            "page_count": max(1, math.ceil(total / page_size)),
            "sources": crud.get_sources(db),
            "categories": crud.get_categories(db, source=source, batch_id=batch_id),
            "item_types": crud.get_item_types(db, source=source, category=category),
            "batch": batch,
            "filters": {
                "source": source or "", "category": category or "", "item_type": item_type or "",
                "batch_id": batch_id or "", "context": context or "", "q": q or "",
                "date_from": date_from or "", "date_to": date_to or "",
                "cols": ",".join(field_columns) if dynamic else "",
            },
            # Returns, return statuses and refunds share their order ID as
            # `context` with the purchase lines they belong to -- link back to
            # see what was actually ordered (and, for a partial return, which
            # items): stats.purchase_line_type names that platform's lines.
            "show_order_link": item_type in stats.RETURN_ITEM_TYPES,
        },
    )


# Item types whose `context` is an order ID shared with the order's other
# lines -- the item page links to the whole order.
_ORDER_CONTEXT_TYPES = (
    "order_item", "digital_order", "audible_order", "return", "return_status",
    # Seazon: its order, a refund, a meal, a cancelled slot and a delivery
    # rating all carry the order ID they belong to.
    "seazon_order", "seazon_refund", "meal", "order_cancelled", "opinion",
)


@router.get("/items/{item_id}", response_class=HTMLResponse)
def item_detail(item_id: int, request: Request, db: Session = Depends(get_db)):
    item = crud.get_item(db, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item introuvable")
    fields = json.loads(item.fields_json) if item.fields_json else None
    values = list(fields.values()) if fields else []
    if item.content:
        values.append(item.content)
    instagram_urls = find_instagram_post_urls(values)
    return templates.TemplateResponse(
        request, "item_detail.html",
        {
            "item": item, "fields": fields, "instagram_urls": instagram_urls,
            "copies": crud.count_copies(db, item),
            "order_link": item.context and item.item_type in _ORDER_CONTEXT_TYPES,
        },
    )
