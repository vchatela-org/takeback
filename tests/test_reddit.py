import csv
import io
import json
import re
import sys
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import anonymize, dedup, importers, platforms  # noqa: E402
from app.importers import reddit  # noqa: E402

OWNER = "exemple_redditor"


def table(header, *rows):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue()


POST_COLUMNS = ("id", "permalink", "date", "ip", "subreddit", "gildings", "title", "url", "body")
COMMENT_COLUMNS = ("id", "permalink", "date", "ip", "subreddit", "gildings", "gildings_silver", "gildings_supergold",
                   "link", "parent", "body", "media")
MESSAGE_COLUMNS = ("id", "permalink", "thread_id", "date", "ip", "from", "to", "subject", "body")

# Received from a newsletter, twice; then a friend's message the owner answered
# in the same thread. The export writes its permalinks one slash short.
MESSAGES = [
    ("aaa111", "https:/www.reddit.com/message/messages/aaa111", "", "2022-09-09 09:27:42 UTC", "",
     "/u/Lettre_Hebdo", f"/u/{OWNER}", "La lettre de septembre", "Bonjour à tous,\n\nles nouvelles du mois."),
    ("bbb222", "https:/www.reddit.com/message/messages/bbb222", "", "2022-10-09 09:27:42 UTC", "",
     "/u/Lettre_Hebdo", f"/u/{OWNER}", "La lettre d'octobre", "Encore des nouvelles."),
    ("ccc333", "https:/www.reddit.com/message/messages/ccc333", "", "2023-01-01 10:00:00 UTC", "",
     "/u/ami_un", f"/u/{OWNER}", "Salut", "Tu viens samedi ?"),
    ("ddd444", "https:/www.reddit.com/message/messages/ddd444", "ccc333", "2023-01-01 10:05:00 UTC", "203.0.113.9",
     f"/u/{OWNER}", "/u/ami_un", "re: Salut", "Oui !"),
]

