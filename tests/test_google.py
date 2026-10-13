import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import stats  # noqa: E402
from app.importers.google_takeout import GoogleTakeoutImporter, classify_activity  # noqa: E402

NBSP = "\xa0"


def _local(*args, offset_hours=2):
    """A date written with a fixed UTC offset, as the server's local time."""
    return datetime(*args, tzinfo=timezone(timedelta(hours=offset_hours))).astimezone().replace(tzinfo=None)


def _entry(header, body, caption):
    """One "My Activity" HTML entry in the markup of a real French export."""
    return (
        '<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp"><div class="mdl-grid">'
        f'<div class="header-cell mdl-cell mdl-cell--12-col"><p class="mdl-typography--title">{header}<br></p></div>'
        f'<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">{body}</div>'
        '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1 mdl-typography--text-right"></div>'
        f'<div class="content-cell mdl-cell mdl-cell--12-col mdl-typography--caption">{caption}</div>'
        '</div></div>'
    )


def _caption(product, extra=""):
    return (
        f"<b>Produits{NBSP}:</b><br>&emsp;{product}<br>{extra}"
        f"<b>Que fait cette information ici{NBSP}?</b><br>&emsp;Cette activité a été enregistrée sur votre compte "
        '<a href="https://myaccount.google.com/activitycontrols">Commandes</a><br>'
    )


ACTIVITY = {
    "YouTube": [
        _entry("YouTube",
               'Vous avez regardé <a href="https://www.youtube.com/watch?v=a1">Le tuto</a><br>'
               '<a href="https://www.youtube.com/channel/c1">Chaîne Une</a><br>8 mars 2026, 17:14:40 CEST<br>',
               _caption("YouTube")),
        _entry("YouTube",
               'A aimé <a href="https://www.youtube.com/watch?v=a2">Un clip</a><br>'
               '<a href="https://www.youtube.com/channel/c2">Chaîne Deux</a><br>9 août 2025, 21:00:00 CEST<br>',
               _caption("YouTube")),
    ],
    f"Google Play{NBSP}Store": [
        _entry("Gmail", 'Application utilisée&nbsp;: <a href="https://play.google.com/store/apps/details?id=g">'
                        'Gmail</a><br>1 oct. 2026, 08:01:02 CEST<br>', _caption("Google Play Store")),
        _entry("Spotify", 'Application utilisée&nbsp;: <a href="https://play.google.com/x">Spotify: Music and Podcasts'
                          '</a><br>2 oct. 2026, 19:30:00 CEST<br>', _caption("Google Play Store")),
    ],
    "Recherche": [
        _entry("Recherche", 'Vous avez recherché <a href="https://www.google.com/search?q=potager">potager</a><br>'
                            '3 oct. 2026, 10:00:00 CEST<br>',
               _caption("Recherche", f"<b>Lieux{NBSP}:</b><br>&emsp;Dans cette zone générale - D'après votre appareil<br>")),
        _entry("Recherche", 'Vous avez recherché <a href="https://www.google.com/search?q=POTAGER">POTAGER </a><br>'
                            '4 oct. 2026, 11:00:00 CEST<br>', _caption("Recherche")),
        _entry("Recherche", 'Vous avez consulté <a href="https://www.google.com/url?q=https://jardinage.example/guide'
                            '&amp;usg=x">Guide | Potager</a><br>4 oct. 2026, 11:01:00 CEST<br>',
               _caption("Recherche")),
        _entry("Recherche", 'Vous avez consulté <a href="https://local.google.com/place?id=1">Boulangerie du Coin</a>'
                            "<br>4 oct. 2026, 12:00:00 CEST<br>", _caption("Recherche")),
        _entry("Recherche", "2 notifications<br>Sujets couverts&nbsp;:<br>Météo - ignorée<br>"
                            "5 oct. 2026, 07:00:00 CEST<br>", _caption("Recherche")),
    ],
    "Maps": [
        _entry("Maps", 'Itinéraire pour <a href="https://www.google.com/maps/dir/x">Gare de Lyon</a><br>'
                       "Position actuelle<br>Gare de Lyon<br>6 oct. 2026, 09:00:00 CEST<br>", _caption("Maps")),
        _entry("Maps", '<a href="https://www.google.com/maps/place/y">Piscine municipale</a><br>'
                       "6 oct. 2026, 09:05:00 CEST<br>", _caption("Maps")),
    ],
    f"Google{NBSP}Analytics": [
        _entry("Scorely: Live Scores - Apps on Google Play",
               'Vous avez utilisé <a href="https://play.google.com/s">Scorely: Live Scores - Apps on Google '
               "Play</a><br>7 oct. 2026, 20:00:00 CEST<br>", _caption("Google Analytics")),
    ],
    f"Applications{NBSP}Gemini": [
        # The date isn't the last line: an attachment note follows it.
        _entry(f"Applications{NBSP}Gemini",
               "Prompt&nbsp;: Explique les DNS<br>12 mars 2026, 10:00:00 CEST<br>1 fichier joint.<br>",
               _caption("Applications Gemini")),
        # The answer as rich content after the date line.
        _entry(f"Applications{NBSP}Gemini",
               "Prompt&nbsp;: Résume ce texte<br>13 mars 2026, 11:00:00 CEST<br>"
               "<p>Voici un résumé.</p><h3>Points clés</h3><ul><li>Un</li></ul><br>",
               _caption("Applications Gemini")),
    ],
}


