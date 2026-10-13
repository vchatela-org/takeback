import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import dedup, importers, platforms, stats  # noqa: E402
from app.importers import amazon  # noqa: E402

DOC_A = "QWERTYUIOPASDFGHJKLZXCVBNMQWERTY"
DOC_B = "MNBVCXZLKJHGFDSAPOIUYTREWQMNBVCX"
SESSION_HEADER = (
    "start_timestamp,end_timestamp,ASIN,purchased_marketplace,preferred_marketplace,device_family,"
    "device_serial_number,device_software_version,content_type,total_reading_millis,number_of_page_flips\n"
)
WHISPERSYNC_HEADER = (
    "ASIN,Non ASIN,Product Name,Annotation Type,Customer modified date on device,Device Serial Number,"
    "Third Party Device,ContentType,Format,Creation Date,LastUpdatedDate,Highlight,Version,Selection Type,Note,"
    "Is Deleted\n"
)
YELLOW_HIGHLIGHT = (
    "B0TESTBOOK,Not Available,Penser vite,kindle.highlight,2025-03-03T21:05:00Z,G0,Not Available,EBOK,YJBinary,"
    "2025-03-03T21:10:00Z,2025-03-03T21:10:00Z,{mchl_color:yellow},Not Available,Not Available,Not Available,No\n"
)


def _right(origin, status, acquired):
    return {
        "rightType": "Download", "origin": {"originType": origin}, "rightStatus": status, "acquiredDate": acquired,
        "consumptions": [{"resourceItemType": "Content", "targetEntity": {"entityType": "Device"}}],
    }


OWNERSHIP = [
    {"resource": {"resourceType": "KindleEBook", "catalog": "Amazon", "ASIN": "B0TESTBOOK", "Product Name": "Penser vite"},
     "rights": [_right("Purchase", "Revoked", "2024-12-01T10:00:00.000Z"),
                _right("Purchase", "Active", "2025-01-10T10:00:00.000Z")]},
    # Listed again, as the real export does for every book.
    {"resource": {"resourceType": "KindleEBook", "catalog": "Amazon", "ASIN": "B0TESTBOOK", "Product Name": "Penser vite"},
     "rights": [_right("Purchase", "Active", "2025-01-10T10:00:00.000Z")]},
    {"resource": {"resourceType": "KindleEBook", "catalog": "Amazon", "ASIN": "B0OTHERBK1", "Product Name": "Not Available"},
     "rights": [_right("Purchase", "Revoked", "2024-06-01T10:00:00.000Z")]},
    {"resource": {"resourceType": "KindleEBook", "catalog": "Amazon", "ASIN": "B0DICTIONA", "Product Name": "Dictionnaire"},
     "rights": [_right("KindleDictionary", "Active", "2023-01-01T10:00:00.000Z")]},
    {"resource": {"resourceType": "KindlePDoc", "catalog": "Amazon", "Product Name": "Not Available"},
     "rights": [_right("PDocs", "Active", "2025-02-20T10:00:00.000Z")]},
]