# A made-up export in the shape of a real one: a flat zip of CSVs, three of
# them with a textless `*_headers.csv` twin, and the header-only files.
FILES = {
    "statistics.csv": table(
        ("statistic", "value"),
        ("account name", OWNER), ("export time", "2026-10-14 14:51:58 UTC"), ("is_deleted", "False"),
        ("registration date", "2019-03-04 10:00:00 UTC"), ("email verified", "True"),
        ("email address", "owner@example.com"),
    ),
    "ip_logs.csv": table(("date", "ip"), ("registration ip", "198.51.100.7"), ("2026-08-07 15:03:45 UTC", "203.0.113.9")),
    "account_gender.csv": table(("account_gender",), ("not specified",)),
    "birthdate.csv": table(("source", "verification_method", "birthdate", "age_greater_than_equal_to", "age_less_than",
                            "last_updated")),
    "linked_identities.csv": table(("issuer_id", "subject_id"), ("https://accounts.google.com", "1234567890")),
    "linked_phone_number.csv": table(("phone_number",)),
    "stripe.csv": table(("stripe_account_id",)),
    "persona.csv": table(("persona_inquiry_id",)),
    "posts.csv": table(
        POST_COLUMNS,
        # A text post: its `url` is its own path, not a link it shares.
        ("1aaaaaa", "https://www.reddit.com/r/france/comments/1aaaaaa/une_question_sur_le_tri/",
         "2025-09-18 10:35:44 UTC", "", "france", "0", "Une question sur le tri ?",
         "/r/france/comments/1aaaaaa/une_question_sur_le_tri/", "Bonjour à tous,\n\nune vraie question."),
        # An image post, awarded twice, with the IP recent rows carry.
        ("1bbbbbb", "https://www.reddit.com/r/pics/comments/1bbbbbb/mon_balcon/", "2026-08-07 14:56:47 UTC",
         "203.0.113.9", "pics", "2", "Mon balcon", "https://i.redd.it/abcdef123.jpeg", ""),
        # Posted to the owner's own profile.
        ("1cccccc", f"https://www.reddit.com/r/u_{OWNER}/comments/1cccccc/sur_mon_profil/", "2024-01-02 08:00:00 UTC",
         "", f"u_{OWNER}", "0", "Sur mon profil", f"/r/u_{OWNER}/comments/1cccccc/sur_mon_profil/", "Du texte."),
    ),
    "post_headers.csv": table(
        ("id", "permalink", "date", "ip", "subreddit", "gildings", "url"),
        ("1aaaaaa", "https://www.reddit.com/r/france/comments/1aaaaaa/une_question_sur_le_tri/",
         "2025-09-18 10:35:44 UTC", "", "france", "0", "/r/france/comments/1aaaaaa/une_question_sur_le_tri/"),
        # Only the twin lists this one.
        ("1dddddd", "https://www.reddit.com/r/AskReddit/comments/1dddddd/une_autre/", "2023-05-06 07:08:09 UTC",
         "", "AskReddit", "0", "/r/AskReddit/comments/1dddddd/une_autre/"),
    ),
    "comments.csv": table(
        COMMENT_COLUMNS,
        ("k1aaaaa", "https://www.reddit.com/r/france/comments/1zzzzzz/comment/k1aaaaa/", "2026-03-09 15:59:23 UTC",
         "", "france", "0", "0", "0", "https://www.reddit.com/r/france/comments/1zzzzzz", "",
         'Merci, c\'est "exactement" ça.\nDeuxième ligne.', ""),
        ("k2bbbbb", "https://www.reddit.com/r/france/comments/1zzzzzz/comment/k2bbbbb/", "2026-03-09 16:10:00 UTC",
         "203.0.113.9", "france", "1", "1", "0", "https://www.reddit.com/r/france/comments/1zzzzzz", "k0paren",
         "Je ne suis pas d'accord.", ""),
    ),
    "comment_headers.csv": table(
        COMMENT_COLUMNS[:-2],
        ("k1aaaaa", "https://www.reddit.com/r/france/comments/1zzzzzz/comment/k1aaaaa/", "2026-03-09 15:59:23 UTC",
         "", "france", "0", "0", "0", "https://www.reddit.com/r/france/comments/1zzzzzz", ""),
    ),
    "messages_archive.csv": table(MESSAGE_COLUMNS, *MESSAGES),
    "messages_archive_headers.csv": table(MESSAGE_COLUMNS[:-2], *(row[:-2] for row in MESSAGES)),
    "chat_history.csv": table(
        ("message_id", "created_at", "updated_at", "username", "message", "thread_parent_message_id", "channel_url",
         "subreddit", "channel_name", "conversation_type"),
        ("chat1", "2024-02-03T04:05:06Z", "", "ami_un", "Coucou en chat", "", "https://chat.reddit.com/room/abc",
         "", "ami_un", "direct"),
    ),
    "announcements.csv": table(
        ("announcement_id", "sent_at", "read_at", "from_id", "from_username", "subject", "body", "url"),
        ("ann_1", "2025-07-15T17:53:34Z", "2025-09-18T10:36:27Z", "t2_1", "reddit", "Une annonce", "Son texte.",
         "https://www.reddit.com/notifications/a/ann_1"),
        ("ann_2", "2025-10-09T23:19:11Z", "", "t2_1", "reddit", "Une autre", "Non lue.",
         "https://www.reddit.com/notifications/a/ann_2"),
    ),
    "post_votes.csv": table(
        ("id", "permalink", "direction"),
        ("1eeeeee", "https://www.reddit.com/r/france/comments/1eeeeee/whats_your_take/", "up"),
        # A title with no Latin letter in it: Reddit slugs it "_".
        ("1ffffff", "https://www.reddit.com/r/pics/comments/1ffffff/_/", "down"),
    ),
    "comment_votes.csv": table(
        ("id", "permalink", "direction"),
        ("k3ccccc", "https://www.reddit.com/r/AskReddit/comments/1gggggg/une_bonne_question/k3ccccc/", "none"),
        ("k4ddddd", "https://www.reddit.com/r/AskReddit/comments/1gggggg/une_bonne_question/k4ddddd/", "up"),
    ),
    "poll_votes.csv": table(
        ("post_id", "user_selection", "text", "image_url", "is_prediction", "stake_amount"),
        ("t3_1hhhhhh", "123456", "Option B", "", "False", ""),
    ),
    "saved_posts.csv": table(("id", "permalink"), ("1iiiiii", "https://www.reddit.com/r/france/comments/1iiiiii/un_lien_utile/")),
    "saved_comments.csv": table(
        ("id", "permalink"), ("k5eeeee", "https://www.reddit.com/r/france/comments/1iiiiii/un_lien_utile/k5eeeee/"),
    ),
    "hidden_posts.csv": table(("id", "permalink")),
    "subscribed_subreddits.csv": table(("subreddit",), ("france",), ("AskReddit",)),
    "moderated_subreddits.csv": table(("subreddit",)),
    "approved_submitter_subreddits.csv": table(("subreddit",)),
    # Empty on the real export: a row here to check where it goes.
    "friends.csv": table(("username", "note"), ("ami_un", "Rencontré à Lyon")),
    "user_preferences.csv": table(("preference", "value"), ("lang", "fr"), ("nightmode", "True"), ("default_theme_sr", "")),
    "sensitive_ads_preferences.csv": table(
        ("type", "preference"), ("show_alcohol", "True"), ("show_gambling", "False"), ("show_new_topic", "True"),
    ),
    "inferred_age.csv": table(("under_18", "under_16", "under_13", "persona_estimated_age"), ("false", "false", "false", "0")),
    "inferred_age_ads.csv": table(("Ads - Inferred User Age",), ("25-34",)),
    "inferred_gender_ads.csv": table(("Ads - Inferred User Gender",)),
    # Header-only on the real export: a row here to check it is kept whole.
    "drafts.csv": table(
        ("id", "title", "body", "kind", "created", "spoiler", "nsfw", "original_content", "content_category", "flair_id",
         "flair_text", "send_replies", "subreddit", "is_public_link"),
        ("d1", "Un brouillon", "Pas fini", "self", "2026-01-01 12:00:00 UTC", "False", "False", "False", "", "", "",
         "True", "france", "False"),
    ),
    "purchases.csv": table(("processor", "transaction_id", "product", "date", "cost", "currency", "status")),
    "subscriptions.csv": table(("processor", "subscription_id", "product", "product_id", "product_name", "status",
                                "start_date", "end_date")),
    "payouts.csv": table(("payout_amount_usd", "date", "payout_id")),
    "gilded_content.csv": table(("content_link", "award", "amount", "date")),
    "gold_received.csv": table(("content_link", "gold_received", "gilder_username", "date")),
    "multireddits.csv": table(("id", "display_name", "date", "description", "privacy", "subreddits", "image_url",
                               "is_owner", "favorited", "followers")),
    "scheduled_posts.csv": table(("scheduled_post_id", "subreddit", "title", "body", "url", "submission_time", "recurrence")),
    # A file Reddit might add one day.
    "new_feature.csv": table(("created_at", "thing"), ("2026-05-05 05:05:05 UTC", "quelque chose")),
    "checkfile.csv": table(("filename", "sha256"), ("posts.csv", "0" * 64)),
}


