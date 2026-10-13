"""LinkedIn's "Get a copy of your data" export (Settings > Data privacy).

LinkedIn delivers it in two archives: a "Basic" one within minutes
(Basic_LinkedInDataExport_<date>.zip: profile, connections, invitations,
endorsements, positions, Learning...) and a "Complete" one about a day later
that repeats those files byte for byte and adds shares, comments, reactions,
searches, logins, ad clicks, inferred scores... Both are flat folders of CSV
files, one per category, with English headers whatever the account's
language. Imported one after the other, the repeated files are folded by
the cross-import dedup.

Every file and every column used below are confirmed on a real export of
each kind (10/2026) -- except messages.csv, which that Complete export
didn't carry: it is mapped from its publicly documented header. All are read
with the same tolerance (several column names tried, case-insensitive,
never a required column). Any CSV without a dedicated mapping is still
imported row by row (`_parse_generic`) rather than dropped: LinkedIn adds
files over time.

Quirks of the real export:
- The Complete export suffixes its activity files with the member's id
  ("Shares_387121575.csv"): files are matched on their name without it.
- Comments.csv escapes the quotes inside a comment as \\" instead of
  doubling them, unlike every other file (csv_escapechar).
- Connections.csv opens with a "Notes:" paragraph and a blank line before
  its header row.
- Each file has its own date format: "08 Oct 2026" (Connections), "Oct 2020"
  (Positions), "2020" (Education), "Thu Oct 08 10:12:34 UTC 2020" (Company
  Follows, Logins), "10/8/26, 9:45 AM" (month first: Invitations, Jobs,
  Recommendations), "2020/10/08 10:12:34 UTC" (Endorsements, searches, ad
  clicks), "2023-05-01 10:12 UTC" (Learning), "2023-05-01 10:12:34"
  (shares, comments, reactions...), a start-end range (Events) and a
  sentence (Rich_Media: "You uploaded a feed photo on July 12, 2020 at
  9:45 PM (GMT)"). All are read by _parse_date; times are UTC, kept naive
  like Amazon's.
- A date known only to the month or the year (a position, a degree) reads
  as the 1st at midnight: placed on the time axis, but with no time of day
  (stats.has_time_of_day), so it stays out of the hour heatmaps.
- Ad_Targeting.csv repeats some column names; repeated columns are merged.

The fields that legitimately change between two exports of the same record
(a connection's employer, an endorsement accepted since, a course watched
again) are listed in dedup.VOLATILE_FIELDS, so importing a new export every
year doesn't count those records twice.
"""
import csv
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

from bs4 import BeautifulSoup

from app.importers.base import BaseImporter, NormalizedItem
from app.importers.meta_common import humanize
from app.importers.registry import register

Row = Dict[str, str]

# The csv module rejects a cell over 128 KB, failing the whole file: one long
# InMail would cost every message. Ad_Targeting.csv already holds a 25 KB cell.
csv.field_size_limit(max(csv.field_size_limit(), 64 * 1024 * 1024))

# File name (_file_key) -> a header column only LinkedIn's version of that
# file has. Detection reads headers rather than trusting names alone:
# "Profile.csv" or "Connections.csv" are generic enough to appear in another
# export.
_SIGNATURES = {
    "connections.csv": "connected on",
    "invitations.csv": "inviterprofileurl",
    "endorsement_received_info.csv": "endorser public url",
    "positions.csv": "started on",
    "profile.csv": "headline",
    "messages.csv": "conversation id",
    "shares.csv": "sharecommentary",
}
# "Shares_387121575.csv": the member id the Complete export appends (also
# replaced by the anonymizer).
MEMBER_ID_SUFFIX_RE = re.compile(r"_\d{5,}(?=\.csv$)", re.IGNORECASE)
# Files written with \" for a quote inside a value rather than "" -- see
# csv_escapechar.
_BACKSLASH_ESCAPED_FILES = {"comments.csv"}

# Categories, in French like Amazon's -- the network ones are what the
# "Réseau" tab reads (stats.NETWORK_CATEGORIES).
NETWORK = "Réseau"
CAREER = "Parcours"
SKILLS = "Compétences"
RECOMMENDATIONS = "Recommandations"
PROFILE = "Profil"
JOBS = "Emploi"
LEARNING = "Formation"
EVENTS = "Événements"
MEDIA = "Médias"
ADS = "Publicité"
MESSAGES = "Messages"
POSTS = "Publications"
COMMENTS = "Commentaires"
REACTIONS = "Réactions"
SEARCHES = "Recherches"
SECURITY = "Sécurité"
INFERENCES = "Inférences"
OTHER = "Autres données"

# Files without a dedicated mapping (empty on the sample export, or not in
# it), still worth a readable category over the generic one (item type =
# slug of the name).
_GENERIC_CATEGORIES = {
    "lan_ads_engagement": ADS,
    "receipts": "Achats",
    "receipts_v2": "Achats",
    "groups": NETWORK,
    "learningcoachmessages": LEARNING,
    "job_applicant_saved_answers": JOBS,
    "job_applicant_saved_screening_question_responses": JOBS,
}
_FOLDER_CATEGORIES = {"jobs": JOBS, "verifications": PROFILE}

# Endorsements and recommendations: LinkedIn's status codes in French.
_STATUS_LABELS = {"ACCEPTED": "Acceptée", "PENDING": "En attente", "REJECTED": "Refusée", "VISIBLE": "Visible", "HIDDEN": "Masquée"}

# Reactions.csv "Type" -> the reaction's name in LinkedIn's French interface.
# MAYBE is "Curious", retired in 2022 for "Funny" (ENTERTAINMENT): the real
# export has no MAYBE after 01/2022 and no ENTERTAINMENT before 10/2022.
_REACTION_LABELS = {
    "LIKE": "J'aime", "PRAISE": "Bravo", "APPRECIATION": "Soutien", "EMPATHY": "J'adore",
    "INTEREST": "Instructif", "ENTERTAINMENT": "Amusant", "MAYBE": "Curieux",
}