# A made-up Kindle-only export ("Kindle.zip") in the shapes of a real one.
FILES = {
    "Kindle.Devices.ReadingSession/Kindle.Devices.ReadingSession.csv": SESSION_HEADER + (
        f"2025-03-01T20:10:00Z,2025-03-01T20:40:00Z,{DOC_A},www.amazon.fr,www.amazon.fr,Kindle E-reader,G0,1,Personal Document,1800000,30\n"
        f"2025-03-02T21:00:00Z,2025-03-02T21:15:00Z,{DOC_A},www.amazon.fr,www.amazon.fr,Kindle E-reader,G0,1,Personal Document,900000,12\n"
        "2025-03-03T21:00:00Z,2025-03-03T21:10:00Z,B0TESTBOOK,www.amazon.fr,www.amazon.fr,Kindle E-reader,G0,1,E-Book,600000,8\n"
        "2025-03-05T08:00:00Z,2025-03-05T08:05:00Z,B0TESTBOOK,www.amazon.fr,www.amazon.fr,,G0,1,E-Book,300000,4\n"
        # Only an end: 1 133 of the real rows look like this.
        "Not Available,2025-03-04T10:00:00Z,B0TESTBOOK,www.amazon.fr,www.amazon.fr,Kindle Cloud Reader,A1,,E-Book,,\n"
    ),
    "Kindle.ReadingInsights/datasets/Kindle.reading-insights-sessions_with_adjustments/"
    "Kindle.reading-insights-sessions_with_adjustments.csv": (
        "ASIN,end_time,personal_document_id,product_name,reading_marketplace,start_time,total_reading_milliseconds\n"
        f"Not Available,2025-03-01T20:40:00Z,{DOC_A},Un roman,www.amazon.fr,2025-03-01T20:10:00Z,1800000\n"
        "B0TESTBOOK,2025-03-03T21:10:00Z,Not Available,Penser vite,www.amazon.fr,2025-03-03T21:00:00Z,600000\n"
    ),
    "Kindle.ReadingInsights/datasets/Kindle.UserUniqueTitlesCompleted/Kindle.UserUniqueTitlesCompleted.csv": (
        "asin_date_and_content_type,personal_document_id,product_name\n"
        f"Not Applicable_2025-03-02_AUTOMATIC,{DOC_A},Un roman\n"
        "B0TESTBOOK_2025-03-06_MANUAL,Not Applicable,Penser vite\n"
        "pas une clé,,Autre\n"
    ),
    "Kindle.KindleDocs/datasets/Kindle.KindleDocs.DocumentMetadata/Kindle.KindleDocs.DocumentMetadata.csv": (
        "DocumentId,Title,DocumentProvider,Filename,DocumentOriginalType,DocumentSizeInBytes,"
        "DocumentTypeAndConvertionCompletionStatus 1,DocumentTypeAndConvertionCompletionStatus 2,"
        "DocumentTypeAndConvertionCompletionStatus 3,DocumentTypeAndConvertionCompletionStatus 4,"
        "DocumentTypeAndConvertionCompletionStatus 5,HasBeenDeleted,EntryCreationDate\n"
        f"{DOC_A},Un roman: le sous-titre,\"Dupont, Marie\",Un roman.azw3,application/epub+zip,2500000,"
        "documentOrigin = personal document,conversionStatus = finished,documentType = document,"
        "AssetGenerationState = COMPLETE,Not Available,No,2025-02-20T10:00:00.000Z\n"
        f"{DOC_B},Jamais lu,Unknown,Jamais lu.azw3,application/pdf,1000000,documentOrigin = personal document,"
        "conversionStatus = finished,documentType = document,AssetGenerationState = COMPLETE,Not Available,No,"
        "2025-02-21T10:00:00.000Z\n"
    ),
    "Digital.Content.Whispersync/whispersync.csv": WHISPERSYNC_HEADER + YELLOW_HIGHLIGHT + YELLOW_HIGHLIGHT + (
        f"Not Available,{DOC_A},Not Available,kindle.highlight,2025-03-01T20:20:00Z,G0,Not Available,PDOC,"
        "YJBinary,2025-03-01T20:30:00Z,2025-03-01T20:30:00Z,Une phrase à retenir,Not Available,Not Available,"
        "Not Available,No\n"
        f"Not Available,{DOC_A},Not Available,kindle.last_read,2025-03-02T21:15:00Z,G0,Not Available,PDOC,"
        "YJBinary,2025-03-01T20:10:00Z,2025-03-02T21:15:00Z,Not Available,Not Available,Not Available,Not Available,No\n"
        "B0TESTBOOK,Not Available,Penser vite,kindle.bookmark,2025-03-03T21:07:00Z,G0,Not Available,EBOK,YJBinary,"
        "2025-03-03T21:10:00Z,2025-03-03T21:10:00Z,{mchl_color:dark_blue},Not Available,Not Available,Not Available,No\n"
    ),
    "Digital.Content.Ownership/Digital.Content.Ownership.1.json": json.dumps(OWNERSHIP),
    "Kindle.UnifiedLibraryIndex/datasets/Kindle.UnifiedLibraryIndex.CustomerAuthorNameRelationship.1.1/"
    "Kindle.UnifiedLibraryIndex.CustomerAuthorNameRelationship.1.1.csv": (
        "Product Name,ASIN,Author Name\nPenser vite,B0TESTBOOK,\"Kahneman, Daniel\"\n"
    ),
    "Kindle.BookRewards/datasets/Kindle.BookRewards.Achievements.2/Kindle.BookRewards.Achievements.2.csv": (
        "AchievementGroupName,AchievementName,EarnDate,Marketplace,Quantity\n"
        "2025 Kindle Spring Challenge,SilverReader,2025-04-01T10:00:00Z,www.amazon.fr,1\n"
    ),
    # Telemetry, left alone.
    "Kindle.Devices.EreaderPowerdStateChange/Kindle.Devices.EreaderPowerdStateChange.csv": (
        "created_timestamp,preferred_marketplace,country,device_family,ASIN,prev_state,curr_state\n"
        "2024-05-09T19:52:32Z,www.amazon.fr,FR,Kindle E-reader,Not Available,NOT ACTIVE,SCREEN SAVER\n"
    ),
}


