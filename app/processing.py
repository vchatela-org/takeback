import json
import logging
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import importers, anonymize, dedup, limits
from app.database import SessionLocal, imports_dir
from app.models import ImportBatch, ImportStatus, Item, MediaFile

logger = logging.getLogger(__name__)

# Called with a platform key once an import, a re-analysis or a deletion
# has changed what that platform holds (app/scheduler.py hooks the
# InfluxDB refresh here). Never allowed to fail the import itself.
on_platform_changed: List[Callable[[str], None]] = []


def notify_platform_changed(source: Optional[str]) -> None:
    if not source:
        return
    for callback in on_platform_changed:
        try:
            callback(source)
        except Exception:
            logger.exception("Platform-changed hook failed for %s", source)


def process_import(batch_id: int, upload_paths: List[Path]) -> None:
    """Runs in a FastAPI BackgroundTask: extract every uploaded zip into the
    SAME directory, detect the source module, parse the merged tree into
    Item/MediaFile rows, and update the batch status throughout so the UI
    can poll progress.

    Large exports (Facebook in particular) get split into several numbered
    zips that only form a complete export once extracted together -- so a
    batch is always "one or more zips extracted into one tree", never
    processed one zip at a time."""
    db = SessionLocal()
    extract_dir = imports_dir() / str(batch_id)
    try:
        batch = db.get(ImportBatch, batch_id)
        batch.status = ImportStatus.extracting
        db.commit()

        extract_dir.mkdir(parents=True, exist_ok=True)
        try:
            for upload_path in upload_paths:
                size = upload_path.stat().st_size if upload_path.exists() else -1
                logger.info("Extracting %s (%d bytes on disk)", upload_path.name, size)
            # Bounded, and every member's target path checked to be inside
            # extract_dir: see app/limits.py for why not `extractall`.
            limits.extract_archive(upload_paths, extract_dir)
        except zipfile.BadZipFile:
            batch.status = ImportStatus.error
            batch.error_message = "Un des fichiers n'est pas une archive .zip valide."
            db.commit()
            return
        except limits.UploadTooLarge as exc:
            batch.status = ImportStatus.error
            batch.error_message = str(exc)
            db.commit()
            return

        top_level = sorted(p.name for p in extract_dir.iterdir())
        total_files = sum(1 for _ in extract_dir.rglob("*") if _.is_file())
        logger.info(
            "Batch %s: extraction done, %d total files on disk, top-level entries: %s",
            batch_id, total_files, top_level,
        )

        _detect_and_parse(db, batch, extract_dir)
    except Exception as exc:
        logger.exception("Import processing failed for batch %s", batch_id)
        db.rollback()
        batch = db.get(ImportBatch, batch_id)
        if batch:
            batch.status = ImportStatus.error
            batch.error_message = str(exc)[:2000]
            db.commit()
    finally:
        for upload_path in upload_paths:
            upload_path.unlink(missing_ok=True)
        db.close()


def reanalyze_import(batch_id: int) -> None:
    """Re-run detection+parsing against files already extracted on disk from
    a previous import, without touching the uploaded zips at all. Existing
    Item/MediaFile rows for the batch are discarded and replaced.

    For an export that took a long time to upload (large Facebook exports in
    particular), this is what makes it possible to pick up a parser fix and
    see the result without pushing everything again -- the extracted tree
    from the first upload is kept around for exactly this."""
    db = SessionLocal()
    extract_dir = imports_dir() / str(batch_id)
    previous_source = None
    try:
        batch = db.get(ImportBatch, batch_id)
        if not batch:
            return
        if not extract_dir.exists() or not any(extract_dir.iterdir()):
            batch.status = ImportStatus.error
            batch.error_message = "Aucun fichier extrait à réanalyser (relancez un import complet)."
            db.commit()
            return

        logger.info("Batch %s: reanalyzing %s (no re-upload)", batch_id, extract_dir)

        previous_source = batch.source
        _clear_items(db, batch_id)
        # Older imports of the same platform take over the items this one
        # held while it's being parsed again.
        dedup.refresh_duplicates(db, batch.source)
        batch.status = ImportStatus.parsing
        batch.error_message = None
        batch.item_count = 0
        db.commit()

        _detect_and_parse(db, batch, extract_dir)
    except Exception as exc:
        logger.exception("Reanalysis failed for batch %s", batch_id)
        db.rollback()
        batch = db.get(ImportBatch, batch_id)
        if batch:
            batch.status = ImportStatus.error
            batch.error_message = str(exc)[:2000]
            db.commit()
    finally:
        try:
            _notify_previous_platform(db, batch_id, previous_source)
        finally:
            db.close()


def _notify_previous_platform(db: Session, batch_id: int, previous_source: Optional[str]) -> None:
    """The items a re-analysis cleared belonged to `previous_source`. A
    successful parse into that same platform has refreshed it already
    (_detect_and_parse); any other ending -- unrecognized, an error, another
    platform -- must refresh it too, or its points outlive its items."""
    if not previous_source:
        return
    try:
        batch = db.get(ImportBatch, batch_id)
        if batch is None or batch.status != ImportStatus.done or batch.source != previous_source:
            notify_platform_changed(previous_source)
    except Exception:
        logger.exception("Batch %s: could not refresh %s after the re-analysis", batch_id, previous_source)


def _clear_items(db: Session, batch_id: int) -> None:
    """Bulk-delete: plain Query.delete() skips ORM cascades, and SQLite
    doesn't enforce foreign keys by default, so MediaFile rows are deleted
    explicitly first rather than relying on either to clean them up."""
    item_ids = select(Item.id).where(Item.batch_id == batch_id)
    db.query(MediaFile).filter(MediaFile.item_id.in_(item_ids)).delete(synchronize_session=False)
    db.query(Item).filter(Item.batch_id == batch_id).delete(synchronize_session=False)
    db.commit()


def _detect_and_parse(db: Session, batch: ImportBatch, extract_dir: Path) -> None:
    importer = importers.detect(extract_dir)
    if importer is None:
        batch.status = ImportStatus.unrecognized
        batch.error_message = "Aucun module d'import ne reconnaît ce format d'export."
        db.commit()
        return

    batch.source = importer.key
    batch.status = ImportStatus.parsing
    db.commit()

    count = 0
    for normalized in importer.parse(extract_dir):
        item = Item(
            batch_id=batch.id,
            source=importer.key,
            category=normalized.category,
            item_type=normalized.item_type,
            timestamp=normalized.timestamp,
            title=normalized.title,
            content=normalized.content,
            author=normalized.author,
            context=normalized.context,
            fields_json=json.dumps(normalized.fields, ensure_ascii=False, default=str) if normalized.fields else None,
            raw_json=json.dumps(normalized.raw, ensure_ascii=False, default=str),
        )
        db.add(item)
        db.flush()
        for relative_path, media_type in normalized.media:
            db.add(MediaFile(
                item_id=item.id,
                relative_path=relative_path,
                media_type=media_type,
                original_name=Path(relative_path).name,
            ))
        count += 1
        if count % 200 == 0:
            db.commit()
    db.commit()

    # Before flagging the batch done, so its items show up in the platform
    # views already deduplicated against the other imports of that platform.
    dedup.finalize_batch(db, batch.id, importer.key)

    batch.item_count = count
    batch.status = ImportStatus.done
    batch.completed_at = datetime.utcnow()
    db.commit()
    notify_platform_changed(importer.key)


def delete_batch_files(batch_id: int) -> None:
    extract_dir = imports_dir() / str(batch_id)
    shutil.rmtree(extract_dir, ignore_errors=True)
    anonymize.delete_artifacts(batch_id)