def write_export(root, files):
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")
    return root


@pytest.fixture()
def export(tmp_path):
    return write_export(tmp_path, FILES)


@pytest.fixture()
def items(export):
    return list(reddit.RedditImporter().parse(export))


def by_type(items, item_type):
    return [item for item in items if item.item_type == item_type]


def one(items, item_type, **match):
    found = [item for item in by_type(items, item_type) if all(getattr(item, k) == v for k, v in match.items())]
    assert len(found) == 1, found
    return found[0]


# ── Detection ──────────────────────────────────────────────────────────────

def test_detected_as_reddit_only(export):
    assert importers.detect(export).key == "reddit"
    claiming = [importer.key for importer in importers.all_importers() if importer.detect(export)]
    assert claiming == ["reddit"]


def test_a_comments_csv_from_another_platform_is_not_claimed(tmp_path):
    """LinkedIn's Complete export has a Comments.csv too -- not a Reddit one."""
    (tmp_path / "Comments.csv").write_text("Date,Link,Message\n2023-05-01 10:12:34,https://x,Hi\n", encoding="utf-8")
    assert not reddit.RedditImporter().detect(tmp_path)


def test_detected_from_one_file_of_a_partial_export(tmp_path):
    write_export(tmp_path, {"post_votes.csv": FILES["post_votes.csv"]})
    assert importers.detect(tmp_path).key == "reddit"


# ── What is read ───────────────────────────────────────────────────────────