@pytest.fixture()
def export(tmp_path):
    for name, content in FILES.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return tmp_path


@pytest.fixture()
def items(export):
    return list(amazon.AmazonImporter().parse(export))


def by_type(items, item_type):
    return [i for i in items if i.item_type == item_type]


def rows_of(items):
    """The items as crud.get_library_rows returns them (naive timestamps)."""
    return [
        (n, i.timestamp.replace(tzinfo=None) if i.timestamp else None, i.item_type, i.context, i.content, i.author,
         json.dumps(i.fields, ensure_ascii=False) if i.fields else None)
        for n, i in enumerate(items)
    ]


def test_kindle_only_export_detected_as_amazon(export):
    assert importers.detect(export).key == "amazon"


def test_only_reading_files_become_items(items):
    assert {i.item_type for i in items} == {
        "reading_session", "kindle_finished", "kindle_highlight", "kindle_bookmark", "kindle_book",
        "kindle_document", "kindle_badge",
    }


def test_sessions_named_after_their_book(items):
    sessions = by_type(items, "reading_session")
    assert len(sessions) == 4  # the row with only an end is dropped
    novel = [s for s in sessions if s.fields.get("Document") == DOC_A]
    # The document's own metadata wins over the shorter title of another file.
    assert {s.content for s in novel} == {s.context for s in novel} == {"Un roman: le sous-titre"}
    assert novel[0].author == "Marie Dupont"
    assert novel[0].fields["Type"] == "Document personnel"
    assert novel[0].fields["Durée de lecture"] == "30.0 min"
    book = next(s for s in sessions if s.fields.get("ASIN") == "B0TESTBOOK")
    assert (book.content, book.author) == ("Penser vite", "Daniel Kahneman")
    assert book.fields["Lien"] == "https://www.amazon.fr/dp/B0TESTBOOK"
    assert book.timestamp == datetime(2025, 3, 3, 21, 0, tzinfo=timezone.utc)


def test_finished_books_read_from_the_packed_key(items):
    finished = {f.context: f for f in by_type(items, "kindle_finished")}
    assert set(finished) == {"Un roman: le sous-titre", "Penser vite"}
    assert finished["Un roman: le sous-titre"].timestamp == datetime(2025, 3, 2)
    assert finished["Un roman: le sous-titre"].fields["Marqué terminé"].startswith("Automatiquement")
    assert finished["Penser vite"].fields == {
        "Marqué terminé": "Manuellement", "ASIN": "B0TESTBOOK", "Lien": "https://www.amazon.fr/dp/B0TESTBOOK",
    }


def test_annotations_sync_copies_collapsed_positions_left_out(items):
    highlights = by_type(items, "kindle_highlight")
    assert len(highlights) == 2
    colored = next(h for h in highlights if h.context == "Penser vite")
    assert (colored.content, colored.fields["Couleur"]) == ("Penser vite", "Jaune")
    # A highlight whose text is exported keeps it, under its book.
    quoted = next(h for h in highlights if h.context == "Un roman: le sous-titre")
    assert quoted.content == "Une phrase à retenir"
    assert quoted.fields["Livre"] == "Un roman: le sous-titre"
    bookmark, = by_type(items, "kindle_bookmark")
    assert bookmark.fields["Couleur"] == "Bleu foncé"


def test_library_one_book_per_asin_without_dictionaries(items):
    books = {b.fields["ASIN"]: b for b in by_type(items, "kindle_book")}
    assert set(books) == {"B0TESTBOOK", "B0OTHERBK1"}
    bought = books["B0TESTBOOK"]
    # Dated by its first acquisition still active, not the revoked one.
    assert bought.timestamp == datetime(2025, 1, 10, 10, 0, tzinfo=timezone.utc)
    assert (bought.content, bought.fields["Origine"], bought.fields["Statut"]) == ("Penser vite", "Achat", "Actif")
    revoked = books["B0OTHERBK1"]
    assert (revoked.content, revoked.context, revoked.fields["Statut"]) == ("B0OTHERBK1", "B0OTHERBK1", "Révoqué")


def test_personal_documents(items):
    docs = {d.fields["Document"]: d for d in by_type(items, "kindle_document")}
    novel = docs[DOC_A]
    assert (novel.content, novel.author) == ("Un roman: le sous-titre", "Marie Dupont")
    assert novel.fields["Format d'origine"] == "EPUB"
    assert novel.fields["Taille"] == "2.5 Mo"
    assert novel.timestamp == datetime(2025, 2, 20, 10, 0, tzinfo=timezone.utc)
    assert docs[DOC_B].author is None  # "Unknown"


