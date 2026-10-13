"""InfluxDB 1.x sink: writes the points app/influx_export.py builds, one
database per domain, for Grafana.

When it runs (app/scheduler.py):
  - two minutes after the application starts (often a new version, whose
    series may have changed): everything rebuilt, so no point of an older
    shape lingers;
  - after an import, a re-analysis or an import deletion,
    for that platform only: its series are dropped, then written again, so
    an item that changed or disappeared leaves no stale point behind;
  - every day at TAKEBACK_INFLUXDB_PUSH_HOUR (UTC), everything, without
    dropping first -- a safety net, rewriting the same points is harmless;
  - on demand: "Envoyer maintenant" (everything), "Reconstruire" (drop all
    of Takeback's series, then everything).
Dropping only ever touches the series tagged via=takeback (and the
platform's): a domain database other trackers write into keeps their data.

Writes go in batches of BATCH_LINES lines, well under InfluxDB 1.x's
default 25 MB request body limit. The databases are created when missing
(TAKEBACK_INFLUXDB_CREATE_DATABASES), with year-long shards, see
create_missing_databases. stdlib urllib, no client library.
"""
import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sqlalchemy.orm import Session

from app import influx_export, platforms
from app.templating import number
from app.config import settings
from app.models import SyncRun

logger = logging.getLogger(__name__)

KIND = "influxdb"
BATCH_LINES = 5000
TIMEOUT_SECONDS = 60
SHARD_DURATION = "52w"
# A 400 whose body says some points of the batch were dropped and the others
# written: older than the database's retention, or a field type conflict.
_PARTIAL_WRITE = re.compile(r"partial write: (?P<reason>.*?)\s*dropped=(?P<dropped>\d+)")


class SinkError(Exception):
    """A push that could not complete, with a message meant for the UI."""


def is_configured() -> bool:
    return bool(settings.influxdb_url)


def _post(path: str, params: Dict[str, str], body: bytes = b"") -> bytes:
    url = settings.influxdb_url.rstrip("/") + path + "?" + urlencode(params)
    request = Request(url, data=body, method="POST", headers={"Content-Type": "text/plain; charset=utf-8"})
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()[:500]
        raise SinkError(f"InfluxDB a refusé {path} ({exc.code}) : {detail}") from exc
    except URLError as exc:
        raise SinkError(f"InfluxDB injoignable ({settings.influxdb_url}) : {exc.reason}") from exc
    except TimeoutError as exc:
        raise SinkError(f"InfluxDB ne répond pas ({settings.influxdb_url})") from exc


def query(statement: str, database: Optional[str] = None) -> Dict:
    """Run one InfluxQL statement. /query answers 200 even when the
    statement fails: the error is in the body."""
    params = {"q": statement}
    if database:
        params["db"] = database
    payload = json.loads(_post("/query", params) or b"{}")
    for result in payload.get("results") or []:
        if result.get("error"):
            raise SinkError(f"InfluxDB : {result['error']} ({statement})")
    return payload


def create_missing_databases(names: List[str]) -> List[str]:
    """Create the databases that don't exist yet, with year-long shards:
    InfluxDB's default for a database kept forever is one shard per week,
    and Takeback's history spans years -- 600+ mostly tiny shards for the
    first push of a modest import, each costing memory and file handles for
    good. An existing database is left exactly as it is."""
    payload = query("SHOW DATABASES")
    existing = {
        row[0] for result in payload.get("results") or [] for series in result.get("series") or []
        for row in series.get("values") or []
    }
    created = [name for name in names if name not in existing]
    for name in created:
        query(f'CREATE DATABASE "{name}" WITH SHARD DURATION {SHARD_DURATION}')
    return created


def write(database: str, lines: List[str]) -> Tuple[int, Optional[str]]:
    """(points InfluxDB dropped, why -- None when only for being older than
    the database's retention, which is expected). A partial write keeps the
    batch's other points, so the push goes on rather than leave everything
    after it unwritten; any other refusal raises."""
    try:
        _post("/write", {"db": database, "precision": "s"}, "\n".join(lines).encode("utf-8"))
    except SinkError as exc:
        match = _PARTIAL_WRITE.search(str(exc))
        if not match:
            raise
        reason = match["reason"].rstrip(": ")
        return int(match["dropped"]), None if "beyond retention policy" in reason else reason
    return 0, None


def _quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def drop(database: str, platform: Optional[str] = None) -> None:
    """Takeback's series of one database, or of one platform only."""
    statement = f"DROP SERIES WHERE \"via\" = {_quote(influx_export.VIA)}"
    if platform:
        statement += f" AND \"platform\" = {_quote(platform)}"
    query(statement, database)


def push(db: Session, source: Optional[str] = None, rebuild: bool = False) -> SyncRun:
    """One push, recorded as a SyncRun whatever happens. `source`: that
    platform only, its series rebuilt; `rebuild`: drop every Takeback
    series first, then push everything (a `source` is then ignored)."""
    if rebuild:
        source = None
    run = SyncRun(kind=KIND, status="running")
    db.add(run)
    db.commit()
    try:
        if not is_configured():
            raise SinkError("Envoi non configuré : TAKEBACK_INFLUXDB_URL.")
        databases = influx_export.databases()
        if settings.influxdb_create_databases:
            create_missing_databases(databases)
        if source or rebuild:
            for database in databases:
                drop(database, source)

        written: Counter = Counter()
        expired = 0
        refused: Counter = Counter()  # reason -> points

        def flush(database: str, buffer: List[str]) -> None:
            nonlocal expired
            dropped, reason = write(database, buffer)
            written[database] += len(buffer) - dropped
            if reason:
                refused[f"{database} : {reason}"] += dropped
            else:
                expired += dropped
            buffer.clear()

        buffers: Dict[str, List[str]] = defaultdict(list)
        for database, line in influx_export.export(db, [source] if source else None):
            buffer = buffers[database]
            buffer.append(line)
            if len(buffer) >= BATCH_LINES:
                flush(database, buffer)
        for database, buffer in buffers.items():
            if buffer:
                flush(database, buffer)

        total = sum(written.values())
        detail = ", ".join(f"{database} {number(n)}" for database, n in sorted(written.items()))
        scope = f" ({platforms.get(source).label}, reconstruit)" if source else (" (tout reconstruit)" if rebuild else "")
        run.message = f"{number(total)} points écrits{scope}" + (f" : {detail}." if detail else ".")
        if expired:
            run.message += f" {number(expired)} plus anciens que la rétention de leur base, ignorés."
        for reason, dropped in refused.items():
            run.message += f" {number(dropped)} refusés ({reason})."
        run.status = "error" if refused else "ok"
        run.stats_json = json.dumps(dict(written))
    except SinkError as exc:
        run.status, run.message = "error", str(exc)
        logger.warning("InfluxDB push failed: %s", exc)
    except Exception as exc:  # noqa: BLE001 -- recorded for the UI, never fatal to the scheduler
        logger.exception("InfluxDB push failed")
        db.rollback()
        run.status, run.message = "error", f"Erreur inattendue : {exc}"[:2000]
    finally:
        run.finished_at = datetime.utcnow()
        db.add(run)
        db.commit()
    return run