def _write_export(path):
    root = path / "Takeout"
    for service, entries in ACTIVITY.items():
        folder = root / "Mon activité" / service
        folder.mkdir(parents=True)
        (folder / "MonActivité.html").write_text(
            '<html><body><div class="mdl-grid">' + "".join(entries) + "</div></body></html>", encoding="utf-8",
        )
    chrome = root / "Chrome"
    chrome.mkdir()
    (chrome / "Historique.json").write_text(json.dumps({"Browser History": [
        {"time_usec": 1_759_600_000_000_000, "title": "Guide | Potager", "url": "https://jardinage.example/guide",
         "page_transition": "LINK"},
        {"time_usec": 1_759_599_990_000_000, "title": "potager - Recherche Google",
         "url": "https://www.google.com/search?q=potager", "page_transition": "GENERATED"},
    ]}), encoding="utf-8")
    return path


@pytest.fixture()
def export(tmp_path):
    return _write_export(tmp_path)


@pytest.fixture()
def items(export):
    return list(GoogleTakeoutImporter().parse(export))


def _one(items, item_type, **match):
    found = [i for i in items if i.item_type == item_type and all(getattr(i, k) == v for k, v in match.items())]
    assert len(found) == 1, [(i.item_type, i.title) for i in items]
    return found[0]


