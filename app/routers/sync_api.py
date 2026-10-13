from itertools import islice
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app import crud, influx_export, scheduler
from app.database import get_db
from app.schemas import SyncRunOut

router = APIRouter(prefix="/api/sync", tags=["sync"])


@router.get("/runs", response_model=List[SyncRunOut])
def list_runs(kind: Optional[str] = None, limit: int = Query(20, ge=1, le=200), db: Session = Depends(get_db)):
    """The latest sink runs, most recent first."""
    return crud.get_sync_runs(db, kind, limit)


@router.get("/influxdb/preview", response_class=PlainTextResponse)
def influxdb_preview(
    measurement: Optional[str] = None, source: Optional[str] = None,
    limit: int = Query(50, ge=1, le=5000), db: Session = Depends(get_db),
):
    """The line-protocol points the InfluxDB sink would write (oldest
    first), each after a `# <database>` line -- to check a series of
    app/influx_export.py before pushing it. Works without InfluxDB."""
    if measurement and measurement not in {s.measurement for s in influx_export.SERIES}:
        raise HTTPException(status_code=404, detail="Mesure inconnue")
    lines = islice(influx_export.export(db, [source] if source else None, measurement), limit)
    return "".join(f"# {database}\n{line}\n" for database, line in lines)


@router.post("/{kind}", status_code=202)
def request_run(kind: str, source: Optional[str] = None, rebuild: bool = False):
    """Queue a run of a configured sink right away; it runs in the
    background, see GET /api/sync/runs. `influxdb`: a push of everything, or of one platform (`source`, its
    series rebuilt), or a full rebuild (`rebuild=true`, every platform, even
    with a `source`)."""
    job = scheduler.get(kind)
    if job is None:
        raise HTTPException(status_code=404, detail="Synchronisation inconnue")
    # A rebuild covers every platform: a `source` would only narrow its name.
    wanted = (("rebuild", True),) if rebuild else (("source", source),)
    options = {k: v for k, v in wanted if v and k in job.options}
    if not scheduler.request(kind, **options):
        raise HTTPException(status_code=409, detail="Synchronisation non configurée")
    return {"status": "queued", "kind": kind, **options}
