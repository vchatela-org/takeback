"""In-process background jobs: sinks pushing data out (app/sinks/). One
daemon thread runs one job at a time, on its schedule or when asked (a
button, the API, the end of an import) -- the same shape as the dedup backfill
thread in app/database.py, and for the same reason: the server must start
listening right away, a slow first run must never hold up a probe.

Assumes a single replica, as the app already does (one uvicorn worker on one
SQLite file): a second process would run every job a second time.

Every run is recorded as a SyncRun (app/models.py): what the UI shows on the
imports page.
"""
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from app import processing
from app.config import settings
from app.database import SessionLocal
from app.models import SyncRun
from app.sinks import influxdb as influx_sink

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Job:
    kind: str
    label: str
    icon: str
    # run(db, **options) -> SyncRun, recording its own outcome.
    run: Callable[..., Any]
    enabled: Callable[[], bool]
    # Seconds until the next scheduled run, from now.
    next_delay: Callable[[], float]
    schedule_label: Callable[[], str]
    # Never at startup itself: the first run waits this long (None: until
    # its schedule says, next_delay()).
    first_delay: Optional[float] = 60.0
    # Options a request may carry (e.g. the platform to push).
    options: Tuple[str, ...] = field(default=())
    # Options of the first scheduled run after a start (e.g. rebuild).
    first_options: Tuple[Tuple[str, Any], ...] = field(default=())
    button: str = "Synchroniser maintenant"


_jobs: Dict[str, Job] = {}
_requests: "queue.Queue[Tuple[str, Tuple]]" = queue.Queue()
_pending: set = set()
_pending_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_interrupted_closed = False


def register(job: Job) -> None:
    _jobs[job.kind] = job


def get(kind: str) -> Optional[Job]:
    return _jobs.get(kind)


def all_jobs() -> List[Job]:
    return list(_jobs.values())


def configured() -> List[Job]:
    return [job for job in _jobs.values() if job.enabled()]


def start() -> bool:
    """Start the thread if any job is configured. Returns whether it runs."""
    global _thread, _interrupted_closed
    jobs = configured()
    if not jobs:
        if not _interrupted_closed:
            # The imports page still shows a sink that ran before: its last
            # run must not stay "running" forever once it is unconfigured.
            # A thread of its own, never holding up the start.
            _interrupted_closed = True
            threading.Thread(target=_close_interrupted_runs, name="scheduler-cleanup", daemon=True).start()
        logger.info("Scheduler: no sink configured, not started")
        return False
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_loop, name="scheduler", daemon=True)
        _thread.start()
        logger.info("Scheduler started: %s", ", ".join(f"{j.label} ({j.schedule_label()})" for j in jobs))
    return True


def request(kind: str, **options: Any) -> bool:
    """Queue a run now (after the current one, if any). A run already
    queued with the same options is not queued twice. False when the job
    is unknown or not configured."""
    job = _jobs.get(kind)
    if job is None or not job.enabled():
        return False
    key = (kind, tuple(sorted((k, v) for k, v in options.items() if k in job.options)))
    with _pending_lock:
        if key in _pending:
            return True
        _pending.add(key)
    _requests.put(key)
    start()
    return True


def _close_interrupted_runs(kind: Optional[str] = None,
                            message: str = "Interrompue par un redémarrage de l'application.") -> None:
    """A run still "running" at startup died with the previous process; one
    of `kind` still "running" after its job raised could not record its end."""
    try:
        with SessionLocal() as db:
            query = db.query(SyncRun).filter(SyncRun.status == "running")
            if kind:
                query = query.filter(SyncRun.kind == kind)
            stale = query.all()
            for run in stale:
                run.status = "error"
                run.message = message
                run.finished_at = run.finished_at or datetime.utcnow()
            if stale:
                db.commit()
    except Exception:
        logger.exception("Scheduler: could not close interrupted runs")


def _run(kind: str, options: Tuple) -> None:
    job = _jobs.get(kind)
    if job is None or not job.enabled():
        return
    started = time.monotonic()
    try:
        with SessionLocal() as db:
            run = job.run(db, **dict(options))
            # Read while the session is open: the run's attributes expire
            # on commit and can't be reloaded once it is closed.
            logger.info("Scheduler: %s %s in %.1fs: %s", job.label, getattr(run, "status", "?"),
                        time.monotonic() - started, getattr(run, "message", ""))
    except Exception as exc:
        logger.exception("Scheduler: %s failed", job.label)
        _close_interrupted_runs(kind, f"Erreur inattendue : {exc}"[:2000])


def _after_run(job: Job, options: Tuple, due: Dict[str, float], started: set) -> None:
    """Restart a job's timer after a run that stands for its scheduled one:
    the first run after a start (`first_options`, e.g. the rebuild), then any
    full run. A request made before the first scheduled run leaves that run
    due, so a push asked for right after a start never cancels the rebuild."""
    if options == job.first_options:
        started.add(job.kind)
    elif options or job.kind not in started:
        return
    due[job.kind] = time.monotonic() + job.next_delay()


def _loop() -> None:
    global _interrupted_closed
    _interrupted_closed = True
    _close_interrupted_runs()
    now = time.monotonic()
    due = {
        job.kind: now + (job.first_delay if job.first_delay is not None else job.next_delay())
        for job in _jobs.values()
    }
    started = set()
    while True:
        enabled = {kind: at for kind, at in due.items() if _jobs[kind].enabled()}
        next_kind = min(enabled, key=enabled.get) if enabled else None
        timeout = max(0.0, enabled[next_kind] - time.monotonic()) if next_kind else None
        try:
            kind, options = _requests.get(timeout=timeout)
            with _pending_lock:
                _pending.discard((kind, options))
        except queue.Empty:
            kind = next_kind
            options = _jobs[kind].first_options if kind not in started else ()
        _run(kind, options)
        _after_run(_jobs[kind], options, due, started)


def _seconds_until_hour(hour: int) -> float:
    """From now to the next HH:00 UTC."""
    now = datetime.utcnow()
    target = now.replace(hour=hour % 24, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


register(Job(
    kind=influx_sink.KIND,
    label="InfluxDB (Grafana)",
    icon="bi-graph-up",
    run=influx_sink.push,
    enabled=influx_sink.is_configured,
    next_delay=lambda: _seconds_until_hour(settings.influxdb_push_hour),
    schedule_label=lambda: (f"au démarrage, après chaque import, et chaque jour à "
                            f"{settings.influxdb_push_hour % 24:02d}:00 UTC"),
    # After a start -- often a new version, whose series may have changed --
    # everything is rebuilt, so no point of an older shape lingers.
    first_delay=120.0,
    first_options=(("rebuild", True),),
    options=("source", "rebuild"),
    button="Envoyer maintenant",
))


def _platform_changed(source: str) -> None:
    """An import, re-analysis or deletion changed a platform: refresh its
    points in InfluxDB (nothing when the sink is off)."""
    request(influx_sink.KIND, source=source)


processing.on_platform_changed.append(_platform_changed)