@pytest.mark.parametrize("service, sentence, expected", [
    (f"Google Play{NBSP}Store", "Application utilisée : LinkedIn: Jobs & Business News", ("Applications", "app_used", "LinkedIn")),
    ("Maps", "Application utilisée : Maps", ("Applications", "app_used", "Maps")),
    ("Recherche", "Vous avez recherché  claude code", ("Recherches", "search", "claude code")),
    ("Maps", "Vous avez recherché Restaurants", ("Recherches", "search", "Restaurants")),
    ("Recherche d_images", "A consulté l'image sur example.com", ("Web", "image_view", "example.com")),
    ("Recherche", "1 notification", ("Autres", "notification", None)),
    ("YouTube", "Vous vous êtes abonné à Une Chaîne", ("Vidéos & TV", "channel_subscribed", "Une Chaîne")),
    ("YouTube", "N'a pas aimé Une vidéo", ("Vidéos & TV", "video_disliked", "Une vidéo")),
    (f"Android{NBSP}TV", "Vous avez consulté Plex", ("Vidéos & TV", "video_page", "Plex")),
    ("Chrome", "Vous avez consulté GitHub", ("Web", "chrome_visit", "GitHub")),
    ("Aide", "Vous avez consulté Aide Gmail", ("Web", "page_visit", "Aide Gmail")),
    ("Assistant", "Vous avez dit arrête", ("Assistant & IA", "assistant_command", "arrête")),
    ("Mode IA", "Vous avez recherché comment marche X", ("Assistant & IA", "ai_prompt", "comment marche X")),
    ("Solutions publicitaires", "Vous avez utilisé Reddit", ("Suivi par des tiers", "tracked_app", "Reddit")),
    ("Google Play Console", "Vous avez utilisé la Google Play Console", ("Applications", "service_used", "Google Play Console")),
    ("Android", "A utilisé la fonctionnalité Écran partagé", ("Applications", "app_feature", "Écran partagé")),
    ("Keep", "Une phrase inconnue", ("Autres", "activity", None)),
    # English exports
    ("YouTube", "Watched Some video", ("Vidéos & TV", "video_watched", "Some video")),
    ("Search", "Searched for kubernetes", ("Recherches", "search", "kubernetes")),
    ("Maps", "Directions to Home", ("Trajets & lieux", "directions", "Home")),
    ("Ads", "Used Scorely", ("Suivi par des tiers", "tracked_app", "Scorely")),
])
def test_classify_activity(service, sentence, expected):
    assert classify_activity(service, sentence) == expected


def test_html_activity_filed_by_usage(items):
    watched = _one(items, "video_watched")
    assert (watched.category, watched.title, watched.author) == ("Vidéos & TV", "Le tuto", "Chaîne Une")
    assert watched.context == "YouTube"
    assert watched.fields["Chaîne"] == "Chaîne Une"
    assert watched.fields["Produit"] == "YouTube"  # the French caption header is read...
    assert "Que fait" not in json.dumps(watched.fields, ensure_ascii=False)  # ...and its explanation isn't

    assert _one(items, "video_liked").author == "Chaîne Deux"
    assert {i.title for i in items if i.item_type == "app_used"} == {"Gmail", "Spotify"}
    assert {i.context for i in items if i.item_type == "app_used"} == {"Google Play Store"}

    searches = [i for i in items if i.item_type == "search"]
    assert [s.title for s in searches] == ["potager", "POTAGER"]
    assert searches[0].fields["Lieu"].startswith("Dans cette zone générale")

    page = _one(items, "page_visit")
    assert (page.category, page.title, page.author) == ("Web", "Guide | Potager", "jardinage.example")
    assert page.fields["Lien"] == "https://jardinage.example/guide"  # Google's redirect unwrapped

    route = _one(items, "directions")
    assert (route.category, route.title) == ("Trajets & lieux", "Gare de Lyon")
    assert route.fields["Précisions"] == "Position actuelle · Gare de Lyon"
    assert _one(items, "place_viewed", context="Maps").title == "Piscine municipale"
    # A business opened from the search results is a place looked at.
    assert _one(items, "place_viewed", context="Recherche").title == "Boulangerie du Coin"

    tracked = _one(items, "tracked_app")
    assert (tracked.category, tracked.title, tracked.context) == ("Suivi par des tiers", "Scorely", "Google Analytics")
    assert _one(items, "notification").category == "Autres"


def test_html_dates_converted_from_the_offset_they_state(items):
    # March is winter time in France, yet the export writes "CEST": the
    # stated offset is what the date is converted from.
    assert _one(items, "video_watched").timestamp == _local(2026, 3, 8, 17, 14, 40)
    assert _one(items, "app_used", title="Gmail").timestamp == _local(2026, 10, 1, 8, 1, 2)