def test_badges(items):
    badge, = by_type(items, "kindle_badge")
    assert (badge.content, badge.context) == ("Silver Reader", "2025 Kindle Spring Challenge")


def test_library_statuses(items):
    books = {b["context"]: b for b in stats.build_library(rows_of(items), {"B0OTHERBK1": "Autre livre"})}
    novel = books["Un roman: le sous-titre"]
    assert (novel["status"], novel["kind"], novel["sessions"], novel["minutes"], novel["annotations"]) == (
        "finished", "kindle_document", 2, 45.0, 1)
    assert novel["added"] == datetime(2025, 2, 20, 10, 0)
    assert novel["finished"] == datetime(2025, 3, 2)
    assert books["Penser vite"]["kind"] == "kindle_book"
    assert books["Penser vite"]["annotations"] == 2  # the highlight and the bookmark
    assert books["Jamais lu"]["status"] == "unopened"
    # Named by its ASIN alone: shown under the title of its purchase.
    assert (books["B0OTHERBK1"]["title"], books["B0OTHERBK1"]["status"]) == ("Autre livre", "unopened")

    breakdown = stats.library_breakdown(list(books.values()))
    assert breakdown["labels"] == ["Livres Kindle", "Documents personnels"]
    assert {s["keys"][0]: s["data"] for s in breakdown["series"]} == {"finished": [1, 1], "unopened": [1, 1]}


def test_library_arrivals_stacked_by_kind(items):
    arrivals = stats.build_library_arrivals(rows_of(items), "year")
    assert arrivals["labels"] == ["2024", "2025"]
    assert {s["label"]: s["data"] for s in arrivals["series"]} == {
        "Livres Kindle": [1, 1], "Documents personnels": [0, 2],
    }


def test_reading_days_streak_and_heatmap(items):
    rows = [row[:5] + row[6:] for row in rows_of(items)]  # DashboardRow: no author
    days = stats.reading_days(rows)
    assert days == [date(2025, 3, 1), date(2025, 3, 2), date(2025, 3, 3), date(2025, 3, 5)]
    assert stats.longest_streak(days) == {"days": 3, "start": date(2025, 3, 1), "end": date(2025, 3, 3)}
    grid = stats.build_reading_heatmap(rows)
    assert grid[5][20] == 30  # Saturday 1 March, 20h: 30 minutes
    assert sum(map(sum, grid)) == 60

    dashboard = stats.build_reading_dashboard(rows, "month")
    assert dashboard["total_finished"] == 2
    assert dashboard["finished_series"][0]["data"] == [2]
    assert dashboard["total_sessions"] == 4


def test_reading_titles_flag_finished_books(items):
    rows = [row[:5] + row[6:] for row in rows_of(items)]
    titles = stats.build_reading_titles(rows, {}, finished={"Penser vite"})
    assert [(t["label"], t["finished"]) for t in titles] == [("Un roman: le sous-titre", False), ("Penser vite", True)]


def test_library_tab_shown_for_kindle_data_only():
    assert "library" in [v.key for v in platforms.available_views({"kindle_document"})]
    assert "library" not in [v.key for v in platforms.available_views({"order_item", "reading_session"})]


def test_a_revoked_book_is_the_same_record():
    def signature(status):
        fields = json.dumps({"Origine": "Achat", "Statut": status, "ASIN": "B0TESTBOOK"})
        return dedup.item_signature("amazon", "kindle_book", "Bibliothèque Kindle", datetime(2025, 1, 10), None,
                                    "Penser vite", None, "Penser vite", fields)

    assert signature("Actif") == signature("Révoqué")


# -- Two e-readers on one account -------------------------------------------

@pytest.fixture()
def two_devices(export):
    """The export plus a second e-reader (a different serial, the same
    family): one session on a book of the first, one on a book of its own."""
    path = export / "Kindle.Devices.ReadingSession/Kindle.Devices.ReadingSession.csv"
    with path.open("a", encoding="utf-8") as f:
        f.write(
            f"2025-03-10T10:00:00Z,2025-03-10T11:00:00Z,B0TESTBOOK,www.amazon.fr,www.amazon.fr,Kindle E-reader,G1,1,E-Book,3600000,50\n"
            f"2025-03-11T10:00:00Z,2025-03-11T10:20:00Z,{DOC_B},www.amazon.fr,www.amazon.fr,Kindle E-reader,G1,1,Personal Document,1200000,20\n"
        )
    items = list(amazon.AmazonImporter().parse(export))
    return [row[:5] + row[6:] for row in rows_of(items)], items