def test_every_file_of_the_export_is_read(items):
    assert Counter(item.item_type for item in items) == {
        "profile": 1, "inference": 5, "post": 4, "comment": 2, "message": 5, "announcement": 2,
        "post_vote": 2, "comment_vote": 2, "poll_vote": 1, "saved_post": 1, "saved_comment": 1,
        "subreddit_subscription": 2, "following": 1, "ip_log": 1, "account_settings": 1, "ad_preferences": 1,
        "draft": 1, "new_feature": 1,
    }


def test_the_checksum_file_is_not_data(items):
    assert not [item for item in items if "sha256" in json.dumps(item.raw)]


def test_a_twin_adds_only_the_records_its_full_file_lacks(items):
    posts = {item.title or item.fields["Lien"] for item in by_type(items, "post")}
    assert len(by_type(items, "post")) == 4  # 1aaaaaa, in both files, once
    textless = one(items, "post", context="r/AskReddit")
    assert textless.title is None and textless.content is None
    assert textless.timestamp == datetime(2023, 5, 6, 7, 8, 9)
    assert "Une question sur le tri ?" in posts
    assert len(by_type(items, "comment")) == 2
    assert len([m for m in by_type(items, "message") if m.title]) == 4


def test_a_text_post_shares_no_link_an_image_post_does(items):
    text = one(items, "post", title="Une question sur le tri ?")
    assert "Lien partagé" not in text.fields
    assert text.content == "Bonjour à tous,\n\nune vraie question."
    assert text.context == "r/france"
    image = one(items, "post", title="Mon balcon")
    assert image.fields["Lien partagé"] == "https://i.redd.it/abcdef123.jpeg"
    assert image.fields["Adresse IP"] == "203.0.113.9"
    assert image.fields["Récompenses"] == 2
    assert image.timestamp == datetime(2026, 8, 7, 14, 56, 47)


def test_a_post_to_ones_profile_is_filed_under_the_user(items):
    assert one(items, "post", title="Sur mon profil").context == f"u/{OWNER}"


def test_a_reply_links_the_comment_it_answers(items):
    reply = one(items, "comment", content="Je ne suis pas d'accord.")
    assert reply.fields["En réponse à"] == "https://www.reddit.com/r/france/comments/1zzzzzz/comment/k0paren/"
    assert reply.fields["Publication"] == "https://www.reddit.com/r/france/comments/1zzzzzz"
    assert reply.fields["Récompenses"] == 2
    top = one(items, "comment", context="r/france", timestamp=datetime(2026, 3, 9, 15, 59, 23))
    assert "En réponse à" not in top.fields
    assert top.content == 'Merci, c\'est "exactement" ça.\nDeuxième ligne.'


def test_a_conversation_is_the_correspondent_whoever_wrote(items):
    messages = by_type(items, "message")
    contexts = Counter(m.context for m in messages if m.title)
    assert contexts == {"u/Lettre_Hebdo": 2, "u/ami_un": 2}
    reply = one(items, "message", content="Oui !")
    assert reply.author == f"u/{OWNER}" and reply.context == "u/ami_un"
    assert reply.title == "re: Salut"
    assert reply.fields["À"] == "u/ami_un"


def test_a_message_to_moderators_stays_one_conversation(tmp_path):
    """A message to a community's moderators is answered by one of them: the
    thread's first message names the conversation, not each reply's sender."""
    write_export(tmp_path, {
        "statistics.csv": FILES["statistics.csv"],
        "messages_archive.csv": table(
            MESSAGE_COLUMNS,
            ("eee555", "", "", "2023-02-01 09:00:00 UTC", "", f"/u/{OWNER}", "/r/france", "Modération", "Une question"),
            ("fff666", "", "t4_eee555", "2023-02-01 10:00:00 UTC", "", "/u/modo_france", f"/u/{OWNER}",
             "re: Modération", "Une réponse"),
        ),
    })
    messages = by_type(reddit.RedditImporter().parse(tmp_path), "message")
    assert {m.context for m in messages} == {"r/france"}


def test_the_message_permalinks_are_repaired(items):
    assert one(items, "message", content="Tu viens samedi ?").fields["Lien"] == \
        "https://www.reddit.com/message/messages/ccc333"


def test_a_chat_message_is_a_message_of_its_channel(items):
    chat = one(items, "message", content="Coucou en chat")
    assert chat.author == "u/ami_un" and chat.context == "ami_un"
    assert chat.timestamp == datetime(2024, 2, 3, 4, 5, 6)


