import json
import sys
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import anonymize, dedup, importers, stats  # noqa: E402
from app.importers import linkedin  # noqa: E402

# A made-up export in the shapes of a real one (headers, preamble, dates).
FILES = {
    "Connections.csv": (
        "Notes:\n"
        '"When exporting your connection data, you may notice that some of the email addresses are missing."\n'
        "\n"
        "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
        "Alice,Martin,https://www.linkedin.com/in/alice-martin,,Acme,Data Engineer,08 Oct 2026\n"
        "Bruno,Petit,https://www.linkedin.com/in/bruno-petit,bruno@example.com,Acme,CTO,12 Jan 2015\n"
        "Chloé,Durand,https://www.linkedin.com/in/chloe-durand,,Globex,Designer,03 Mar 2019\n"
        ",,,,,,05 May 2020\n"
    ),
    "Invitations.csv": (
        "From,To,Sent At,Message,Direction,inviterProfileUrl,inviteeProfileUrl\n"
        "Denis Roux,Owner Name,\"10/8/26, 9:45 AM\",,INCOMING,https://www.linkedin.com/in/denis,https://www.linkedin.com/in/owner\n"
        "Owner Name,Emma Blanc,\"1/15/26, 11:02 PM\",Bonjour Emma,OUTGOING,https://www.linkedin.com/in/owner,https://www.linkedin.com/in/emma\n"
    ),
    "Company Follows.csv": "Organization,Followed On\nAcme,Thu Oct 08 10:12:34 UTC 2020\n",
    "Profile.csv": "First Name,Last Name,Headline,Summary\nOwner,Name,Ingénieur données,Un résumé\n",
    "Positions.csv": (
        "Company Name,Title,Description,Location,Started On,Finished On\n"
        "Acme,Lead Data Engineer,,Paris,Jan 2022,\n"
        "Acme,Data Engineer,Pipelines,Paris,Oct 2018,Dec 2021\n"
        "Freelance,Consultant,,,Jun 2021,Mar 2022\n"
    ),
    "Education.csv": "School Name,Start Date,End Date,Notes,Degree Name,Activities\nÉcole X,2013,2016,,Ingénieur,\n",
    "Endorsement_Received_Info.csv": (
        "Endorsement Date,Skill Name,Endorser First Name,Endorser Last Name,Endorser Public Url,Endorsement Status\n"
        "2020/01/23 10:12:34 UTC,Python,Bruno,Petit,www.linkedin.com/in/bruno-petit,ACCEPTED\n"
        "2021/02/01 08:00:00 UTC,Python,Chloé,Durand,www.linkedin.com/in/chloe-durand,ACCEPTED\n"
        "2021/02/02 08:00:00 UTC,SQL,Bruno,Petit,www.linkedin.com/in/bruno-petit,ACCEPTED\n"
    ),
    "Endorsement_Given_Info.csv": (
        "Endorsement Date,Skill Name,Endorsee First Name,Endorsee Last Name,Endorsee Public Url,Endorsement Status\n"
        "2019/05/05 12:00:00 UTC,Design,Chloé,Durand,www.linkedin.com/in/chloe-durand,PENDING\n"
    ),
    "Learning.csv": (
        "Content Title,Content Description,Content Type,Content Last Watched Date (if viewed),"
        "Content Completed At (if completed),Content Saved,Notes taken on videos (if taken),\n"
        "Apprendre SQL,<a>Une</a> intro</br>en SQL,Course,2023-05-01 10:12 UTC,N/A,false,N/A,\n"
    ),
    "Events.csv": (
        "Event Name,Event Time,Status,External Url\n"
        "Meetup data,\"Mar 12, 2023 10:00 AM - Mar 12, 2023 11:30 AM\",APPROVED,https://example.com/e\n"
    ),
    "Rich_Media.csv": (
        "Date/Time,Media Description,Media Link\n"
        "\"You uploaded a feed photo on July 12, 2020 at 9:45 PM (GMT)\",Une photo,\n"
        "\"You uploaded a profile photo on June 3, 2021 at 10:05 AM (GMT)\", -,https://media.example.com/p\n"
    ),
    "Ad_Targeting.csv": "Member Age,Job Titles,Job Titles\n,Engineer; Analyst,Engineer; Analyst\n",
    "Profile Summary.csv": "Profile Summary\n\n",
    "Jobs/Saved Jobs.csv": "Saved Date,Job Url,Job Title,Company Name\n\"10/27/24, 9:05 AM\",https://example.com/j,Data Engineer,Initech\n",
    "Some_New_File.csv": "Created On,Thing\n08 Oct 2026,Valeur\n",
    "messages.csv": (
        "CONVERSATION ID,CONVERSATION TITLE,FROM,SENDER PROFILE URL,TO,RECIPIENT PROFILE URLS,DATE,SUBJECT,CONTENT,FOLDER\n"
        "c1,,Alice Martin,u,Owner Name,u,2023-04-12 08:31:22 UTC,,Salut,INBOX\n"
        "c1,,Owner Name,u,Alice Martin,u,2023-04-12 08:40:00 UTC,,Hello,INBOX\n"
        "c2,Projet,Owner Name,u,\"Alice Martin, Bruno Petit\",u,2023-05-01 09:00:00 UTC,,Point,INBOX\n"
    ),
    # The Complete export's activity files, suffixed with the member's id.
    "Shares_123456789.csv": (
        "Date,ShareLink,ShareCommentary,SharedUrl,MediaUrl,Visibility\n"
        "2024-02-03 08:15:00,https://www.linkedin.com/feed/update/urn%3Ali%3Ashare%3A1,\"Un \"\"vrai\"\" post\n"
        "sur deux lignes\",,,MEMBER_NETWORK\n"
        # A backslash right before a closing quote: plain text here.
        "2024-02-06 08:15:00,https://www.linkedin.com/feed/update/urn%3Ali%3Ashare%3A7,\"Dossier C:\\temp\\\",,,MEMBER_NETWORK\n"
        "2024-02-07 08:15:00,https://www.linkedin.com/feed/update/urn%3Ali%3Ashare%3A8,Suivant,,,MEMBER_NETWORK\n"
    ),
    "Comments_123456789.csv": (
        "Date,Link,Message\n"
        "2024-02-04 09:00:00,https://www.linkedin.com/feed/update/urn%3Ali%3Aactivity%3A2,\"Bravo pour ce \\\"projet\\\",\n"
        "vraiment\"\n"
        "2024-02-05 10:00:00,https://www.linkedin.com/feed/update/urn%3Ali%3Aactivity%3A3,Merci\n"
    ),
    "Reactions_123456789.csv": (
        "Date,Type,Link\n"
        "2021-03-01 12:00:00,MAYBE,https://www.linkedin.com/feed/update/urn%3Ali%3Aactivity%3A4\n"
        "2023-03-01 12:00:00,ENTERTAINMENT,https://www.linkedin.com/feed/update/urn%3Ali%3Aactivity%3A5\n"
    ),
    "Member_Follows_123456789.csv": (
        "Date,Status,FullName\n"
        "2022-01-01 10:00:00,Active,Alice Martin\n"
        "2023-01-01 10:00:00,Unfollow,Bruno Petit\n"
        "2024-01-01 10:00:00,Paused,Chloé Durand\n"
    ),
    "Hashtag_Follows_123456789.csv": (
        "HashTag,CreatedTime,LastModifiedTime,State\n"
        "data,2014-12-01 10:00:00,2014-12-01 10:00:00,Follow\n"
        "#python,2015-01-01 10:00:00,2020-01-01 10:00:00,Unfollow\n"
    ),
    "Saved_Items_123456789.csv": "savedItem,CreatedTime\nhttps://www.linkedin.com/feed/update/urn:li:activity:6,2024-05-06 07:00:00\n",
    "Ads Clicked.csv": "Ad clicked Date,Ad Title/Id\n2026/01/02 10:11:12 UTC,123456\n2026/01/02 10:11:12 UTC,123456\n",
    "Security Challenges.csv": (
        "Challenge Date,IP Address,User Agent,Country,Challenge Type\n"
        "Thu May 02 10:00:00 UTC 2024,192.0.2.1,Mozilla/5.0,France,EMAIL_PIN_CHALLENGE\n"
    ),
    "Inferences_about_you.csv": (
        "Category,Type of inference,Description,Inference\n"
        "Career inferences,Data professional,Whether we think you work with data.,Yes\n"
    ),
    "Inferred Credibility Scores.csv": (
        "ScoreType,Topic,Category,Range,2026-10-08,2026-10-05,2026-09-28\n"
        "Knowledge,Data Engineering,Topic score,,,-0.4512345678901234,2.8\n"
        "Knowledge,Data Engineering,Topic normalized score,\"1-4 (continuous, 2 decimals)\",,2.9512345678901234,\n"
        "Notability,,Public reach,0-5 (integer),2,,1\n"
        "Knowledge,Empty Topic,Topic score,1-4 (integer),,,\n"
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
    return list(linkedin.LinkedInImporter().parse(export))


def by_type(items, item_type):
    return [i for i in items if i.item_type == item_type]


def test_detected_as_linkedin_only(export):
    assert importers.detect(export).key == "linkedin"


@pytest.mark.parametrize("value, expected", [
    ("08 Oct 2026", datetime(2026, 10, 8)),
    ("Oct 2020", datetime(2020, 10, 1)),
    ("2016", datetime(2016, 1, 1)),
    ("10/8/26, 9:45 AM", datetime(2026, 10, 8, 9, 45)),
    ("2020/01/23 10:12:34 UTC", datetime(2020, 1, 23, 10, 12, 34)),
    ("2023-05-01 10:12 UTC", datetime(2023, 5, 1, 10, 12)),
    ("Thu Oct 08 10:12:34 UTC 2020", datetime(2020, 10, 8, 10, 12, 34)),
    ("Mar 12, 2023 10:00 AM", datetime(2023, 3, 12, 10, 0)),
    ("2024-05-01", datetime(2024, 5, 1)),
    ("N/A", None),
    ("", None),
])
def test_date_formats(value, expected):
    assert linkedin._parse_date(value) == expected


def test_connections_past_the_notes_preamble(items):
    connections = by_type(items, "connection")
    assert [c.title for c in connections] == ["Alice Martin", "Bruno Petit", "Chloé Durand", None]
    assert connections[0].timestamp == datetime(2026, 10, 8)
    assert connections[1].fields == {
        "Profil": "https://www.linkedin.com/in/bruno-petit", "Entreprise": "Acme", "Poste": "CTO",
        "E-mail": "bruno@example.com",
    }
    # A hidden member: only the date, still a connection.
    assert connections[3].fields is None and connections[3].timestamp == datetime(2020, 5, 5)


def test_invitations_split_by_direction_named_by_the_other_side(items):
    received, = by_type(items, "invitation_received")
    sent, = by_type(items, "invitation_sent")
    assert (received.title, received.fields["Profil"]) == ("Denis Roux", "https://www.linkedin.com/in/denis")
    assert (sent.title, sent.content, sent.timestamp) == ("Emma Blanc", "Bonjour Emma", datetime(2026, 1, 15, 23, 2))


def test_career_records(items):
    current, = [p for p in by_type(items, "position") if p.title == "Lead Data Engineer"]
    assert current.fields["Fin"] == "En cours" and current.fields["Début"] == "01/2022" and current.context == "Acme"
    education, = by_type(items, "education")
    assert (education.fields["Début"], education.fields["Fin"], education.timestamp) == ("2013", "2016", datetime(2013, 1, 1))


def test_endorsements_skill_as_title_person_as_author_or_context(items):
    received = by_type(items, "endorsement_received")
    assert [(e.title, e.author) for e in received][:1] == [("Python", "Bruno Petit")]
    assert received[0].fields == {"Profil": "https://www.linkedin.com/in/bruno-petit", "Statut": "Acceptée"}
    given, = by_type(items, "endorsement_given")
    assert (given.title, given.context, given.author, given.fields["Statut"]) == ("Design", "Chloé Durand", None, "En attente")


def test_learning_events_media(items):
    course, = by_type(items, "learning_course")
    assert course.content == "Une intro\nen SQL" and "Terminé le" not in course.fields and course.fields["Enregistré"] == "Non"
    event, = by_type(items, "event")
    assert event.timestamp == datetime(2023, 3, 12, 10, 0) and event.fields["Fin"] == "12/03/2023 11:30"
    feed, profile = by_type(items, "media_upload")
    assert (feed.title, feed.timestamp, feed.content) == ("Photo publiée", datetime(2020, 7, 12, 21, 45), "Une photo")
    assert (profile.title, profile.content, profile.fields) == ("Photo de profil", None, {"Lien": "https://media.example.com/p"})


def test_repeated_columns_merged_and_empty_rows_dropped(items):
    targeting, = by_type(items, "ad_targeting")
    assert targeting.fields == {"Job Titles": "Engineer; Analyst"}
    assert by_type(items, "profile_summary") == []


def test_unknown_file_kept_by_the_generic_parser(items):
    row, = by_type(items, "some_new_file")
    assert (row.category, row.timestamp, row.fields) == ("Autres données", datetime(2026, 10, 8), {"Created On": "08 Oct 2026", "Thing": "Valeur"})
    saved, = by_type(items, "saved_job")
    assert saved.category == "Emploi" and saved.timestamp == datetime(2024, 10, 27, 9, 5)


def test_messages_threaded_by_title_or_other_participants(items):
    messages = by_type(items, "message")
    assert [(m.author, m.context) for m in messages] == [
        ("Alice Martin", "Alice Martin"), ("Owner Name", "Alice Martin"), ("Owner Name", "Projet"),
    ]


def test_member_id_suffixed_files_use_their_dedicated_mapping(items, tmp_path):
    assert not any(any(c.isdigit() for c in i.item_type) for i in items)
    posts = by_type(items, "post")
    assert [p.content for p in posts] == ['Un "vrai" post\nsur deux lignes', "Dossier C:\\temp\\", "Suivant"]
    assert posts[0].timestamp == datetime(2024, 2, 3, 8, 15)
    saved, = by_type(items, "saved_item")
    assert saved.category == "Publications" and saved.fields == {"Lien": "https://www.linkedin.com/feed/update/urn:li:activity:6"}


def test_detected_on_a_suffixed_file_alone(tmp_path):
    (tmp_path / "Shares_42424242.csv").write_text(FILES["Shares_123456789.csv"], encoding="utf-8")
    assert importers.detect(tmp_path).key == "linkedin"


def test_comments_with_backslash_escaped_quotes(items):
    first, second = by_type(items, "comment")
    assert first.content == 'Bravo pour ce "projet",\nvraiment'
    assert (second.content, second.timestamp) == ("Merci", datetime(2024, 2, 5, 10, 0))


def test_reactions_named_as_in_linkedin(items):
    assert [r.content for r in by_type(items, "reaction")] == ["Curieux", "Amusant"]


def test_unfollowed_members_are_not_counted_as_followed(items):
    followed, unknown = by_type(items, "member_follow")
    unfollowed, = by_type(items, "member_unfollow")
    assert (followed.title, followed.fields, unfollowed.title) == ("Alice Martin", None, "Bruno Petit")
    # A status LinkedIn may add later stays visible.
    assert unknown.fields == {"Statut": "Paused"}
    snapshot = stats.network_snapshot(_connection_rows(items, 1), {"member_follow": [1]})
    assert [p["name"] for p in snapshot["following"]["members"].values()] == ["Alice Martin", "Chloé Durand"]
    hashtag, = by_type(items, "hashtag_follow")
    dropped, = by_type(items, "hashtag_unfollow")
    assert (hashtag.title, hashtag.fields, dropped.title) == ("#data", {"Modifié le": "01/12/2014"}, "#python")


def test_ads_security_and_inferences(items):
    clicks = by_type(items, "ad_click")
    assert len(clicks) == 2 and clicks[0].timestamp == datetime(2026, 1, 2, 10, 11, 12) and clicks[0].fields == {"Annonce": "123456"}
    challenge, = by_type(items, "security_challenge")
    assert (challenge.timestamp, challenge.content, challenge.fields["Pays"]) == (datetime(2024, 5, 2, 10, 0), "Code reçu par e-mail", "France")
    inference, = by_type(items, "inference")
    assert (inference.category, inference.title, inference.fields["Valeur"]) == ("Inférences", "Data professional", "Yes")
    revised = linkedin.LinkedInImporter()._inference({**inference.raw, "Inference": "No"})
    assert _signature(revised) == _signature(inference)


def test_credibility_scores_keep_their_latest_value(items):
    raw, normalized, reach = by_type(items, "credibility_score")
    assert (raw.title, raw.context, raw.timestamp, "Échelle" in raw.fields) == ("Data Engineering", "Topic score", None, False)
    assert (raw.fields["Score"], raw.fields["Calculé le"]) == ("-0,45", "05/10/2026")
    assert raw.fields["Historique"] == "05/10/2026 : -0,45 · 28/09/2026 : 2,8"
    assert (normalized.context, normalized.fields["Score"]) == ("Topic normalized score", "2,95")
    assert (reach.title, reach.context, reach.fields["Type"], reach.fields["Score"]) == ("Public reach", None, "Notoriété", "2")
    # Next week's export: new score, same record.
    rescored = list(linkedin.LinkedInImporter()._credibility_scores([{**raw.raw, "2026-10-12": "3.1"}]))[0]
    assert rescored.fields["Score"] == "3,1" and _signature(rescored) == _signature(raw)


def _signature(item):
    return dedup.item_signature(
        "linkedin", item.item_type, item.category, item.timestamp, item.title, item.content, item.author, item.context,
        json.dumps(item.fields) if item.fields else None,
    )


def test_a_connection_changing_jobs_is_the_same_record(items):
    before = by_type(items, "connection")[1]
    after = linkedin.LinkedInImporter()._connection({
        "First Name": "Bruno", "Last Name": "Petit", "URL": "https://www.linkedin.com/in/bruno-petit",
        "Email Address": "", "Company": "Globex", "Position": "CEO", "Connected On": "12 Jan 2015",
    })
    assert _signature(before) == _signature(after)


def test_a_course_watched_again_is_the_same_record(items):
    course = by_type(items, "learning_course")[0]
    again = linkedin.LinkedInImporter()._learning_course({**course.raw, "Content Last Watched Date (if viewed)": "2024-01-01 10:00 UTC"})
    assert again.timestamp != course.timestamp and _signature(again) == _signature(course)


def _career_rows(items):
    return [
        (i, item.timestamp, item.item_type, item.title, item.author, item.context,
         json.dumps(item.fields) if item.fields else None)
        for i, item in enumerate(items)
        if item.item_type in stats.CAREER_ITEM_TYPES
    ]


def test_career_timeline_counts_overlapping_jobs_once(items):
    timeline = stats.build_career_timeline(_career_rows(items), today=datetime(2026, 10, 9))
    # Oct 2018 -> Oct 2026 included, the freelance overlap counted once.
    assert timeline["experience_months"] == 8 * 12 + 1
    assert (timeline["positions"], timeline["companies"]) == (3, 2)
    first = timeline["entries"][0]
    assert (first["title"], first["ongoing"], first["when"]) == ("Lead Data Engineer", True, "01/2022 – aujourd'hui · 4 ans 10 mois")
    studies, = [e for e in timeline["entries"] if e["item_type"] == "education"]
    assert studies["when"] == "2013 – 2016" and (studies["start"], studies["end"]) == (datetime(2013, 1, 1), datetime(2016, 1, 1))
    assert timeline["ticks"][0] == {"label": 2013, "left": 0.0}
    assert all(0 <= e["left"] and e["left"] + e["width"] <= 100.001 for e in timeline["entries"])


def test_career_dashboard_ranks_skills_and_people(items):
    dashboard = stats.build_career_dashboard(_career_rows(items), "year", None, None)
    assert dashboard["skills"] == [{"label": "Python", "count": 2}, {"label": "SQL", "count": 1}]
    assert dashboard["endorsers"][0] == {"label": "Bruno Petit", "count": 2}
    assert dashboard["endorsees"] == [{"label": "Chloé Durand", "count": 1}]
    received = [s for s in dashboard["endorsements"]["series"] if s["keys"] == ["endorsement_received"]][0]
    assert dict(zip(dashboard["endorsements"]["labels"], received["data"])) == {"2019": 0, "2020": 1, "2021": 2}


def _connection_rows(items, batch_id, start_id=0):
    return [
        (start_id + i, item.timestamp, batch_id, item.item_type, item.title, json.dumps(item.fields) if item.fields else None)
        for i, item in enumerate(items) if item.category == "Réseau"
    ]


def test_network_snapshot_companies_and_no_false_departure_for_hidden_members(items):
    rows = _connection_rows(items, 1) + _connection_rows(items, 2, start_id=100)
    snapshot = stats.network_snapshot(rows, {"connection": [1, 2], "company_follow": [1, 2]})
    assert len(snapshot["connections"]["members"]) == 4
    assert snapshot["connections"]["lost"] == {}
    companies = stats.network_companies(snapshot)
    assert companies["top"] == [{"label": "Acme", "count": 2}, {"label": "Globex", "count": 1}]
    assert sorted(p["name"] for p in stats.company_people(snapshot, "Acme")) == ["Alice Martin", "Bruno Petit"]


def test_dates_without_a_time_stay_out_of_the_heatmap():
    events = [(datetime(2024, 3, 4), "Réseau"), (datetime(2024, 3, 4, 9, 30), "Réseau")]
    grid = stats.build_heatmap(events)
    assert sum(map(sum, grid)) == 1 and grid[0][9] == 1
    assert stats.most_active(events)[1] == [(9, 1)]


def test_anonymized_connections_keep_their_header_and_dates(export):
    anonymizer = anonymize.Anonymizer(anonymize.PathAnonymizer(anonymize.Faker("fr_FR")))
    text = anonymize._anonymize_csv(anonymizer, FILES["Connections.csv"])
    (export / "Connections.csv").write_text(text, encoding="utf-8")
    rows = linkedin._read_csv(export / "Connections.csv")
    assert list(rows[0]) == ["First Name", "Last Name", "URL", "Email Address", "Company", "Position", "Connected On"]
    assert all(linkedin._parse_date(r["Connected On"]) for r in rows)
    assert "Alice" not in text and "When exporting" not in text
    assert Counter(r["Connected On"] == "08 Oct 2026" for r in rows)[True] == 0


def test_anonymized_complete_export_keeps_its_comments_and_hides_the_member_id(export, tmp_path_factory):
    out = tmp_path_factory.mktemp("anon")
    assert anonymize.anonymize_export(export, out / "anon.zip", source_key="linkedin") == []
    with zipfile.ZipFile(out / "anon.zip") as zf:
        names = zf.namelist()
        zf.extractall(out / "files")
    assert not any("123456789" in n for n in names)
    comments, = [n for n in names if n.startswith("Comments_")]
    rows = linkedin._read_csv(out / "files" / comments)
    assert len(rows) == 2 and all(linkedin._parse_date(r["Date"]) for r in rows)
    assert "projet" not in (out / "files" / comments).read_text(encoding="utf-8")
    # The anonymized copy still imports as a Complete export.
    assert len(by_type(linkedin.LinkedInImporter().parse(out / "files"), "comment")) == 2


def test_namesakes_both_counted_and_company_spellings_merged():
    def row(id_, name, url, company):
        return (id_, datetime(2020, 1, 1), 1, "connection", name,
                json.dumps({"Profil": url, "Entreprise": company}))
    rows = [
        row(1, "Thomas Martin", "https://www.linkedin.com/in/tm-1", "Globex-Corp"),
        row(2, "Thomas Martin", "https://www.linkedin.com/in/tm-2", "Globex - Corp"),
        row(3, "Léa Roux", "https://www.linkedin.com/in/lea", "globex-corp"),
    ]
    snapshot = stats.network_snapshot(rows, {"connection": [1]})
    assert len(snapshot["connections"]["members"]) == 3
    assert stats.network_companies(snapshot)["top"] == [{"label": "Globex-Corp", "count": 3}]
    assert len(stats.company_people(snapshot, "Globex - Corp")) == 3


def test_dates_without_a_time_dont_add_to_the_time_spent():
    events = [(datetime(2024, 3, d), "Réseau") for d in range(1, 11)] + [(datetime(2024, 3, 4, 9, 30), "Réseau")]
    assert stats.estimate_time_spent_minutes(events) == stats.MIN_SESSION_MINUTES