def test_gemini_date_found_above_a_trailing_note(items):
    prompt = _one(items, "ai_prompt", title="Explique les DNS")
    assert prompt.category == "Assistant & IA"
    assert prompt.timestamp == _local(2026, 3, 12, 10, 0, 0)
    assert prompt.fields["Précisions"] == "1 fichier joint."


def test_gemini_answer_kept_as_content_with_the_prompt_dated(items):
    prompt = _one(items, "ai_prompt", title="Résume ce texte")
    assert prompt.timestamp == _local(2026, 3, 13, 11, 0, 0)
    assert prompt.content.startswith("Prompt : Résume ce texte")
    assert "Points clés" in prompt.content


def test_chrome_history_is_web_with_its_site(items):
    visit = _one(items, "browser_history")
    assert (visit.category, visit.author, visit.context) == ("Web", "jardinage.example", "Chrome")
    # A Google results page is the search itself, not a site visited.
    assert _one(items, "results_page").author == "google.com"


def test_json_activity_reads_the_channel_and_converts_utc(tmp_path):
    folder = tmp_path / "Takeout" / "My Activity" / "YouTube"
    folder.mkdir(parents=True)
    (folder / "My Activity.json").write_text(json.dumps([{
        "header": "YouTube", "title": "Watched A video", "titleUrl": "https://www.youtube.com/watch?v=1",
        "subtitles": [{"name": "A channel", "url": "https://www.youtube.com/channel/1"}],
        "time": "2024-01-15T12:00:00.000Z", "products": ["YouTube"],
    }]), encoding="utf-8")
    item = next(iter(GoogleTakeoutImporter().parse(tmp_path)))
    assert (item.item_type, item.title, item.author) == ("video_watched", "A video", "A channel")
    assert item.timestamp == _local(2024, 1, 15, 12, 0, 0, offset_hours=0)


def test_type_labels_are_french():
    from app.templating import display_label
    assert display_label("app_used") == "Application ouverte"
    assert display_label("Vidéos & TV") == "Vidéos & TV"


# ── Usage statistics ──────────────────────────────────────────────────────

def test_ranking_merges_case_and_spacing_and_keeps_the_usual_spelling():
    counts = [("search", "potager", 3), ("search", "Potager ", 1), ("search", "compost", 2), ("search", None, 5)]
    ranking = stats.build_ranking(counts, "title")
    assert ranking["top"][0] == {"label": "potager", "key": "potager", "count": 4}
    assert (ranking["distinct"], ranking["total"]) == (2, 11)


def test_words_ranking_counts_each_query_once_and_skips_stop_words():
    counts = [("search", "comment planter un potager sur un balcon", 2), ("search", "potager potager l'arrosage", 1)]
    words = {w["key"]: w["count"] for w in stats.build_ranking(counts, "words")["top"]}
    assert words["potager"] == 3
    assert words["arrosage"] == 1  # elision dropped
    assert "comment" not in words and "sur" not in words


def test_select_usage_matches_the_ranking_rule():
    rows = [(1, datetime(2026, 1, 1), "potager"), (2, datetime(2026, 1, 2), "POTAGER"), (3, datetime(2026, 1, 3), "compost"),
            (4, None, "potager en carre")]
    assert stats.select_usage(rows, "title", "potager") == [2, 1]
    assert stats.select_usage(rows, "words", "potager") == [2, 1, 4]
    assert stats.select_usage(rows, "title", exclude=["potager"]) == [3, 4]


def test_trend_keeps_leaders_and_hides_the_tail():
    leaders = [{"key": "gmail", "label": "Gmail", "count": 3}]
    events = [(datetime(2026, 1, 5, 10), "Gmail"), (datetime(2026, 1, 6, 10), "gmail"),
              (datetime(2026, 2, 1, 10), "Other app")]
    trend = stats.build_usage_trend(events, "month", leaders)
    gmail, other = trend["series"]
    assert (gmail["label"], gmail["data"], gmail["keys"]) == ("Gmail", [2, 0], ["gmail"])
    assert (other["label"], other["hidden"], other["other"], other["keys"]) == ("Autres", True, True, [])


