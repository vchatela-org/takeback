import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# Config is read at import time: point the app at a throwaway database before
# anything imports it, as the other endpoint tests do.
_TMP = tempfile.mkdtemp(prefix="tb-test-")
os.environ["TAKEBACK_DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["TAKEBACK_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import platforms, stats  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ImportBatch, ImportStatus, Item  # noqa: E402


def view(id_, ts, title, minutes, device=None):
    fields = {"Durée vue": f"{minutes} min"}
    if device:
        fields["Appareil"] = device
    return (id_, ts, "prime_video_view", None, title, json.dumps(fields))


ROWS = [
    view(1, datetime(2024, 3, 1, 20, 0), "Show A", 45, "MIBOX3"),
    view(2, datetime(2024, 3, 2, 21, 0), "Show A", 50, "MIBOX3"),
    view(3, datetime(2024, 3, 2, 22, 0), "Film B", 100, "Fire TV Stick"),
    view(4, datetime(2024, 4, 5, 9, 0), "Show A", 30),
    view(5, None, "Film B", 10),
    view(6, datetime(2024, 4, 6), "Film C", 5),  # a date with no time of day
]


def test_dashboard_buckets_minutes_and_counts_undated_views_in_totals():
    axis = stats.build_video_dashboard(ROWS, "month")
    assert len(axis["labels"]) == 2
    assert axis["minutes_series"][0]["data"] == [195.0, 35.0]
    assert axis["views_series"][0]["data"] == [3, 2]
    assert axis["total_views"] == 6
    assert axis["total_minutes"] == 240.0


def test_titles_group_by_title_and_rank_by_time_watched():
    titles = stats.build_video_titles(ROWS)
    assert [(t["label"], t["minutes"], t["views"]) for t in titles] == [
        ("Show A", 125.0, 3),
        ("Film B", 110.0, 2),
        ("Film C", 5.0, 1),
    ]
    assert titles[0]["last"] == datetime(2024, 4, 5, 9, 0)


def test_titles_limit_keeps_the_leaders():
    assert [t["label"] for t in stats.build_video_titles(ROWS, limit=1)] == ["Show A"]


def test_devices_rank_by_time_and_leave_out_views_with_no_device():
    devices = stats.build_video_devices(ROWS)
    assert [(d["label"], d["minutes"], d["views"]) for d in devices] == [
        ("Fire TV Stick", 100.0, 1),
        ("MIBOX3", 95.0, 2),
    ]


def test_heatmap_places_views_by_weekday_and_hour_and_skips_untimed_ones():
    grid = stats.build_video_heatmap(ROWS)
    # 2024-03-01 is a Friday (weekday 4), 2024-03-02 a Saturday (5).
    assert grid[4][20] == 45
    assert grid[5][21] == 50
    assert grid[5][22] == 100
    # 2024-04-06 is midnight with no time of day: it has no hour to sit in.
    assert sum(sum(day) for day in grid) == 45 + 50 + 100 + 30


def test_days_with_a_view_feed_the_streak():
    days = stats.video_days(ROWS)
    assert days[0].isoformat() == "2024-03-01"
    assert stats.longest_streak(days) == {
        "days": 2,
        "start": days[0],
        "end": days[1],
    }


def test_missing_minutes_read_as_zero():
    rows = [(1, datetime(2024, 3, 1), "prime_video_view", None, "Show", None)]
    assert stats.build_video_dashboard(rows, "day")["total_minutes"] == 0.0


def test_video_tab_appears_only_with_prime_video_views():
    keys = [v.key for v in platforms.available_views(["prime_video_view"])]
    assert "video" in keys
    assert "video" not in [v.key for v in platforms.available_views(["order_item"])]


@pytest.fixture(scope="module")
def client():
    Base.metadata.create_all(engine)
    db = SessionLocal()
    batch = ImportBatch(source="amazon", original_filename="export.zip", status=ImportStatus.done)
    db.add(batch)
    db.flush()
    for row in ROWS:
        _id, ts, _item_type, _context, title, fields_json = row
        db.add(Item(batch_id=batch.id, source="amazon", category="Prime Video", item_type="prime_video_view",
                    timestamp=ts, content=title, fields_json=fields_json, is_duplicate=False))
    db.commit()
    db.close()
    return TestClient(app)


def test_video_page_shows_watch_time_titles_and_devices(client):
    response = client.get("/p/amazon/video")
    assert response.status_code == 200
    assert "Temps de visionnage" in response.text
    assert "Show A" in response.text
    assert "Fire TV Stick" in response.text


def test_video_page_redirects_a_platform_without_prime_video_views(client):
    response = client.get("/p/facebook/video", follow_redirects=False)
    assert response.status_code in (302, 307)
