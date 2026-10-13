import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, BackgroundTasks
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import crud, dedup, processing, anonymize, limits
from app.database import get_db, uploads_dir, imports_dir, anonymized_dir
from app.schemas import ImportBatchOut

router = APIRouter(prefix="/api/imports", tags=["imports"])


@router.get("/", response_model=List[ImportBatchOut])
def list_imports(db: Session = Depends(get_db)):
    return crud.get_batches(db)


@router.get("/{batch_id}", response_model=ImportBatchOut)
def get_import(batch_id: int, db: Session = Depends(get_db)):
    batch = crud.get_batch(db, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Import introuvable")
    return batch


def _batch_label(filenames: List[str]) -> str:
    if len(filenames) == 1:
        return filenames[0]
    preview = ", ".join(filenames[:3])
    if len(filenames) > 3:
        preview += ", ..."
    return f"{len(filenames)} fichiers ({preview})"


@router.post("/", response_model=ImportBatchOut, status_code=201)
async def create_import(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    if not files or any(not f.filename.lower().endswith(".zip") for f in files):
        raise HTTPException(status_code=400, detail="Seules les archives .zip sont acceptées")

    batch = crud.create_batch(db, original_filename=_batch_label([f.filename for f in files]))
    # Read once, before anything can delete the row: an attribute read on a
    # deleted instance re-loads it and raises ObjectDeletedError.
    batch_id = batch.id

    upload_paths = []
    for file in files:
        upload_path = uploads_dir() / f"{batch_id}-{uuid.uuid4().hex}.zip"
        try:
            await limits.stream_upload(file, upload_path, file.filename)
            limits.check_batch_upload_size([*upload_paths, upload_path])
        except limits.UploadTooLarge as exc:
            crud.delete_batch(db, batch_id)
            processing.delete_batch_files(batch_id)
            raise HTTPException(status_code=413, detail=str(exc))
        upload_paths.append(upload_path)

    background_tasks.add_task(processing.process_import, batch_id, upload_paths)
    return batch


@router.delete("/{batch_id}", status_code=204)
def delete_import(batch_id: int, db: Session = Depends(get_db)):
    # "" for an import never recognized: deleted all the same.
    source = crud.delete_batch(db, batch_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Import introuvable")
    processing.delete_batch_files(batch_id)
    if source:
        # Same as the imports page: the remaining imports' copies take over.
        dedup.refresh_duplicates(db, source)
        processing.notify_platform_changed(source)


@router.post("/{batch_id}/reanalyze", status_code=202)
def reanalyze_import(batch_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Re-run detection+parsing on files already extracted from a previous
    upload, without re-uploading anything."""
    batch = crud.get_batch(db, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Import introuvable")
    extract_dir = imports_dir() / str(batch_id)
    if not extract_dir.exists() or not any(extract_dir.iterdir()):
        raise HTTPException(status_code=400, detail="Aucun fichier extrait à réanalyser pour cet import")
    background_tasks.add_task(processing.reanalyze_import, batch_id)
    return {"status": "started"}


@router.post("/{batch_id}/anonymize", status_code=202)
def anonymize_import(batch_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    batch = crud.get_batch(db, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Import introuvable")
    extract_dir = imports_dir() / str(batch_id)
    if not extract_dir.exists():
        raise HTTPException(status_code=400, detail="Fichiers de l'import introuvables")
    background_tasks.add_task(anonymize.run_anonymize_job, batch_id, extract_dir, batch.source)
    return {"status": "started"}


@router.get("/{batch_id}/anonymize")
def anonymize_state(batch_id: int):
    return {
        "state": anonymize.get_state(batch_id),
        "error": anonymize.get_error_detail(batch_id),
        "warnings": anonymize.get_warnings(batch_id),
    }


@router.get("/{batch_id}/anonymize/download")
def download_anonymized(batch_id: int):
    zip_path = anonymized_dir() / f"{batch_id}.zip"
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Archive anonymisée introuvable")
    return FileResponse(zip_path, filename=f"takeback_anonymized_{batch_id}.zip", media_type="application/zip")