def test_without_statistics_the_owner_is_the_one_in_every_message(tmp_path):
    write_export(tmp_path, {"messages_archive.csv": FILES["messages_archive.csv"]})
    tables = {"messages_archive.csv": reddit._read_csv(tmp_path / "messages_archive.csv")}
    assert reddit.RedditImporter._owner(tables) == f"u/{OWNER}"
    # One message: two people in all of them, so nobody is named.
    tables = {"messages_archive.csv": tables["messages_archive.csv"][:1]}
    assert reddit.RedditImporter._owner(tables) is None


def test_an_announcement_says_whether_it_was_read(items):
    read = one(items, "announcement", title="Une annonce")
    assert read.fields["Lue le"] == "18/09/2025 10:36"
    assert read.timestamp == datetime(2025, 7, 15, 17, 53, 34)
    assert read.author == "u/reddit"
    assert "Lue le" not in one(items, "announcement", title="Une autre").fields


def test_votes_are_undated_and_titled_by_their_slug(items):
    up = one(items, "post_vote", context="r/france")
    assert up.timestamp is None
    assert up.title == "Whats your take"
    assert up.fields["Vote"] == "Positif"
    assert one(items, "post_vote", context="r/pics").title is None  # the "_" slug
    withdrawn = [v for v in by_type(items, "comment_vote") if v.fields["Vote"] == "Retiré"]
    assert len(withdrawn) == 1 and withdrawn[0].title == "Une bonne question"


def test_saves_and_a_poll_vote(items):
    assert one(items, "saved_post").title == "Un lien utile"
    assert one(items, "saved_comment").context == "r/france"
    poll = one(items, "poll_vote")
    assert poll.content == "Option B"
    assert poll.fields == {"Sondage": "https://www.reddit.com/comments/1hhhhhh/"}


def test_subscriptions_and_a_friend(items):
    assert {item.title for item in by_type(items, "subreddit_subscription")} == {"r/france", "r/AskReddit"}
    friend = one(items, "following")
    assert friend.title == "u/ami_un" and friend.category == "Réseau"


def test_the_profile_folds_the_account_files_in(items):
    profile = one(items, "profile")
    assert profile.title == f"u/{OWNER}"
    assert profile.timestamp == datetime(2019, 3, 4, 10, 0)
    assert profile.fields == {
        "E-mail": "owner@example.com", "E-mail vérifié": "Oui", "Compte supprimé": "Non",
        "IP d'inscription": "198.51.100.7", "Genre": "not specified", "Comptes liés": "https://accounts.google.com",
    }


def test_the_ip_log_leaves_the_registration_row_to_the_profile(items):
    log = one(items, "ip_log")
    assert log.fields == {"Adresse IP": "203.0.113.9"}
    assert log.timestamp == datetime(2026, 8, 7, 15, 3, 45)


def test_settings_ads_and_inferences(items):
    assert one(items, "account_settings").fields == {"lang": "fr", "nightmode": "True"}
    assert one(items, "ad_preferences").fields == {"Alcool": "Autorisées", "Jeux d'argent": "Limitées",
                                                   "show_new_topic": "Autorisées"}
    inferences = {item.title: item.fields["Valeur"] for item in by_type(items, "inference")}
    assert inferences == {
        "Âge déduit pour la publicité": "25-34", "Moins de 18 ans": "Non", "Moins de 16 ans": "Non",
        "Moins de 13 ans": "Non", "Âge estimé (Persona)": "0",
    }


def test_a_header_only_file_is_kept_whole_and_an_unknown_one_too(items):
    draft = one(items, "draft")
    assert draft.title == "Un brouillon" and draft.context == "r/france"
    assert draft.timestamp == datetime(2026, 1, 1, 12, 0)
    assert draft.fields["kind"] == "self"
    assert "flair_id" not in draft.fields  # empty columns are left out
    future = one(items, "new_feature")
    assert future.timestamp == datetime(2026, 5, 5, 5, 5, 5)
    assert future.fields["thing"] == "quelque chose"


# ── Dedup and views ────────────────────────────────────────────────────────

