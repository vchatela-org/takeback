import json
import re
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import influx_export, processing, scheduler  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402,F401  -- scheduler off: nothing configured at import
from app.sinks import influxdb as sink  # noqa: E402

SOURCE = "influxtest"
SECRETS = ("secret message text", "secret query", "Secret product")


def epoch(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


@pytest.fixture(scope="module")
def db():
    from app.database import Base, SessionLocal, engine
    from app.models import ImportBatch, ImportStatus, Item

    Base.metadata.create_all(engine)
    session = SessionLocal()
    batch = ImportBatch(original_filename="synthetic.zip", source=SOURCE, status=ImportStatus.done)
    session.add(batch)
    session.commit()

    def add(item_type, ts, category=None, context=None, content=None, fields=None, duplicate=False):
        session.add(Item(
            batch_id=batch.id, source=SOURCE, item_type=item_type, category=category, timestamp=ts,
            context=context, content=content, fields_json=json.dumps(fields) if fields else None,
            is_duplicate=duplicate,
        ))

    # Two messages in the same second (microseconds apart), a search.
    add("message", datetime(2025, 3, 1, 20, 0, 5, 123), "messages", "Alice", SECRETS[0])
    add("message", datetime(2025, 3, 1, 20, 0, 5, 900), "messages", "Alice", SECRETS[0])
    add("search", datetime(2025, 3, 1, 21, 0, 0, 1), "Recherches", None, SECRETS[1])
    # One order of two lines and another order, placed the same second.
    for context, department in (("ORDER-1", "Kitchen"), ("ORDER-1", "Kitchen"), ("ORDER-2", "Kitchen"),
                                ("ORDER-1", "Books")):
        add("order_item", datetime(2025, 3, 2, 10, 0, 0), "Commandes", context, SECRETS[2],
            {"Montant": "10.00 EUR", "Département": department})
    # An order known by its day only.
    add("order_item", datetime(2025, 3, 3), "Commandes", "ORDER-3", SECRETS[2], {"Montant": "5.50 EUR"})
    add("reading_session", datetime(2025, 3, 1, 21, 30, 0), "Kindle", "Penser vite", "Penser vite",
        {"Durée de lecture": "30.0 min", "Pages tournées": "12", "Appareil": "Kindle E-reader …G0"})
    add("listening_session", datetime(2025, 3, 4, 8, 0, 0), "Audible", "Le Livre audio", "Le Livre audio",
        {"Durée d'écoute": "20.0 min"})
    add("kindle_finished", datetime(2025, 3, 5, 22, 0, 0), "Kindle", "Dune", "Dune")
    # Left out: no real date, a duplicate of another import's copy.
    add("message", datetime(1970, 1, 1), "messages", "Alice", SECRETS[0])
    add("search", datetime(2025, 3, 6, 9, 0, 0), "Recherches", None, SECRETS[1], duplicate=True)
    session.commit()
    yield session
    session.close()


@pytest.fixture()
def lines(db):
    return list(influx_export.export(db, [SOURCE]))


def _unescape(value):
    return re.sub(r"\\(.)", r"\1", value)


def parse(line):
    """'m,t=v f=1i 123' -> (measurement, tags, fields, ts), tag values
    unescaped, field values as written (strings keep their quotes)."""
    head, fields, ts = re.match(r'^((?:[^ \\]|\\.)+) (.*) (\d+)$', line).groups()
    measurement, *tags = re.split(r"(?<!\\),", head)
    tag_values = dict(map(_unescape, re.split(r"(?<!\\)=", t, maxsplit=1)) for t in tags)
    field_values = dict(re.findall(r'(\w+)=("(?:[^"\\]|\\.)*"|[^,]+)', fields))
    return _unescape(measurement), tag_values, field_values, int(ts)


def points(lines, measurement):
    return [parse(line) for database, line in lines if parse(line)[0] == measurement]


# -- Line protocol ------------------------------------------------------------------

def test_line_protocol_escaping_and_declared_fields():
    line = influx_export.format_line(
        "my measure", {"b key": "a,b=c d", "empty": "", "none": None},
        {"title": 'Le "titre" \\ fin', "count": 2, "minutes": 1.5}, {"title": str, "count": int, "minutes": float},
        1700000000,
    )
    assert line == 'my\\ measure,b\\ key=a\\,b\\=c\\ d count=2i,minutes=1.5,title="Le \\"titre\\" \\\\ fin" 1700000000'
    with pytest.raises(ValueError, match="not declared"):
        influx_export.format_line("m", {}, {"oops": 1}, {}, 0)


def test_line_breaks_never_end_a_line():
    line = influx_export.format_line("m", {"department": "Livres\nAnglais"}, {"title": "a\r\nb"},
                                     {"title": str}, 1)
    assert line == 'm,department=Livres\\ Anglais title="a  b" 1'


def test_every_field_type_is_stable():
    """InfluxDB 1.x rejects a field whose type changes: one type per field
    name within a measurement, whatever the series."""
    seen = {}
    for series in influx_export.SERIES:
        for name, kind in {**influx_export.COMMON_FIELDS, **series.field_types}.items():
            assert kind in (int, float, str)
            assert seen.setdefault((series.database, series.measurement, name), kind) is kind


# -- Privacy ------------------------------------------------------------------------

ALLOWED_TEXT = {("MediaStats", "reading_session", "title"), ("MediaStats", "book_finished", "title")}


def test_only_opted_in_titles_leave_as_text(lines):
    declared = {
        (s.database, s.measurement, name) for s in influx_export.SERIES
        for name, kind in s.field_types.items() if kind is str
    }
    assert declared == ALLOWED_TEXT
    written = {
        (database, parse(line)[0], name) for database, line in lines
        for name, value in parse(line)[2].items() if value.startswith('"')
    }
    assert written <= ALLOWED_TEXT
    text = "\n".join(line for _db, line in lines)
    for secret in SECRETS + ("Alice", "ORDER-1"):
        assert secret not in text


# -- Series -------------------------------------------------------------------------

def test_counts_cover_every_item_type_once_per_second(lines):
    items = points(lines, "item")
    assert {p[1]["item_type"] for p in items} == {
        "message", "search", "order_item", "reading_session", "listening_session", "kindle_finished",
    }
    message, = [p for p in items if p[1]["item_type"] == "message"]
    _m, tags, fields, ts = message
    assert ts == epoch(2025, 3, 1, 20, 0, 5)
    assert fields["count"] == "2i"
    assert tags == {"source": SOURCE, "platform": SOURCE, "category": "messages", "item_type": "message",
                    "via": "takeback", "time_precision": "second"}
    # 20:00 UTC is 21:00 in Paris in March; a Saturday.
    assert (fields["local_hour"], fields["local_weekday"]) == ("21i", "6i")
    assert all(database == "DigitalStats" for database, line in lines if line.startswith("item,"))


def test_purchases_summed_per_second_with_distinct_orders(lines):
    purchases = sorted(points(lines, "purchase"), key=lambda p: (p[3], p[1].get("department")))
    books, kitchen, dated = purchases
    _m, tags, fields, ts = kitchen
    assert ts == epoch(2025, 3, 2, 10, 0, 0)
    assert (tags["kind"], tags["department"], tags["currency"], tags["source"]) == ("retail", "Kitchen", "EUR", SOURCE)
    assert (fields["amount"], fields["lines"], fields["orders"]) == ("30.0", "3i", "2i")
    # ORDER-1 also has a line in Books: counted once, where it came first.
    assert (books[1]["department"], books[2]["amount"], books[2]["lines"], books[2]["orders"]) == (
        "Books", "10.0", "1i", "0i")
    assert sum(int(p[2]["orders"][:-1]) for p in purchases) == 3
    # Known by its day only: no hour to give.
    assert dated[1]["time_precision"] == "day" and "local_hour" not in dated[2]
    assert dated[1]["department"] == "Sans département"


def test_reading_sessions_and_finished_books(lines):
    reading = {p[1]["source"]: p for p in points(lines, "reading_session")}
    kindle = reading["kindle"]
    assert (kindle[1]["kind"], kindle[1]["device"]) == ("ebook", "Kindle E-reader …G0")
    assert (kindle[2]["minutes"], kindle[2]["sessions"], kindle[2]["pages"], kindle[2]["title"]) == (
        "30.0", "1i", "12i", '"Penser vite"')
    audible = reading["audible"]
    assert (audible[1]["kind"], audible[2]["minutes"], audible[2]["title"]) == ("audiobook", "20.0", '"Le Livre audio"')
    assert "pages" not in audible[2] and "device" not in audible[1]
    finished, = points(lines, "book_finished")
    assert (finished[1]["source"], finished[1]["kind"], finished[2]["title"]) == ("kindle", "ebook", '"Dune"')
    assert {database for database, line in lines if line.startswith(("reading_session", "book_finished"))} == {"MediaStats"}


def test_user_tag_when_configured(db, monkeypatch):
    monkeypatch.setattr(settings, "influxdb_user", "someone")
    line = next(line for _db, line in influx_export.export(db, [SOURCE], "book_finished"))
    assert ",user=someone," in line


def test_preview_endpoint(db):
    from fastapi.testclient import TestClient

    client = TestClient(app)
    text = client.get("/api/sync/influxdb/preview", params={"source": SOURCE, "measurement": "purchase"}).text
    assert text.startswith("# ShoppingStats\npurchase,")
    assert client.get("/api/sync/influxdb/preview", params={"measurement": "nope"}).status_code == 404
    assert client.post("/api/sync/influxdb").status_code == 409  # not configured


# -- Push ---------------------------------------------------------------------------

class FakeInflux:
    """Records /query statements and /write bodies; `fail` makes a path answer an error."""

    def __init__(self):
        self.queries, self.writes, self.fail = [], [], {}
        self.databases = {"_internal"}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                url = urlparse(self.path)
                params = {k: v[0] for k, v in parse_qs(url.query).items()}
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                if url.path in outer.fail:
                    code, payload = outer.fail[url.path]
                    self.send_response(code)
                    self.end_headers()
                    self.wfile.write(payload.encode())
                    return
                if url.path == "/query":
                    statement = params["q"]
                    outer.queries.append((params.get("db"), statement))
                    result = {"statement_id": 0}
                    if statement == "SHOW DATABASES":
                        result["series"] = [{"name": "databases", "columns": ["name"],
                                             "values": [[name] for name in sorted(outer.databases)]}]
                    elif statement.startswith("CREATE DATABASE"):
                        outer.databases.add(statement.split('"')[1])
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(json.dumps({"results": [result]}).encode())
                else:
                    outer.writes.append((params["db"], params["precision"], body.split("\n")))
                    self.send_response(204)
                    self.end_headers()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"


@pytest.fixture()
def influx(monkeypatch):
    fake = FakeInflux()
    monkeypatch.setattr(settings, "influxdb_url", fake.url)
    # Requests from the platform-changed hook are not run by a live thread here.
    monkeypatch.setattr(scheduler, "start", lambda: True)
    yield fake
    fake.server.shutdown()
    while not scheduler._requests.empty():
        scheduler._requests.get_nowait()
    scheduler._pending.clear()


def test_push_one_platform_rebuilds_it_in_batches(db, influx, monkeypatch):
    monkeypatch.setattr(sink, "BATCH_LINES", 2)
    run = sink.push(db, source=SOURCE)
    assert run.status == "ok", run.message
    # Created with year-long shards, not one shard per week of history.
    assert [q for _db, q in influx.queries if q.startswith("CREATE")] == [
        f'CREATE DATABASE "{name}" WITH SHARD DURATION 52w' for name in ("DigitalStats", "MediaStats", "ShoppingStats")
    ]
    drops = [(database, q) for database, q in influx.queries if q.startswith("DROP")]
    assert drops == [
        (database, f"DROP SERIES WHERE \"via\" = 'takeback' AND \"platform\" = '{SOURCE}'")
        for database in ("DigitalStats", "MediaStats", "ShoppingStats")
    ]
    assert all(len(body) <= 2 and precision == "s" for _db, precision, body in influx.writes)
    written = {}
    for database, _p, body in influx.writes:
        written[database] = written.get(database, 0) + len(body)
    # One count per (type, second): the two messages of one second, the
    # three lines of the 10:00 orders are one point each.
    assert written == {"DigitalStats": 7, "MediaStats": 3, "ShoppingStats": 3}
    assert run.message == "13 points écrits (Influxtest, reconstruit) : DigitalStats 7, MediaStats 3, ShoppingStats 3."


def test_existing_databases_are_left_alone(db, influx):
    influx.databases.update({"MediaStats", "DigitalStats"})  # e.g. created by hand
    sink.push(db, source=SOURCE)
    assert [q for _db, q in influx.queries if q.startswith("CREATE")] == [
        'CREATE DATABASE "ShoppingStats" WITH SHARD DURATION 52w',
    ]


def test_full_rebuild_drops_only_takeback_series(db, influx):
    sink.push(db, rebuild=True)
    drops = {q for _db, q in influx.queries if q.startswith("DROP")}
    assert drops == {"DROP SERIES WHERE \"via\" = 'takeback'"}
    influx.queries.clear()
    sink.push(db)  # the daily push: nothing dropped
    assert not [q for _db, q in influx.queries if q.startswith("DROP")]


def test_push_errors_are_recorded(db, influx):
    influx.fail["/write"] = (400, '{"error":"field type conflict"}')
    run = sink.push(db, source=SOURCE)
    assert run.status == "error" and "field type conflict" in run.message
    influx.fail = {"/query": (200, '{"results":[{"error":"database not found: x"}]}')}
    run = sink.push(db, source=SOURCE)
    assert run.status == "error" and "database not found" in run.message


def test_an_import_refreshes_its_platform(influx):
    processing.notify_platform_changed(SOURCE)
    assert scheduler._requests.get_nowait() == ("influxdb", (("source", SOURCE),))


def test_nothing_queued_when_the_sink_is_off(monkeypatch):
    monkeypatch.setattr(settings, "influxdb_url", None)
    processing.notify_platform_changed(SOURCE)
    assert scheduler._requests.empty()


# -- Scheduler ----------------------------------------------------------------------

def test_scheduler_off_without_configuration(monkeypatch):
    monkeypatch.setattr(settings, "influxdb_url", None)
    assert scheduler.configured() == []
    assert scheduler.request("influxdb") is False


def test_sync_button_returns_to_its_page_whatever_the_host(monkeypatch):
    from fastapi.testclient import TestClient

    queued = []
    monkeypatch.setattr(scheduler, "request", lambda kind, **options: queued.append((kind, options)) or True)
    client = TestClient(app)
    # Behind the proxy the browser's host is not the app's own.
    response = client.post("/sync/influxdb", follow_redirects=False,
                           headers={"referer": "https://takeback.example/imports?tab=all"})
    assert response.headers["location"] == "/imports?tab=all"
    response = client.post("/sync/influxdb?rebuild=true", follow_redirects=False,
                           headers={"referer": "https://evil.example"})
    assert response.headers["location"] == "/imports"
    assert queued == [("influxdb", {}), ("influxdb", {"rebuild": True})]
    assert client.post("/sync/unknown").status_code == 404


# -- Titles and orders ----------------------------------------------------------------

TITLES_SOURCE = "influxtitles"


@pytest.fixture(scope="module")
def titles_db(db):
    from app.models import ImportBatch, ImportStatus, Item

    batch = ImportBatch(original_filename="titles.zip", source=TITLES_SOURCE, status=ImportStatus.done)
    db.add(batch)
    db.commit()

    def add(item_type, ts, context, fields, content=None):
        db.add(Item(batch_id=batch.id, source=TITLES_SOURCE, item_type=item_type, timestamp=ts, context=context,
                    content=content, fields_json=json.dumps(fields), is_duplicate=False))

    # A book the Kindle files don't name: its order does.
    add("order_item", datetime(2025, 4, 1, 9, 0, 0), "ORDER-9", {"Montant": "5.00 EUR", "ASIN": "B00TEST001"},
        "Le Vrai Titre")
    add("reading_session", datetime(2025, 4, 2, 21, 0, 0), "B00TEST001",
        {"Durée de lecture": "10.0 min", "ASIN": "B00TEST001"})
    # A personal document, named in Takeback, never in InfluxDB.
    add("reading_session", datetime(2025, 4, 3, 21, 0, 0), "Bulletin de salaire secret",
        {"Durée de lecture": "5.0 min", "Document": "0123456789abcdef0123456789abcdef", "Type": "Document personnel"})
    add("kindle_finished", datetime(2025, 4, 4), "Bulletin de salaire secret",
        {"Document": "0123456789abcdef0123456789abcdef"})
    # Two purchase lines without an order id: two orders, as on the purchases page.
    for hour in (10, 11):
        add("order_item", datetime(2025, 4, 5, hour, 0, 0), None, {"Montant": "3.00 EUR"}, "Sans commande")
    db.commit()
    return db


def test_titles_resolved_and_personal_documents_kept_private(titles_db):
    lines = list(influx_export.export(titles_db, [TITLES_SOURCE]))
    titles = {parse(line)[2]["title"] for _db, line in lines if line.startswith(("reading_session", "book_finished"))}
    assert titles == {'"Le Vrai Titre"', f'"{influx_export.PERSONAL_DOCUMENT_TITLE}"'}
    assert "secret" not in "\n".join(line for _db, line in lines)


def test_purchase_lines_without_an_order_count_as_orders(titles_db):
    orders = [int(p[2]["orders"][:-1]) for p in points(list(influx_export.export(titles_db, [TITLES_SOURCE])),
                                                        "purchase")]
    assert sum(orders) == 3


# -- Partial writes and rebuild scope --------------------------------------------------

def test_points_beyond_retention_are_skipped_not_fatal(db, influx):
    influx.fail["/write"] = (400, '{"error":"partial write: points beyond retention policy dropped=1"}')
    run = sink.push(db, source=SOURCE)
    assert run.status == "ok", run.message
    assert run.message.startswith("10 points écrits") and "3 plus anciens que la rétention" in run.message


def test_a_field_type_conflict_keeps_the_push_going(db, influx):
    influx.fail["/write"] = (400, '{"error":"partial write: field type conflict: input field \\"amount\\" '
                                  'on measurement \\"purchase\\" is type integer, already exists as type float dropped=1"}')
    run = sink.push(db, source=SOURCE)
    assert run.status == "error"
    # Every database was still written, one point refused in each.
    assert run.message.startswith("10 points écrits") and "refusés (ShoppingStats : field type conflict" in run.message


def test_a_rebuild_covers_every_platform_even_with_a_source(db, influx):
    sink.push(db, source=SOURCE, rebuild=True)
    assert {q for _db, q in influx.queries if q.startswith("DROP")} == {"DROP SERIES WHERE \"via\" = 'takeback'"}


# -- Scheduler timing and failures ------------------------------------------------------

def test_a_push_asked_right_after_a_start_keeps_the_rebuild_due():
    job = scheduler.get("influxdb")
    due, started = {"influxdb": 120.0}, set()
    scheduler._after_run(job, (), due, started)  # "Envoyer maintenant" within two minutes
    scheduler._after_run(job, (("source", SOURCE),), due, started)  # an import
    assert due == {"influxdb": 120.0} and not started
    scheduler._after_run(job, job.first_options, due, started)  # the rebuild, scheduled or clicked
    assert started == {"influxdb"} and due["influxdb"] > 120.0
    due["influxdb"] = 0.0
    scheduler._after_run(job, (), due, started)  # now a full push stands for the daily one
    assert due["influxdb"] > 0.0


def test_a_job_that_raises_does_not_stay_running(monkeypatch):
    from app.database import SessionLocal
    from app.models import SyncRun

    def boom(db, **options):
        db.add(SyncRun(kind="boom", status="running"))
        db.commit()
        raise RuntimeError("database is locked")

    scheduler.register(scheduler.Job(kind="boom", label="Boom", icon="", run=boom, enabled=lambda: True,
                                     next_delay=lambda: 60.0, schedule_label=lambda: ""))
    try:
        scheduler._run("boom", ())
    finally:
        scheduler._jobs.pop("boom")
    with SessionLocal() as session:
        run = session.query(SyncRun).filter(SyncRun.kind == "boom").one()
        assert run.status == "error" and "database is locked" in run.message


def test_sync_button_never_redirects_off_site(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(scheduler, "request", lambda kind, **options: True)
    client = TestClient(app)
    for referer in ("https://x//evil.example/phish", "https://x/\\evil.example"):
        response = client.post("/sync/influxdb", follow_redirects=False, headers={"referer": referer})
        assert response.headers["location"] == "/imports", referer


# -- Re-analysis -----------------------------------------------------------------------

def test_a_reanalysis_that_loses_its_platform_refreshes_it(db, monkeypatch):
    from app.database import imports_dir
    from app.models import ImportBatch, ImportStatus

    batch = ImportBatch(original_filename="gone.zip", source="influxgone", status=ImportStatus.done)
    db.add(batch)
    db.commit()
    folder = imports_dir() / str(batch.id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "unknown.txt").write_text("not an export")
    notified = []
    monkeypatch.setattr(processing, "notify_platform_changed", notified.append)
    processing.reanalyze_import(batch.id)
    db.refresh(batch)
    assert batch.status == ImportStatus.unrecognized
    assert notified == ["influxgone"]