# Security Challenges.csv "Challenge Type", where its meaning is plain.
_CHALLENGE_LABELS = {"EMAIL_PIN_CHALLENGE": "Code reçu par e-mail", "LINKEDIN_APP_CHALLENGE": "Validation dans l'appli LinkedIn"}
# Member_Follows.csv "Status" / Hashtag_Follows.csv "State" values.
_FOLLOWING_STATES = {"ACTIVE", "FOLLOW"}
_UNFOLLOWED_STATE = "UNFOLLOW"
# Inferred Credibility Scores.csv "ScoreType".
_SCORE_TYPES = {"KNOWLEDGE": "Connaissances", "NOTABILITY": "Notoriété"}
_ISO_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Rich_Media.csv describes each upload in a sentence -- what was uploaded.
_MEDIA_KINDS = {
    "feed photo": "Photo publiée", "profile photo": "Photo de profil", "background photo": "Photo d'arrière-plan",
    "photo": "Photo", "document": "Document", "video": "Vidéo", "feed video": "Vidéo publiée",
}
_MEDIA_SENTENCE_RE = re.compile(r"^You (?:uploaded|added|posted|shared) (?:an? )?(?P<kind>.+?) on (?P<date>.+?)(?: \(\w+\))?$")
_SENTENCE_DATE_RE = re.compile(r"^(?P<date>[A-Z][a-z]+ \d{1,2}, \d{4}) at (?P<time>\d{1,2}:\d{2} ?[AP]M)$")

_DATE_FORMATS = (
    "%d %b %Y",              # 08 Oct 2026 -- Connections "Connected On"
    "%b %Y",                 # Oct 2020 -- Positions, Certifications, Volunteering
    "%Y",                    # 2020 -- Education
    "%m/%d/%y, %I:%M %p",    # 10/8/26, 9:45 AM -- Invitations, Jobs, Recommendations, Registration
    "%m/%d/%Y, %I:%M %p",
    "%Y/%m/%d %H:%M:%S",     # 2020/10/08 10:12:34 UTC -- Endorsements (zone removed)
    "%Y-%m-%d %H:%M:%S",     # messages.csv, Shares.csv...
    "%Y-%m-%d %H:%M",        # 2023-05-01 10:12 UTC -- Learning
    "%Y-%m-%d",              # Verifications
    "%a %b %d %H:%M:%S %Y",  # Thu Oct 08 10:12:34 UTC 2020 -- Company Follows (zone removed)
    "%b %d, %Y %I:%M %p",    # Oct 8, 2026 10:00 AM -- Events
    "%B %d, %Y %I:%M %p",    # July 12, 2020 9:45 PM -- Rich_Media sentences
    "%b %d, %Y",
    "%B %d, %Y",
    "%m/%d/%Y",
)
# A time zone name, written before the year ("10:12:34 UTC 2020") or at the
# end ("10:12 UTC", "(GMT)"). Every value seen is UTC/GMT.
_ZONE_RE = re.compile(r"\s\(?(?:UTC|GMT)\)?(?=\s|$)")
_NOT_AVAILABLE = {"n/a", "na", "null", "none", "-"}


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value or not value.strip():
        return None
    text = _ZONE_RE.sub("", value.strip()).strip()
    sentence = _SENTENCE_DATE_RE.match(text)
    if sentence:
        text = f"{sentence.group('date')} {sentence.group('time')}"
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _read_csv(path: Path) -> List[Row]:
    """Rows of one LinkedIn CSV as {column: value}, past the "Notes:"
    preamble some files open with. A repeated column name keeps its first
    non-empty value; an unnamed column and an entirely empty row are
    dropped (Profile Summary.csv ships one with an empty summary)."""
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        rows = list(csv.reader(f, escapechar=csv_escapechar(path)))
    start = 0
    if rows and len(rows[0]) == 1 and rows[0][0].strip().endswith(":"):
        # "Notes:", the note itself, then a blank line before the header.
        start = next((i + 1 for i, r in enumerate(rows) if i and not any(c.strip() for c in r)), len(rows))
    if start >= len(rows):
        return []
    header = [h.strip() for h in rows[start]]
    result = []
    for raw in rows[start + 1:]:
        row: Row = {}
        for name, value in zip(header, raw):
            if not name:
                continue
            value = value.strip()
            if not row.get(name):
                row[name] = value
        if any(row.values()):
            result.append(row)
    return result


def _get(row: Row, *names: str) -> Optional[str]:
    """First non-empty value among the given columns, matched without case
    (the Complete export's messages.csv shouts its headers). "N/A" -- what
    Learning.csv writes for a course never completed -- counts as empty."""
    lowered = None
    for name in names:
        value = row.get(name)
        if value is None:
            if lowered is None:
                lowered = {k.lower(): v for k, v in row.items()}
            value = lowered.get(name.lower())
        if value and value.strip() and value.strip().lower() not in _NOT_AVAILABLE:
            return value.strip()
    return None


def _full_name(*parts: Optional[str]) -> Optional[str]:
    return " ".join(p for p in parts if p) or None


def _profile_url(value: Optional[str]) -> Optional[str]:
    """Endorsement files write profile links without a scheme
    ("www.linkedin.com/in/..."); a full URL makes them clickable."""
    if not value:
        return None
    return value if value.startswith(("http://", "https://")) else f"https://{value}"


def _plain_text(value: Optional[str]) -> Optional[str]:
    """Learning descriptions (and InMail bodies) carry HTML fragments
    ("<a>", "</br>"): kept as readable text."""
    if not value or "<" not in value:
        return value
    text = BeautifulSoup(value.replace("</br>", "\n"), "html.parser").get_text()
    return re.sub(r"\n{3,}", "\n\n", text).strip() or None