def signature(item_type, fields, **columns):
    return dedup.item_signature("reddit", item_type, columns.get("category"), columns.get("timestamp"),
                                columns.get("title"), columns.get("content"), None, columns.get("context"),
                                json.dumps(fields))


def test_a_vote_changed_since_the_last_export_is_the_same_vote():
    link = {"Lien": "https://www.reddit.com/r/france/comments/1eeeeee/whats_your_take/"}
    assert signature("post_vote", {**link, "Vote": "Positif"}) == signature("post_vote", {**link, "Vote": "Retiré"})


def test_an_award_or_a_reading_since_does_not_double_a_record():
    assert signature("comment", {"Lien": "x"}, content="a") == signature("comment", {"Lien": "x", "Récompenses": 1}, content="a")
    assert signature("announcement", {"Lien": "x"}) == signature("announcement", {"Lien": "x", "Lue le": "01/01/2026 00:00"})


def test_the_platform_offers_posts_messages_and_network(items):
    views = [view.key for view in platforms.available_views({item.item_type for item in items})]
    assert {"posts", "messaging", "conversations", "network"} <= set(views)
    assert "purchases" not in views
    assert platforms.get("reddit").label == "Reddit"


# ── The anonymized copy ────────────────────────────────────────────────────

def test_the_anonymized_copy_keeps_nothing_of_the_export(export, tmp_path_factory):
    out = tmp_path_factory.mktemp("anon")
    assert anonymize.anonymize_export(export, out / "anon.zip", source_key="reddit") == []
    with zipfile.ZipFile(out / "anon.zip") as zf:
        zf.extractall(out / "files")
    assert importers.detect(out / "files").key == "reddit"
    blob = "\n".join(path.read_text(encoding="utf-8") for path in (out / "files").glob("*.csv"))
    for leaked in (OWNER, "owner@example.com", "203.0.113.9", "198.51.100.7", "Une question sur le tri",
                   "Tu viens samedi", "Lettre_Hebdo", "whats_your_take", "Rencontré à Lyon"):
        assert leaked not in blob
    # "sent_at" / "read_at" are dates, shifted rather than turned into text.
    announcement = next(csv.DictReader(io.StringIO((out / "files" / "announcements.csv").read_text(encoding="utf-8"))))
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", announcement["sent_at"])
    assert announcement["sent_at"] != "2025-07-15T17:53:34Z"


# ── The pages themselves ───────────────────────────────────────────────────

# Messages only received: the owner wrote in no conversation, so guessing them
# from who wrote the most could only land on a sender. The textless twin too,
# or the owner's reply would come back from it.
RECEIVED_ONLY = {
    **FILES,
    "messages_archive.csv": table(MESSAGE_COLUMNS, *MESSAGES[:3]),
    "messages_archive_headers.csv": table(MESSAGE_COLUMNS[:-2], *(row[:-2] for row in MESSAGES[:3])),
}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from fastapi.testclient import TestClient

    from app import processing
    from app.database import Base, SessionLocal, engine
    from app.main import app
    from app.models import ImportBatch, ImportStatus

    root = write_export(tmp_path_factory.mktemp("reddit-export"), RECEIVED_ONLY)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    batch = ImportBatch(original_filename="reddit_export.zip", status=ImportStatus.parsing)
    db.add(batch)
    db.commit()
    processing._detect_and_parse(db, batch, root)
    db.close()
    return TestClient(app)


def test_the_owner_is_the_account_the_export_names(client):
    from app import crud
    from app.database import SessionLocal

    with SessionLocal() as db:
        assert crud.get_me_candidate(db, "reddit") == f"u/{OWNER}"


def test_a_received_message_is_not_shown_as_sent(client):
    for context, text in (("u/Lettre_Hebdo", "les nouvelles du mois"), ("u/ami_un", "Tu viens samedi")):
        html = client.get("/p/reddit/conversations", params={"context": context}).text
        assert text in html
        assert "chat-bubble-mine" not in html


def test_the_platform_pages_render(client):
    html = client.get("/p/reddit").text
    for tab in ("posts", "messaging", "conversations", "network", "activity"):
        assert f'href="/p/reddit/{tab}"' in html
        assert client.get(f"/p/reddit/{tab}").status_code == 200
    assert "Abonnements" in client.get("/p/reddit/network").text
    assert "u/ami_un" in client.get("/p/reddit/panel", params={"item_type": "following"}).text