def test_items_name_their_device_by_serial(two_devices):
    _rows, items = two_devices
    devices = {s.fields["Numéro de série"]: s.fields["Appareil"] for s in by_type(items, "reading_session")}
    # Same family, told apart by the end of the serial; a session with no
    # family of its own takes the one its serial has elsewhere.
    assert devices == {"G0": "Kindle E-reader …G0", "G1": "Kindle E-reader …G1"}
    # An annotation has a serial alone; the sessions give it its family.
    assert {h.fields["Appareil"] for h in by_type(items, "kindle_highlight")} == {"Kindle E-reader …G0"}


def test_reading_devices_ranked_with_stable_slots(two_devices):
    rows, _items = two_devices
    devices = stats.reading_devices(rows)
    assert [(d["value"], d["count"], d["slot"]) for d in devices][:2] == [
        ("Kindle E-reader …G0", 4, 0), ("Kindle E-reader …G1", 2, 1),
    ]


def test_filter_keeps_one_device_and_the_books_it_finished(two_devices):
    rows, _items = two_devices
    kept = stats.filter_reading_rows(rows, ["Kindle E-reader …G1"])
    types = sorted(r[2] for r in kept)
    # Penser vite and Jamais lu were read on G1: their library entries and
    # the finished record come along; Un roman, read on G0 only, does not.
    assert types == ["kindle_book", "kindle_document", "kindle_finished", "reading_session", "reading_session"]
    assert stats.filter_reading_rows(rows, []) == rows


def test_dashboard_and_titles_split_by_device(two_devices):
    rows, _items = two_devices
    devices = stats.reading_devices(rows)
    dashboard = stats.build_reading_dashboard(rows, "month", devices)
    labels = {s["label"]: s for s in dashboard["minutes_series"]}
    first, second = labels["Kindle E-reader …G0"], labels["Kindle E-reader …G1"]
    assert (first["slot"], second["slot"]) == (0, 1)
    assert second["device"] == "Kindle E-reader …G1"
    assert sum(second["data"]) == 80.0 and sum(first["data"]) == 60.0

    titles = {t["label"]: t for t in stats.build_reading_titles(rows, {}, devices=devices)}
    assert [(s["label"], s["minutes"]) for s in titles["Penser vite"]["segments"]] == [
        ("Kindle E-reader …G1", 60.0), ("Kindle E-reader …G0", 15.0),
    ]


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from fastapi.testclient import TestClient

    from app import processing
    from app.database import Base, SessionLocal, engine
    from app.main import app
    from app.models import ImportBatch, ImportStatus

    root = tmp_path_factory.mktemp("kindle")
    for name, content in FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    with (root / "Kindle.Devices.ReadingSession/Kindle.Devices.ReadingSession.csv").open("a", encoding="utf-8") as f:
        f.write("2025-03-10T10:00:00Z,2025-03-10T11:00:00Z,B0TESTBOOK,www.amazon.fr,www.amazon.fr,Kindle E-reader,G1,1,E-Book,3600000,50\n")
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        batch = ImportBatch(original_filename="Kindle.zip", status=ImportStatus.parsing)
        db.add(batch)
        db.commit()
        processing._detect_and_parse(db, batch, root)
    return TestClient(app)


def test_reading_page_filters_by_device_and_colours_them(client):
    html = client.get("/p/amazon/reading").text
    assert "Tous les appareils" in html
    assert 'value="Kindle E-reader …G1"' in html
    assert 'background: var(--tb-slot-1)' in html  # the second device's colour in the title bars

    one = client.get("/p/amazon/reading", params={"device": "Kindle E-reader …G1"}).text
    assert "Kindle E-reader …G1" in one and "sur 2 appareils" not in one
    assert "background: var(--tb-slot-0)" not in one.split("rank-list")[1]


def test_panel_filters_by_device(client):
    both = client.get("/p/amazon/panel", params={"item_type": "reading_session"}).text
    only = client.get("/p/amazon/panel", params={"item_type": "reading_session", "device": "Kindle E-reader …G1"}).text
    assert "Penser vite" in only
    assert len(only) < len(both)


def test_library_filters_by_device(client):
    both = client.get("/p/amazon/library").text
    assert "Tous les appareils" in both and "Jamais lu" in both

    only = client.get("/p/amazon/library", params={"device": "Kindle E-reader …G1"}).text
    # Only Penser vite was read on G1: the unopened document is not its.
    assert "Penser vite" in only and "Jamais lu" not in only
    assert "device=Kindle+E-reader" in only  # the book's panel keeps the filter

    panel = client.get("/p/amazon/library/panel", params={"device": "Kindle E-reader …G1"}).text
    assert "Penser vite" in panel and "Jamais lu" not in panel