def _month(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime("%m/%Y") if dt else None


def _day(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime("%d/%m/%Y") if dt else None


def _fields(**pairs: Any) -> Optional[Dict[str, Any]]:
    """Field labels can't all be Python names ("Début", "E-mail"): pass them
    as a dict via **{...}. Empty values are left out."""
    kept = {k: v for k, v in pairs.items() if v not in (None, "")}
    return kept or None


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "file"


def _file_key(path: Path) -> str:
    """'Shares_387121575.csv' -> 'shares.csv': the name a file is known by,
    whichever export (and member) it comes from."""
    return MEMBER_ID_SUFFIX_RE.sub("", path.name.lower())


def csv_escapechar(path: Path) -> Optional[str]:
    """The escape character a LinkedIn CSV is written with. Comments.csv
    writes a quote inside a comment as \\" (read as the standard "", a quoted
    comment spills over the following rows); every other file doubles its
    quotes. Declared per file rather than sniffed: a literal \\" in a post
    of Shares.csv is just text."""
    return "\\" if _file_key(path) in _BACKSLASH_ESCAPED_FILES else None


def _follow_type(kind: str, state: Optional[str]) -> str:
    """'member_follow', or 'member_unfollow' for one no longer followed."""
    return f"{kind}_unfollow" if (state or "").upper() == _UNFOLLOWED_STATE else f"{kind}_follow"


def _unknown_follow_state(state: Optional[str]) -> Optional[str]:
    """A follow state the item type doesn't already tell, kept visible."""
    upper = (state or "").upper()
    return None if upper in _FOLLOWING_STATES or upper == _UNFOLLOWED_STATE else state


def _score(value: Optional[str]) -> Optional[str]:
    """'2.9512345678901234' -> '2,95'; a value that isn't a number as is."""
    try:
        return format(round(float(value), 2), "g").replace(".", ",")
    except (TypeError, ValueError):
        return value


@register
class LinkedInImporter(BaseImporter):
    key = "linkedin"
    label = "LinkedIn"
    # "post", "message" and "search" are left to their shared labels: other
    # platforms use those item types too.
    type_labels = {
        "connection": "Relation",
        "invitation_received": "Invitation reçue",
        "invitation_sent": "Invitation envoyée",
        "company_follow": "Entreprise suivie",
        "member_follow": "Membre suivi",
        "member_unfollow": "Membre plus suivi",
        "hashtag_follow": "Hashtag suivi",
        "hashtag_unfollow": "Hashtag plus suivi",
        "connection_note": "Note sur une relation",
        "position": "Poste",
        "education": "Études",
        "volunteering": "Bénévolat",
        "certification": "Certification",
        "skill": "Compétence",
        "language": "Langue",
        "endorsement_received": "Validation reçue",
        "endorsement_given": "Validation donnée",
        "recommendation_received": "Recommandation reçue",
        "recommendation_given": "Recommandation donnée",
        "profile": "Profil",
        "profile_summary": "Résumé du profil",
        "email_address": "Adresse e-mail",
        "phone_number": "Numéro de téléphone",
        "registration": "Inscription",
        "verification": "Vérification d'identité",
        "cause": "Cause soutenue",
        "job_application": "Candidature",
        "saved_job": "Offre enregistrée",
        "job_seeker_preferences": "Préférences de recherche d'emploi",
        "job_alert": "Alerte emploi",
        "learning_course": "Cours LinkedIn Learning",
        "event": "Événement",
        "media_upload": "Média importé",
        "ad_targeting": "Ciblage publicitaire",
        "ad_click": "Publicité cliquée",
        "comment": "Commentaire",
        "reaction": "Réaction",
        "poll_vote": "Vote à un sondage",
        "repost": "Republication",
        "saved_item": "Publication enregistrée",
        "login": "Connexion",
        "security_challenge": "Vérification de sécurité",
        "inference": "Déduction sur vous",
        "credibility_score": "Score de crédibilité",
    }

    def detect(self, root: Path) -> bool:
        for path in root.rglob("*.csv"):
            marker = _SIGNATURES.get(_file_key(path))
            if not marker:
                continue
            try:
                with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                    head = f.read(4096).lower()
            except OSError:
                continue
            if marker in head:
                return True
        return False

    def parse(self, root: Path) -> Iterator[NormalizedItem]:
        files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".csv")
        # The account owner's name: the side of a conversation that isn't the
        # other participant(s). Passed along rather than kept on the
        # importer, which every import shares.
        owner = self._owner_name(files)
        handlers: Dict[str, Callable[[List[Row]], Iterator[NormalizedItem]]] = {
            "connections.csv": self._each(self._connection),
            "invitations.csv": self._each(self._invitation),
            "company follows.csv": self._each(self._company_follow),
            "member_follows.csv": self._each(self._member_follow),
            "notes.csv": self._each(self._connection_note),
            "positions.csv": self._each(self._position),
            "education.csv": self._each(self._education),
            "volunteering.csv": self._each(self._volunteering),
            "certifications.csv": self._each(self._certification),
            "skills.csv": self._each(self._skill),
            "languages.csv": self._each(self._language),
            "endorsement_received_info.csv": self._each(lambda r: self._endorsement(r, received=True)),
            "endorsement_given_info.csv": self._each(lambda r: self._endorsement(r, received=False)),
            "recommendations_received.csv": self._each(lambda r: self._recommendation(r, received=True)),
            "recommendations_given.csv": self._each(lambda r: self._recommendation(r, received=False)),
            "profile.csv": self._each(self._profile),
            "profile summary.csv": self._each(self._profile_summary),
            "email addresses.csv": self._each(self._email_address),
            "phonenumbers.csv": self._each(self._phone_number),
            "registration.csv": self._each(self._registration),
            "verifications.csv": self._each(self._verification),
            "causes you care about.csv": self._each(self._cause),
            "job applications.csv": self._each(self._job_application),
            "saved jobs.csv": self._each(self._saved_job),
            "job seeker preferences.csv": self._each(self._job_seeker_preferences),
            "savedjobalerts.csv": self._each(self._job_alert),
            "learning.csv": self._each(self._learning_course),
            "events.csv": self._each(self._event),
            "rich_media.csv": self._each(self._media_upload),
            "ad_targeting.csv": self._each(self._ad_targeting),
            # Complete export only.
            "ads clicked.csv": self._each(self._ad_click),
            "shares.csv": self._each(self._share),
            "comments.csv": self._each(self._comment),
            "reactions.csv": self._each(self._reaction),
            "votes.csv": self._each(self._poll_vote),
            "instantreposts.csv": self._each(self._repost),
            "saved_items.csv": self._each(self._saved_item),
            "hashtag_follows.csv": self._each(self._hashtag_follow),
            "searchqueries.csv": self._each(self._search),
            "logins.csv": self._each(self._login),
            "security challenges.csv": self._each(self._security_challenge),
            "inferences_about_you.csv": self._each(self._inference),
            "inferred credibility scores.csv": self._credibility_scores,
            # Not in the sample Complete export: from LinkedIn's documentation.
            "messages.csv": lambda rows: self._messages(rows, owner),
        }
        for path in files:
            try:
                rows = _read_csv(path)
            except Exception:
                continue
            handler = handlers.get(_file_key(path))
            if handler is None:
                yield from self._parse_generic(root, path, rows)
            else:
                yield from handler(rows)

    @staticmethod
    def _each(normalize: Callable[[Row], Optional[NormalizedItem]]) -> Callable[[List[Row]], Iterator[NormalizedItem]]:
        """One item per row; a row that doesn't parse is skipped, never the
        whole file."""
        def run(rows: List[Row]) -> Iterator[NormalizedItem]:
            for row in rows:
                try:
                    item = normalize(row)
                except Exception:
                    continue
                if item is not None:
                    yield item
        return run

    @staticmethod
    def _owner_name(files: Iterable[Path]) -> Optional[str]:
        for path in files:
            if _file_key(path) == "profile.csv":
                try:
                    rows = _read_csv(path)
                except Exception:
                    return None
                if rows:
                    return _full_name(_get(rows[0], "First Name"), _get(rows[0], "Last Name"))
        return None

    # -- Network ---------------------------------------------------------------
    # Connections.csv -- confirmed header (after the "Notes:" preamble):
    # First Name, Last Name, URL, Email Address, Company, Position, Connected
    # On. A member who hides their profile from exports comes with only the
    # date: still a connection, counted, nameless.

    def _connection(self, row: Row) -> Optional[NormalizedItem]:
        when = _parse_date(_get(row, "Connected On"))
        name = _full_name(_get(row, "First Name"), _get(row, "Last Name"))
        if not name and not when:
            return None
        return NormalizedItem(
            item_type="connection", category=NETWORK, timestamp=when, title=name,
            fields=_fields(**{
                "Profil": _get(row, "URL"), "Entreprise": _get(row, "Company"),
                "Poste": _get(row, "Position"), "E-mail": _get(row, "Email Address"),
            }),
            raw=row,
        )

    # Invitations.csv -- confirmed header: From, To, Sent At, Message,
    # Direction (INCOMING / OUTGOING), inviterProfileUrl, inviteeProfileUrl.

    def _invitation(self, row: Row) -> Optional[NormalizedItem]:
        direction = (_get(row, "Direction") or "").upper()
        incoming = direction == "INCOMING"
        sender, recipient = _get(row, "From"), _get(row, "To")
        other = sender if incoming else recipient
        other_url = _get(row, "inviterProfileUrl") if incoming else _get(row, "inviteeProfileUrl")
        if not other and not sender:
            return None
        item_type = {"INCOMING": "invitation_received", "OUTGOING": "invitation_sent"}.get(direction, "invitation")
        return NormalizedItem(
            item_type=item_type, category=NETWORK, timestamp=_parse_date(_get(row, "Sent At")),
            title=other, content=_get(row, "Message"),
            fields=_fields(**{"De": sender, "À": recipient, "Profil": other_url}),
            raw=row,
        )

    # Company Follows.csv -- confirmed header: Organization, Followed On.

    def _company_follow(self, row: Row) -> Optional[NormalizedItem]:
        organization = _get(row, "Organization")
        if not organization:
            return None
        return NormalizedItem(
            item_type="company_follow", category=NETWORK, timestamp=_parse_date(_get(row, "Followed On")),
            title=organization, raw=row,
        )

    # Member_Follows.csv (Complete export) -- confirmed header: Date ("2023-
    # 05-01 10:12:34"), Status, FullName. "Active" for a member followed,
    # "Unfollow" for one followed then unfollowed -- half of the real file:
    # its own type, so the Réseau tab's "Abonnements" counts only the
    # members still followed. Any other status is kept as a field.

    def _member_follow(self, row: Row) -> Optional[NormalizedItem]:
        name = _get(row, "FullName", "Full Name", "Name")
        if not name:
            return None
        status = _get(row, "Status")
        return NormalizedItem(
            item_type=_follow_type("member", status), category=NETWORK,
            timestamp=_parse_date(_get(row, "Date")), title=name,
            fields=_fields(**{"Statut": _unknown_follow_state(status)}), raw=row,
        )

    # Hashtag_Follows.csv (Complete export) -- confirmed header: HashTag,
    # CreatedTime, LastModifiedTime, State ("Follow"; "Unfollow" expected
    # too, read like a member's).

    def _hashtag_follow(self, row: Row) -> Optional[NormalizedItem]:
        tag = _get(row, "HashTag")
        if not tag:
            return None
        state = _get(row, "State")
        return NormalizedItem(
            item_type=_follow_type("hashtag", state), category=NETWORK,
            timestamp=_parse_date(_get(row, "CreatedTime")), title=tag if tag.startswith("#") else f"#{tag}",
            fields=_fields(**{
                "Statut": _unknown_follow_state(state),
                "Modifié le": _day(_parse_date(_get(row, "LastModifiedTime"))),
            }),
            raw=row,
        )

    # Notes.csv -- confirmed header (empty on the sample): Connection First
    # Name, Connection Last Name, Connection Profile URL, Note, Created On,
    # Edited On.

    def _connection_note(self, row: Row) -> Optional[NormalizedItem]:
        note = _get(row, "Note")
        if not note:
            return None
        edited = _parse_date(_get(row, "Edited On"))
        return NormalizedItem(
            item_type="connection_note", category=NETWORK, timestamp=_parse_date(_get(row, "Created On")),
            title=_full_name(_get(row, "Connection First Name"), _get(row, "Connection Last Name")),
            content=note,
            fields=_fields(**{"Profil": _get(row, "Connection Profile URL"), "Modifiée le": _day(edited)}),
            raw=row,
        )

    # -- Career ------------------------------------------------------------------
    # Positions.csv -- confirmed header: Company Name, Title, Description,
    # Location, Started On, Finished On ("Oct 2020"; empty while current).
    # "Début"/"Fin" are what the Parcours tab draws the career timeline from.

    def _position(self, row: Row) -> Optional[NormalizedItem]:
        title, company = _get(row, "Title"), _get(row, "Company Name")
        if not title and not company:
            return None
        start, end = _parse_date(_get(row, "Started On")), _parse_date(_get(row, "Finished On"))
        return NormalizedItem(
            item_type="position", category=CAREER, timestamp=start, title=title or company, context=company,
            fields=_fields(**{
                "Entreprise": company, "Lieu": _get(row, "Location"), "Début": _month(start),
                "Fin": _month(end) or ("En cours" if start else None), "Description": _get(row, "Description"),
            }),
            raw=row,
        )

    # Education.csv -- confirmed header: School Name, Start Date, End Date
    # (years), Notes, Degree Name, Activities.

    def _education(self, row: Row) -> Optional[NormalizedItem]:
        school = _get(row, "School Name")
        if not school:
            return None
        return NormalizedItem(
            item_type="education", category=CAREER, timestamp=_parse_date(_get(row, "Start Date")), title=school,
            context=_get(row, "Degree Name"),
            fields=_fields(**{
                "Diplôme": _get(row, "Degree Name"), "Début": _get(row, "Start Date"), "Fin": _get(row, "End Date"),
                "Notes": _get(row, "Notes"), "Activités": _get(row, "Activities"),
            }),
            raw=row,
        )

    # Volunteering.csv -- confirmed header: Company Name, Role, Cause,
    # Started On, Finished On, Description.

    def _volunteering(self, row: Row) -> Optional[NormalizedItem]:
        role, organization = _get(row, "Role"), _get(row, "Company Name")
        if not role and not organization:
            return None
        start, end = _parse_date(_get(row, "Started On")), _parse_date(_get(row, "Finished On"))
        return NormalizedItem(
            item_type="volunteering", category=CAREER, timestamp=start, title=role or organization,
            context=organization,
            fields=_fields(**{
                "Organisation": organization, "Cause": _get(row, "Cause"), "Début": _month(start),
                "Fin": _month(end) or ("En cours" if start else None), "Description": _get(row, "Description"),
            }),
            raw=row,
        )

    # Certifications.csv -- confirmed header: Name, Url, Authority, Started
    # On (issued), Finished On (expires), License Number.

    def _certification(self, row: Row) -> Optional[NormalizedItem]:
        name = _get(row, "Name")
        if not name:
            return None
        issued, expires = _parse_date(_get(row, "Started On")), _parse_date(_get(row, "Finished On"))
        return NormalizedItem(
            item_type="certification", category=CAREER, timestamp=issued, title=name,
            context=_get(row, "Authority"),
            fields=_fields(**{
                "Organisme": _get(row, "Authority"), "Délivrée": _month(issued), "Expire": _month(expires),
                "Numéro de licence": _get(row, "License Number"), "Lien": _get(row, "Url"),
            }),
            raw=row,
        )

    # -- Skills, endorsements, recommendations ----------------------------------
    # Skills.csv: Name. Languages.csv: Name, Proficiency. Both undated.

    def _skill(self, row: Row) -> Optional[NormalizedItem]:
        name = _get(row, "Name")
        return NormalizedItem(item_type="skill", category=SKILLS, title=name, raw=row) if name else None

    def _language(self, row: Row) -> Optional[NormalizedItem]:
        name = _get(row, "Name")
        if not name:
            return None
        return NormalizedItem(
            item_type="language", category=SKILLS, title=name,
            fields=_fields(**{"Niveau": _get(row, "Proficiency")}), raw=row,
        )

    # Endorsement_Received_Info.csv / Endorsement_Given_Info.csv --
    # confirmed headers: Endorsement Date, Skill Name, Endorser (or
    # Endorsee) First Name, Last Name, Public Url, Endorsement Status. The
    # skill is the title; the endorser the author of a received one, the
    # endorsee the context of a given one -- the Parcours tab filters on
    # each exactly.

    def _endorsement(self, row: Row, received: bool) -> Optional[NormalizedItem]:
        role = "Endorser" if received else "Endorsee"
        skill = _get(row, "Skill Name")
        person = _full_name(_get(row, f"{role} First Name"), _get(row, f"{role} Last Name"))
        if not skill and not person:
            return None
        status = _get(row, "Endorsement Status")
        return NormalizedItem(
            item_type="endorsement_received" if received else "endorsement_given", category=SKILLS,
            timestamp=_parse_date(_get(row, "Endorsement Date")), title=skill,
            author=person if received else None, context=None if received else person,
            fields=_fields(**{
                "Profil": _profile_url(_get(row, f"{role} Public Url")),
                "Statut": _STATUS_LABELS.get((status or "").upper(), status),
            }),
            raw=row,
        )

    # Recommendations_Received.csv / Recommendations_Given.csv -- confirmed
    # header: First Name, Last Name, Company, Job Title, Text, Creation Date,
    # Status.

    def _recommendation(self, row: Row, received: bool) -> Optional[NormalizedItem]:
        person = _full_name(_get(row, "First Name"), _get(row, "Last Name"))
        text = _get(row, "Text")
        if not person and not text:
            return None
        status = _get(row, "Status")
        return NormalizedItem(
            item_type="recommendation_received" if received else "recommendation_given", category=RECOMMENDATIONS,
            timestamp=_parse_date(_get(row, "Creation Date")), title=person, content=text,
            author=person if received else None, context=_get(row, "Company"),
            fields=_fields(**{
                "Entreprise": _get(row, "Company"), "Poste": _get(row, "Job Title"),
                "Statut": _STATUS_LABELS.get((status or "").upper(), status),
            }),
            raw=row,
        )

    # -- Profile -------------------------------------------------------------------
    # Profile.csv -- confirmed header: First Name, Last Name, Maiden Name,
    # Address, Birth Date (day and month only), Headline, Summary, Industry,
    # Zip Code, Geo Location, Twitter Handles, Websites, Instant Messengers.
    # Undated: each export's version of the profile is its own item, so a
    # headline changed between two exports keeps both.

    def _profile(self, row: Row) -> Optional[NormalizedItem]:
        name = _full_name(_get(row, "First Name"), _get(row, "Last Name"))
        if not name:
            return None
        return NormalizedItem(
            item_type="profile", category=PROFILE, title=name, content=_get(row, "Headline"),
            fields=_fields(**{
                "Résumé": _get(row, "Summary"), "Secteur": _get(row, "Industry"),
                "Localisation": _get(row, "Geo Location"), "Code postal": _get(row, "Zip Code"),
                "Adresse": _get(row, "Address"), "Anniversaire": _get(row, "Birth Date"),
                "Nom de naissance": _get(row, "Maiden Name"), "Sites web": _get(row, "Websites"),
                "Twitter": _get(row, "Twitter Handles"), "Messageries": _get(row, "Instant Messengers"),
            }),
            raw=row,
        )

    def _profile_summary(self, row: Row) -> Optional[NormalizedItem]:
        summary = _get(row, "Profile Summary")
        return NormalizedItem(item_type="profile_summary", category=PROFILE, content=summary, raw=row) if summary else None

    # Email Addresses.csv -- confirmed header: Email Address, Confirmed,
    # Primary, Updated On. PhoneNumbers.csv: Extension, Number, Type.

    def _email_address(self, row: Row) -> Optional[NormalizedItem]:
        email = _get(row, "Email Address")
        if not email:
            return None
        return NormalizedItem(
            item_type="email_address", category=PROFILE, timestamp=_parse_date(_get(row, "Updated On")), title=email,
            fields=_fields(**{"Confirmée": _get(row, "Confirmed"), "Principale": _get(row, "Primary")}),
            raw=row,
        )

    def _phone_number(self, row: Row) -> Optional[NormalizedItem]:
        number = _get(row, "Number")
        if not number:
            return None
        return NormalizedItem(
            item_type="phone_number", category=PROFILE, title=number,
            fields=_fields(**{"Type": _get(row, "Type"), "Extension": _get(row, "Extension")}), raw=row,
        )

    # Registration.csv -- confirmed header: Registered At, Registration Ip,
    # Subscription Types. The date the account was created.

    def _registration(self, row: Row) -> Optional[NormalizedItem]:
        when = _parse_date(_get(row, "Registered At"))
        if not when:
            return None
        return NormalizedItem(
            item_type="registration", category=PROFILE, timestamp=when, title="Inscription à LinkedIn",
            fields=_fields(**{"IP d'inscription": _get(row, "Registration Ip"), "Abonnements": _get(row, "Subscription Types")}),
            raw=row,
        )

    # Verifications/Verifications.csv -- confirmed header: First name, Middle
    # name, Last name, Verification type, Organization name, Email address,
    # Country, State, City, Year of birth, Issuing authority, Document type,
    # Verification service provider, Verified date, Expiry date.

    def _verification(self, row: Row) -> Optional[NormalizedItem]:
        kind = _get(row, "Verification type")
        verified = _parse_date(_get(row, "Verified date"))
        if not kind and not verified:
            return None
        return NormalizedItem(
            item_type="verification", category=PROFILE, timestamp=verified, title=kind or "Vérification",
            fields=_fields(**{
                "Prestataire": _get(row, "Verification service provider"), "Autorité": _get(row, "Issuing authority"),
                "Document": _get(row, "Document type"), "Organisation": _get(row, "Organization name"),
                "Expire": _get(row, "Expiry date"),
            }),
            raw=row,
        )

    def _cause(self, row: Row) -> Optional[NormalizedItem]:
        cause = _get(row, "Supported Cause")
        return NormalizedItem(item_type="cause", category=PROFILE, title=cause, raw=row) if cause else None

    # -- Jobs ---------------------------------------------------------------------
    # Jobs/Job Applications.csv -- confirmed header: Application Date,
    # Contact Email, Contact Phone Number, Company Name, Job Title, Job Url,
    # Resume Name, Question And Answers. Jobs/Saved Jobs.csv: Saved Date,
    # Job Url, Job Title, Company Name.

    def _job_application(self, row: Row) -> Optional[NormalizedItem]:
        title, company = _get(row, "Job Title"), _get(row, "Company Name")
        if not title and not company:
            return None
        return NormalizedItem(
            item_type="job_application", category=JOBS, timestamp=_parse_date(_get(row, "Application Date")),
            title=title or company, context=company,
            fields=_fields(**{
                "Entreprise": company, "Lien": _get(row, "Job Url"), "CV envoyé": _get(row, "Resume Name"),
                "E-mail de contact": _get(row, "Contact Email"), "Téléphone de contact": _get(row, "Contact Phone Number"),
                "Questions et réponses": _get(row, "Question And Answers"),
            }),
            raw=row,
        )

    def _saved_job(self, row: Row) -> Optional[NormalizedItem]:
        title, company = _get(row, "Job Title"), _get(row, "Company Name")
        if not title and not company:
            return None
        return NormalizedItem(
            item_type="saved_job", category=JOBS, timestamp=_parse_date(_get(row, "Saved Date")),
            title=title or company, context=company,
            fields=_fields(**{"Entreprise": company, "Lien": _get(row, "Job Url")}), raw=row,
        )

    # Jobs/Job Seeker Preferences.csv (one row, 20 columns, mostly empty)
    # and SavedJobAlerts.csv (ALERT_PARAMETERS, QUERY_CONTEXT,
    # SAVED_SEARCH_ID -- internal parameter blobs): every filled column kept.

    def _job_seeker_preferences(self, row: Row) -> Optional[NormalizedItem]:
        fields = _fields(**row)
        if not fields:
            return None
        return NormalizedItem(item_type="job_seeker_preferences", category=JOBS, title="Préférences de recherche d'emploi",
                              fields=fields, raw=row)

    def _job_alert(self, row: Row) -> Optional[NormalizedItem]:
        fields = _fields(**row)
        if not fields:
            return None
        return NormalizedItem(item_type="job_alert", category=JOBS, title="Alerte emploi", fields=fields, raw=row)

    # -- Learning, events, media, ads --------------------------------------------
    # Learning.csv -- confirmed header: Content Title, Content Description
    # (HTML), Content Type, Content Last Watched Date (if viewed), Content
    # Completed At (if completed), Content Saved, Notes taken on videos (if
    # taken), plus an unnamed trailing column. Dated by the last viewing:
    # watching a course again moves it, so dedup leaves that date out of the
    # fingerprint (dedup.VOLATILE_TIMESTAMP_TYPES).

    def _learning_course(self, row: Row) -> Optional[NormalizedItem]:
        title = _get(row, "Content Title")
        if not title:
            return None
        completed = _parse_date(_get(row, "Content Completed At (if completed)"))
        saved = (_get(row, "Content Saved") or "").lower()
        return NormalizedItem(
            item_type="learning_course", category=LEARNING,
            timestamp=_parse_date(_get(row, "Content Last Watched Date (if viewed)")), title=title,
            content=_plain_text(_get(row, "Content Description")),
            fields=_fields(**{
                "Type": _get(row, "Content Type"), "Terminé le": _day(completed),
                "Enregistré": {"true": "Oui", "false": "Non"}.get(saved, saved or None),
                "Notes": _get(row, "Notes taken on videos (if taken)"),
            }),
            raw=row,
        )

    # Events.csv -- confirmed header: Event Name, Event Time ("Oct 8, 2026
    # 10:00 AM - Oct 8, 2026 11:00 AM"), Status (APPROVED, RELINQUISHED),
    # External Url.

    def _event(self, row: Row) -> Optional[NormalizedItem]:
        name = _get(row, "Event Name")
        if not name:
            return None
        start_text, _, end_text = (_get(row, "Event Time") or "").partition(" - ")
        end = _parse_date(end_text)
        return NormalizedItem(
            item_type="event", category=EVENTS, timestamp=_parse_date(start_text), title=name,
            fields=_fields(**{
                "Fin": end.strftime("%d/%m/%Y %H:%M") if end else None, "Statut": _get(row, "Status"),
                "Lien": _get(row, "External Url"),
            }),
            raw=row,
        )

    # Rich_Media.csv -- confirmed header: Date/Time (a sentence: "You
    # uploaded a feed photo on July 12, 2020 at 9:45 PM (GMT)"), Media
    # Description (" -" when there is none), Media Link.

    def _media_upload(self, row: Row) -> Optional[NormalizedItem]:
        sentence = _get(row, "Date/Time") or ""
        match = _MEDIA_SENTENCE_RE.match(sentence)
        kind = match.group("kind") if match else None
        when = _parse_date(match.group("date")) if match else _parse_date(sentence)
        description = _get(row, "Media Description")
        if description and description.strip(" -") == "":
            description = None
        link = _get(row, "Media Link")
        if not when and not description and not link:
            return None
        return NormalizedItem(
            item_type="media_upload", category=MEDIA, timestamp=when,
            title=_MEDIA_KINDS.get((kind or "").lower(), kind.capitalize() if kind else "Média"),
            content=description, fields=_fields(**{"Lien": link}), raw=row,
        )

    # Ad_Targeting.csv -- confirmed: one row, ~35 columns of "; "-separated
    # segments LinkedIn targets ads with (some column names repeated).

    def _ad_targeting(self, row: Row) -> Optional[NormalizedItem]:
        fields = _fields(**row)
        if not fields:
            return None
        return NormalizedItem(item_type="ad_targeting", category=ADS, title="Ciblage publicitaire", fields=fields, raw=row)

    # Ads Clicked.csv (Complete export) -- confirmed header: Ad clicked Date
    # ("2026/10/08 10:12:34 UTC"), Ad Title/Id (a bare numeric id). Clicks
    # come in bursts sharing one second: each row is kept.

    def _ad_click(self, row: Row) -> Optional[NormalizedItem]:
        when, ad = _parse_date(_get(row, "Ad clicked Date", "Date")), _get(row, "Ad Title/Id")
        if not when and not ad:
            return None
        return NormalizedItem(
            item_type="ad_click", category=ADS, timestamp=when, title="Publicité cliquée",
            fields=_fields(**{"Annonce": ad}), raw=row,
        )

    # -- Inferences (Complete export) ---------------------------------------------
    # Inferences_about_you.csv -- confirmed header: Category, Type of
    # inference, Description, Inference (the inferred value). Undated. The
    # value is a field, left out of the dedup fingerprint: an inference
    # LinkedIn revises is the same record, not a second one beside it.

    def _inference(self, row: Row) -> Optional[NormalizedItem]:
        kind, value = _get(row, "Type of inference"), _get(row, "Inference")
        if not kind and not value:
            return None
        return NormalizedItem(
            item_type="inference", category=INFERENCES, title=kind or "Déduction",
            fields=_fields(**{
                "Valeur": value, "Catégorie": _get(row, "Category"), "Description": _get(row, "Description"),
            }),
            raw=row,
        )

    # Inferred Credibility Scores.csv -- confirmed header: ScoreType
    # (Knowledge, Notability), Topic, Category ("Topic score", "Topic
    # normalized score", "Role and scope"...), Range ("1-4 (integer)"; empty
    # for a raw "Topic score", which runs below 0), then
    # one column per day LinkedIn computed scores on ("2026-10-08", most
    # recent first), each row filled on a few of them only. One item per
    # row with its latest score. Undated, like the other inferences: a
    # weekly computation by LinkedIn isn't the member's activity. The score
    # and its history move every week, so dedup leaves them out of the
    # fingerprint (dedup.VOLATILE_FIELDS).

    def _credibility_scores(self, rows: List[Row]) -> Iterator[NormalizedItem]:
        for row in rows:
            try:
                history = sorted(
                    ((datetime.strptime(column, "%Y-%m-%d"), value) for column, value in row.items()
                     if _ISO_DAY_RE.match(column) and value),
                    reverse=True,
                )
                topic, category = _get(row, "Topic"), _get(row, "Category")
                if not history or not (topic or category):
                    continue
                kind = _get(row, "ScoreType")
                yield NormalizedItem(
                    item_type="credibility_score", category=INFERENCES,
                    title=topic or category, context=category if topic else None,
                    fields=_fields(**{
                        "Type": _SCORE_TYPES.get((kind or "").upper(), kind), "Échelle": _get(row, "Range"),
                        "Score": _score(history[0][1]), "Calculé le": _day(history[0][0]),
                        "Historique": " · ".join(f"{_day(day)} : {_score(value)}" for day, value in history),
                    }),
                    raw=row,
                )
            except Exception:
                continue

    # -- Complete export activity -------------------------------------------------
    # messages.csv (from LinkedIn's documentation, absent from the sample
    # Complete export): CONVERSATION ID, CONVERSATION TITLE, FROM, SENDER PROFILE
    # URL, TO, RECIPIENT PROFILE URLS, DATE ("2023-04-12 08:31:22 UTC"),
    # SUBJECT, CONTENT, FOLDER (+ ATTACHMENTS on recent exports). A
    # conversation is named by its title, or else by its participants other
    # than the account owner, gathered over the whole conversation so every
    # message of it lands in the same thread.

    def _messages(self, rows: List[Row], owner: Optional[str]) -> Iterator[NormalizedItem]:
        participants: Dict[str, set] = {}
        titles: Dict[str, str] = {}
        for row in rows:
            conversation = _get(row, "CONVERSATION ID") or ""
            people = participants.setdefault(conversation, set())
            sender = _get(row, "FROM")
            if sender:
                people.add(sender)
            people.update(name.strip() for name in (_get(row, "TO") or "").split(",") if name.strip())
            title = _get(row, "CONVERSATION TITLE")
            if title:
                titles.setdefault(conversation, title)

        def label(conversation: str) -> Optional[str]:
            if conversation in titles:
                return titles[conversation]
            others = sorted(participants.get(conversation, set()) - {owner})
            return ", ".join(others) or conversation or None

        for row in rows:
            try:
                content = _plain_text(_get(row, "CONTENT"))
                sender = _get(row, "FROM")
                if not content and not sender:
                    continue
                yield NormalizedItem(
                    item_type="message", category=MESSAGES, timestamp=_parse_date(_get(row, "DATE")),
                    content=content, author=sender, context=label(_get(row, "CONVERSATION ID") or ""),
                    fields=_fields(**{
                        "Objet": _get(row, "SUBJECT"), "Dossier": _get(row, "FOLDER"),
                        "Profil de l'expéditeur": _get(row, "SENDER PROFILE URL"),
                        "Pièces jointes": _get(row, "ATTACHMENTS"),
                    }),
                    raw=row,
                )
            except Exception:
                continue

    # Confirmed headers, dates as "2023-05-01 10:12:34": Shares.csv: Date,
    # ShareLink, ShareCommentary (empty on some), SharedUrl,
    # MediaUrl (a remote link, rarely filled -- nothing to show in the
    # Publications gallery), Visibility. Comments.csv: Date, Link, Message.
    # Reactions.csv: Date, Type, Link. Votes.csv: Date, Link, OptionText.
    # InstantReposts.csv: Date, Link. Saved_Items.csv: savedItem (the saved
    # post's link), CreatedTime.

    def _share(self, row: Row) -> Optional[NormalizedItem]:
        text, link = _get(row, "ShareCommentary"), _get(row, "ShareLink")
        if not text and not link:
            return None
        return NormalizedItem(
            item_type="post", category=POSTS, timestamp=_parse_date(_get(row, "Date")), content=text,
            fields=_fields(**{
                "Lien": link, "Lien partagé": _get(row, "SharedUrl"), "Média": _get(row, "MediaUrl"),
                "Visibilité": _get(row, "Visibility"),
            }),
            raw=row,
        )

    def _comment(self, row: Row) -> Optional[NormalizedItem]:
        text = _get(row, "Message")
        if not text:
            return None
        return NormalizedItem(
            item_type="comment", category=COMMENTS, timestamp=_parse_date(_get(row, "Date")), content=text,
            fields=_fields(**{"Lien": _get(row, "Link")}), raw=row,
        )

    def _reaction(self, row: Row) -> Optional[NormalizedItem]:
        kind, link = _get(row, "Type"), _get(row, "Link")
        if not kind and not link:
            return None
        return NormalizedItem(
            item_type="reaction", category=REACTIONS, timestamp=_parse_date(_get(row, "Date")),
            content=_REACTION_LABELS.get((kind or "").upper(), kind),
            fields=_fields(**{"Lien": link}), raw=row,
        )

    def _poll_vote(self, row: Row) -> Optional[NormalizedItem]:
        option, link = _get(row, "OptionText"), _get(row, "Link")
        if not option and not link:
            return None
        return NormalizedItem(
            item_type="poll_vote", category=REACTIONS, timestamp=_parse_date(_get(row, "Date")), content=option,
            fields=_fields(**{"Lien": link}), raw=row,
        )

    def _repost(self, row: Row) -> Optional[NormalizedItem]:
        link = _get(row, "Link")
        if not link:
            return None
        return NormalizedItem(
            item_type="repost", category=POSTS, timestamp=_parse_date(_get(row, "Date")),
            content="Republication", fields=_fields(**{"Lien": link}), raw=row,
        )

    def _saved_item(self, row: Row) -> Optional[NormalizedItem]:
        link = _get(row, "savedItem", "Link")
        if not link:
            return None
        return NormalizedItem(
            item_type="saved_item", category=POSTS, timestamp=_parse_date(_get(row, "CreatedTime", "Date")),
            title="Publication enregistrée", fields=_fields(**{"Lien": link}), raw=row,
        )

    # Confirmed headers: SearchQueries.csv: Time ("2023/05/01 10:12:34 UTC"),
    # Search Query. Logins.csv: Login Date ("Mon May 01 10:12:34 UTC 2023"),
    # IP Address, User Agent, Login Type. Security Challenges.csv: Challenge
    # Date (same shape), IP Address, User Agent, Country, Challenge Type.

    def _search(self, row: Row) -> Optional[NormalizedItem]:
        query = _get(row, "Search Query")
        if not query:
            return None
        return NormalizedItem(
            item_type="search", category=SEARCHES, timestamp=_parse_date(_get(row, "Time", "Date")), content=query, raw=row,
        )

    def _login(self, row: Row) -> Optional[NormalizedItem]:
        when = _parse_date(_get(row, "Login Date", "Date"))
        if not when:
            return None
        return NormalizedItem(
            item_type="login", category=SECURITY, timestamp=when, title="Connexion",
            fields=_fields(**{
                "Adresse IP": _get(row, "IP Address"), "Navigateur": _get(row, "User Agent"),
                "Type": _get(row, "Login Type"),
            }),
            raw=row,
        )

    def _security_challenge(self, row: Row) -> Optional[NormalizedItem]:
        when, kind = _parse_date(_get(row, "Challenge Date", "Date")), _get(row, "Challenge Type")
        if not when and not kind:
            return None
        return NormalizedItem(
            item_type="security_challenge", category=SECURITY, timestamp=when, title="Vérification de sécurité",
            content=_CHALLENGE_LABELS.get((kind or "").upper(), kind),
            fields=_fields(**{
                "Pays": _get(row, "Country"), "Adresse IP": _get(row, "IP Address"),
                "Navigateur": _get(row, "User Agent"),
            }),
            raw=row,
        )

    # -- Any other CSV -----------------------------------------------------------
    # One item per row: every filled column kept as a field, dated by the
    # first date-like column (by name) that parses. The item type is the
    # file's name, so a file LinkedIn adds later shows up on its own.

    def _parse_generic(self, root: Path, path: Path, rows: List[Row]) -> Iterator[NormalizedItem]:
        item_type = _slug(Path(_file_key(path)).stem)
        parent = path.parent.name.lower() if path.parent != root else ""
        category = _GENERIC_CATEGORIES.get(item_type) or _FOLDER_CATEGORIES.get(parent) or OTHER
        for row in rows:
            fields = _fields(**row)
            if not fields:
                continue
            timestamp = None
            for column, value in fields.items():
                name = column.lower()
                if "date" in name or "time" in name or name.endswith((" on", " at")):
                    timestamp = _parse_date(value)
                    if timestamp:
                        break
            preview = " · ".join(f"{k} : {v}" for k, v in list(fields.items())[:3])
            yield NormalizedItem(
                item_type=item_type, category=category, timestamp=timestamp,
                title=humanize(item_type), content=preview[:500], fields=fields, raw=row,
            )
