import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# Config is read at import time: point the app at a throwaway database and
# data dir before anything imports it, so no test touches ./data.
_TMP = tempfile.mkdtemp(prefix="tb-test-")
os.environ["TAKEBACK_DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["TAKEBACK_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ImportBatch, ImportStatus, Item  # noqa: E402


@pytest.fixture(scope="module")
def client():
    Base.metadata.create_all(engine)
    db = SessionLocal()
    batch = ImportBatch(source="facebook", original_filename="export.zip", status=ImportStatus.done)
    db.add(batch)
    db.flush()

    def message(ts, author, context):
        db.add(Item(batch_id=batch.id, source="facebook", category="messages", item_type="message",
                    timestamp=ts, author=author, context=context, content="hi", is_duplicate=False))

    for hour in range(3):
        message(datetime(2024, 3, 1, 10 + hour), "Me", "Alice")
    message(datetime(2024, 3, 2, 9), "Alice", "Alice")
    message(datetime(2024, 3, 3, 9), "Me", "Bob")
    db.commit()
    db.close()
    return TestClient(app)


PERIOD = {"date_from": "2024-03-01", "date_to": "2024-03-31"}


def test_overview_lists_conversations_busiest_first(client):
    html = client.get("/p/facebook/messaging/panel", params={"overview": 1, **PERIOD}).text
    assert html.index("<strong>Alice</strong>") < html.index("<strong>Bob</strong>")
    assert "3 envoyés · 1 reçu" in html
    assert "80,0 %" in html  # Alice: 4 of the 5 messages of the period


def test_overview_rows_open_their_conversation_with_the_period(client):
    html = client.get("/p/facebook/messaging/panel", params={"overview": 1, **PERIOD}).text
    assert 'data-drill-url="/p/facebook/messaging/panel?date_from=2024-03-01&amp;date_to=2024-03-31&amp;context=Alice"' in html


def test_overview_direction_keeps_only_that_side(client):
    html = client.get("/p/facebook/messaging/panel",
                      params={"overview": 1, "direction": "received", **PERIOD}).text
    assert "<strong>Alice</strong>" in html
    assert "<strong>Bob</strong>" not in html


def test_messages_page_wires_the_chart_to_the_overview(client):
    html = client.get("/p/facebook/messaging", params={**PERIOD, "granularity": "day"})
    assert html.status_code == 200
    assert "overview: 1" in html.text
    assert "select: function (first, last)" in html.text


def test_empty_period_says_so(client):
    html = client.get("/p/facebook/messaging/panel",
                      params={"overview": 1, "date_from": "2000-01-01", "date_to": "2000-01-31"}).text
    assert "Aucune conversation sur cette période." in html