def test_a_category_named_autres_is_never_a_coloured_series():
    slots = stats.series_slots(["Applications", "Autres", "Web"])
    assert slots == {"Applications": 0, "Web": 1}


def test_hour_profile_rows_follow_the_ranking():
    rows = stats.build_hour_profile([("Web", 21, 4), ("Applications", 8, 9), ("Applications", 21, 1)],
                                    ["Applications", "Web"])
    assert [r["label"] for r in rows] == ["Applications", "Web"]
    assert (rows[0]["peak"], rows[0]["max"], rows[0]["total"]) == (8, 9, 10)


def test_coverage_spans_each_service_coloured_by_its_main_category():
    coverage = stats.build_coverage([
        ("YouTube", "Vidéos & TV", 20, datetime(2016, 12, 31), datetime(2026, 10, 9)),
        ("Recherche", "Recherches", 8, datetime(2025, 4, 17), datetime(2026, 10, 9)),
        ("Recherche", "Web", 3, datetime(2025, 4, 20), datetime(2026, 10, 8)),
    ], ["Vidéos & TV", "Recherches", "Web"])
    youtube, search = coverage["entries"]  # the busiest first
    assert (youtube["label"], youtube["slot"], youtube["count"]) == ("YouTube", 0, 20)
    assert (search["category"], search["count"], search["first"]) == ("Recherches", 11, datetime(2025, 4, 17))
    assert coverage["ticks"][0] == {"label": 2016, "left": 0.0}
    assert 0 < youtube["left"] < search["left"]


# ── The Usages page ───────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from fastapi.testclient import TestClient

    from app import processing
    from app.database import Base, SessionLocal, engine
    from app.main import app
    from app.models import ImportBatch, ImportStatus

    root = _write_export(tmp_path_factory.mktemp("takeout"))
    Base.metadata.create_all(engine)
    db = SessionLocal()
    batch = ImportBatch(original_filename="takeout.zip", status=ImportStatus.parsing)
    db.add(batch)
    db.commit()
    processing._detect_and_parse(db, batch, root)
    db.close()
    return TestClient(app)


def test_usage_tab_offered_and_lists_the_families(client):
    html = client.get("/p/google").text
    assert 'href="/p/google/usage"' in html
    html = client.get("/p/google/usage").text
    for label in ("Applications", "Recherches", "Vidéos &amp; TV", "Web", "Trajets &amp; lieux",
                  "Assistant &amp; IA", "Suivi par des tiers"):
        assert label in html
    assert "Applications les plus ouvertes" in html
    assert "Rythme de la journée" in html and "Historique conservé" in html


def test_usage_rankings_per_family(client):
    html = client.get("/p/google/usage", params={"family": "search"}).text
    assert "Mots les plus recherchés" in html
    assert "trendChart" not in html  # searches have no "over time" chart
    html = client.get("/p/google/usage", params={"family": "web"}).text
    assert "jardinage.example" in html and "Sites les plus consultés" in html
    html = client.get("/p/google/usage", params={"family": "video"}).text
    assert "Chaîne Une" in html and "trendChart" in html


def test_usage_panel_opens_what_a_ranking_counted(client):
    html = client.get("/p/google/usage/panel", params={"family": "search", "rank": 0, "key": "potager"}).text
    assert "2 éléments" in html
    html = client.get("/p/google/usage/panel", params={"family": "apps", "rank": 0, "exclude": "gmail"}).text
    assert "Spotify" in html and "Gmail" not in html
    # The opened search result and Chrome's history entry: one site, two visits.
    html = client.get("/p/google/usage/panel", params={"family": "web", "rank": 0, "key": "jardinage.example"}).text
    assert "2 éléments" in html


def test_item_types_shown_in_french(client):
    html = client.get("/p/google/activity", params={"group_by": "item_type"}).text
    assert "Application ouverte" in html
