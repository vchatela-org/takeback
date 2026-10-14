"""Reddit's data export (reddit.com/settings/data-request).

Reddit mails a link to a FLAT zip of CSVs, one per kind of record, with English
snake_case headers whatever the account's language, plus a `checkfile.csv`
holding every other file's SHA-256 (skipped: it says nothing about the
account). Everything below was read off a real export (10/2026, 40 files):
posts, comments, private messages, announcements, votes, saved posts and
comments, a poll vote, subscribed communities, the account's statistics, its
IP log, its settings and what Reddit infers about it. 19 of those files held
their header and not one row: their columns are confirmed, their values are
not. Those are kept row by row, every filled column a field, under a type of
their own (`_HEADER_ONLY_FILES`) rather than mapped from a guess -- with one
exception, `chat_history.csv`, mapped to messages from its header because Chat
is where Reddit has been moving its direct messages. Any CSV Reddit adds later is
imported row by row too (`_parse_generic`).

Points that need a word:

* **Three files come twice.** `posts.csv`, `comments.csv` and
  `messages_archive.csv` each have a `*_headers.csv` twin holding the same
  records minus their text (no title, no body). On the real export the twins
  list exactly the same ids, so they add nothing; a record only a twin lists is
  still imported, textless, rather than lost.

* **Votes, saves and subscriptions carry no date** -- only the permalink of what
  was voted on or saved. They are imported undated, never dated by the export,
  and titled by the permalink's slug: `.../comments/1abc2de/whats_your_take/`
  reads "Whats your take". That is Reddit's own URL form of the post's title --
  lowercased, punctuation dropped, cut short -- and the only title the export
  gives; a comment vote is titled by the post the comment was under. A vote's
  direction is a field rather than its type: the real export holds votes
  withdrawn ("none"), and a vote changed between two exports is the same
  record, so dedup leaves "Vote" out of its fingerprint.

* **The export names its owner** (`statistics.csv`, "account name"). It titles
  the profile item, which `owner_item_type` points the messaging pages at
  instead of their guess -- the person who wrote in the most conversations
  (crud.get_me_candidate). That guess needs the owner to have written: the real
  inbox holds nothing but what Reddit, bots and newsletters sent, and it picked
  a welcome bot, showing its message as the owner's own.

* **A private message's conversation is its correspondent**: whoever is not
  the owner on the first message of its thread (`thread_id`, empty on that
  first message itself). Every message of a thread lands in one conversation,
  and two threads with the same person read as one chat, as Meta's do. The
  subject is the message's title.

* **Names are written as Reddit writes them**: `u/name` for a user, `r/name`
  for a community, so a list mixing both tells them apart (the files write
  `/u/name`, `/r/name`, or a bare name depending on the file). A post to one's
  own profile is filed under subreddit `u_name`, shown `u/name` as Reddit does.

* **`messages_archive.csv` writes its permalinks `https:/www.reddit.com/...`**,
  one slash short: repaired, or the link would not open.

* **Dates** are "2026-03-09 15:59:23 UTC" in most files and ISO with a "Z" in
  `announcements.csv`. All UTC, kept naive like every importer here.
"""
import csv
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from app.importers.base import BaseImporter, NormalizedItem
from app.importers.meta_common import humanize
from app.importers.registry import register

Row = Dict[str, str]

# File name -> a column of its header only Reddit's version has. Detection reads
# headers rather than trusting names: "posts.csv" or "comments.csv" are generic
# enough to turn up elsewhere -- LinkedIn's Complete export has a Comments.csv.
_SIGNATURES = {
    "posts.csv": "subreddit",
    "comments.csv": "subreddit",
    "post_votes.csv": "direction",
    "comment_votes.csv": "direction",
    "messages_archive.csv": "thread_id",
    "subscribed_subreddits.csv": "subreddit",
}

# Categories, in French like the other importers'.
POSTS = "Publications"
COMMENTS = "Commentaires"
MESSAGES = "Messages"
ANNOUNCEMENTS = "Annonces"
VOTES = "Votes"
SAVED = "Enregistrements"
HIDDEN = "Publications masquées"
COMMUNITIES = "Communautés"
# What stats.NETWORK_CATEGORIES reads the Réseau tab from.
NETWORK = "Réseau"
ACCOUNT = "Compte"
SECURITY = "Sécurité"
ADS = "Publicité"
INFERENCES = "Inférences"
PURCHASES = "Achats"
AWARDS = "Récompenses"
OTHER = "Autres données"

# Files read into the profile item rather than as items of their own.
_PROFILE_FILES = {
    "statistics.csv", "account_gender.csv", "birthdate.csv", "linked_phone_number.csv",
    "linked_identities.csv", "stripe.csv", "persona.csv",
}
_SKIPPED_FILES = {"checkfile.csv"}
# Full file -> its textless `*_headers.csv` twin (see the module docstring).
_TWINS = {
    "posts.csv": "post_headers.csv",
    "comments.csv": "comment_headers.csv",
    "messages_archive.csv": "messages_archive_headers.csv",
}

# Header-only on the real export (see the module docstring): file -> (item
# type, category, the column dating a row, the column titling it).
_HEADER_ONLY_FILES = {
    "drafts.csv": ("draft", POSTS, "created", "title"),
    "scheduled_posts.csv": ("scheduled_post", POSTS, "submission_time", "title"),
    "multireddits.csv": ("multireddit", COMMUNITIES, "date", "display_name"),
    "purchases.csv": ("reddit_purchase", PURCHASES, "date", "product"),
    "subscriptions.csv": ("reddit_subscription", PURCHASES, "start_date", "product_name"),
    "payouts.csv": ("payout", PURCHASES, "date", None),
    "gilded_content.csv": ("award_given", AWARDS, "date", "award"),
    "gold_received.csv": ("award_received", AWARDS, "date", None),
}

# A vote's "direction". "none" is a vote withdrawn: Reddit still lists it.
_VOTE_LABELS = {"up": "Positif", "down": "Négatif", "none": "Retiré"}

# sensitive_ads_preferences.csv: whether ads on a sensitive topic are allowed
# ("True") or shown less ("False", Reddit's "See fewer").
_SENSITIVE_AD_TOPICS = {
    "show_alcohol": "Alcool", "show_dating": "Rencontres", "show_gambling": "Jeux d'argent",
    "show_pregnancy_and_parenting": "Grossesse et parentalité", "show_weight_loss": "Perte de poids",
}
_SENSITIVE_AD_VALUES = {"true": "Autorisées", "false": "Limitées"}

# The inference files' columns, each one an inference of its own.
_INFERENCE_LABELS = {
    "ads - inferred user age": "Âge déduit pour la publicité",
    "ads - inferred user gender": "Genre déduit pour la publicité",
    "under_18": "Moins de 18 ans",
    "under_16": "Moins de 16 ans",
    "under_13": "Moins de 13 ans",
    "persona_estimated_age": "Âge estimé (Persona)",
}
_INFERENCE_FILES = ("inferred_age_ads.csv", "inferred_gender_ads.csv", "inferred_age.csv")

# The statistics.csv rows the profile names itself; any other is kept under its
# own name. "export time" is left out: it changes with every export, and would
# make each one's profile a record of its own.
_STATISTICS = {"account name", "registration date", "email address", "email verified", "is_deleted", "export time"}

# The registration row of ip_logs.csv, whose "date" column holds this phrase
# rather than a date: its IP goes to the profile.
_REGISTRATION_IP = "registration ip"

# https://www.reddit.com/r/<community>/comments/<post>[/<slug>][/<comment>]/ --
# a vote or a save names its post's slug; an own comment's permalink has
# "comment" where the slug would be.
_PERMALINK_RE = re.compile(
    r"^https?://(?:[a-z]+\.)?reddit\.com/r/(?P<community>[^/]+)/comments/(?P<post>[a-z0-9]+)"
    r"(?:/(?P<slug>[^/?#]+))?(?:/(?P<comment>[a-z0-9]+))?/?(?:[?#].*)?$",
    re.IGNORECASE,
)
# Reddit's type prefixes ("t1_" a comment, "t3_" a post, "t4_" a message), which
# some columns carry and others don't.
_THING_PREFIX_RE = re.compile(r"^t\d_", re.IGNORECASE)


def _read_csv(path: Path) -> List[Row]:
    """{column: value} rows; an entirely empty row is dropped."""
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.reader(f)
        header = [name.strip() for name in next(reader, [])]
        rows = []
        for cells in reader:
            row = {name: cell.strip() for name, cell in zip(header, cells) if name}
            if any(row.values()):
                rows.append(row)
    return rows


def _header(path: Path) -> List[str]:
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
            return [name.strip().lower() for name in next(csv.reader(f), [])]
    except (OSError, csv.Error):
        return []


def _get(row: Row, name: str) -> Optional[str]:
    value = row.get(name)
    return value.strip() or None if isinstance(value, str) else None


def _fields(**pairs: Any) -> Optional[Dict[str, Any]]:
    """Field labels can't all be Python names ("Adresse IP"): pass them as a
    dict via **{...}. Empty values are left out."""
    kept = {k: v for k, v in pairs.items() if v not in (None, "")}
    return kept or None


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    """'2026-03-09 15:59:23 UTC' or '2025-07-15T17:53:34Z', as naive UTC. An
    unparseable value is treated as absent, never as "now"."""
    if not value or not value.strip():
        return None
    text = value.strip()
    if text.upper().endswith(" UTC"):
        text = text[:-4]
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def _stamp(value: Optional[str]) -> Optional[str]:
    parsed = _parse_date(value)
    return parsed.strftime("%d/%m/%Y %H:%M") if parsed else None


def _yes_no(value: Optional[str]) -> Optional[str]:
    return {"true": "Oui", "false": "Non"}.get((value or "").strip().lower(), value or None)


def _user(value: Optional[str]) -> Optional[str]:
    """'/u/name', 'u/name' or 'name' -> 'u/name'; a message to a community's
    moderators ('/r/name') -> 'r/name'. '[deleted]' stays as it is."""
    text = (value or "").strip().lstrip("/")
    if not text:
        return None
    if text.lower().startswith(("u/", "r/")):
        return text[0].lower() + text[1:]
    return text if text.startswith("[") else f"u/{text}"


def _community(value: Optional[str]) -> Optional[str]:
    """'france' / 'r/france' -> 'r/france'; a profile's own 'u_name' -> 'u/name'."""
    text = (value or "").strip().lstrip("/")
    if not text:
        return None
    if text.lower().startswith(("r/", "u/")):
        return text[0].lower() + text[1:]
    if text.lower().startswith("u_"):
        return f"u/{text[2:]}"
    return f"r/{text}"


def _link(value: Optional[str]) -> Optional[str]:
    """A permalink, with the "https:/www" messages_archive.csv writes repaired."""
    text = (value or "").strip()
    if not text:
        return None
    return re.sub(r"^(https?):/(?!/)", r"\1://", text)


def _thing_id(value: Optional[str]) -> Optional[str]:
    """'t1_abc12' / 'abc12' -> 'abc12'."""
    text = (value or "").strip()
    return _THING_PREFIX_RE.sub("", text) or None


def _permalink(value: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """(community, post id, title read off the slug) of a Reddit permalink,
    each None when the link doesn't say -- as for a title with no Latin
    letter in it, which Reddit slugs "_"."""
    match = _PERMALINK_RE.match(_link(value) or "")
    if not match:
        return None, None, None
    slug = match.group("slug")
    title = None
    if slug and slug.lower() != "comment":
        words = slug.replace("_", " ").strip()
        title = words[:1].upper() + words[1:] if words else None
    return _community(match.group("community")), match.group("post"), title


def _awards(row: Row) -> Optional[int]:
    """How many awards a post or comment received: the three award columns
    summed (a comment has all three, a post only "gildings"). Never a field
    when it is none, as on every row of the real export."""
    total = 0
    for column in ("gildings", "gildings_silver", "gildings_supergold"):
        try:
            total += int(row.get(column) or 0)
        except ValueError:
            continue
    return total or None


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "file"


def _with_twin(tables: Dict[str, List[Row]], full: str, twin: str) -> List[Row]:
    """A file's rows, plus those of its textless `*_headers.csv` twin it lacks."""
    rows = list(tables.get(full, []))
    seen = {row.get("id") for row in rows}
    rows.extend(row for row in tables.get(twin, []) if row.get("id") and row.get("id") not in seen)
    return rows


@register
class RedditImporter(BaseImporter):
    key = "reddit"
    label = "Reddit"
    # The profile's title is "u/<account name>", as messages spell an author.
    owner_item_type = "profile"
    # "post", "message" and "following" are left to their shared labels:
    # other platforms use those item types too.
    type_labels = {
        "comment": "Commentaire",
        "announcement": "Annonce",
        "post_vote": "Vote sur une publication",
        "comment_vote": "Vote sur un commentaire",
        "poll_vote": "Vote à un sondage",
        "saved_post": "Publication enregistrée",
        "saved_comment": "Commentaire enregistré",
        "hidden_post": "Publication masquée",
        "subreddit_subscription": "Communauté rejointe",
        "moderated_subreddit": "Communauté modérée",
        "approved_submitter": "Contributeur approuvé",
        "profile": "Profil",
        "ip_log": "Adresse IP enregistrée",
        "account_settings": "Préférences du compte",
        "ad_preferences": "Préférences publicitaires",
        "inference": "Déduction sur vous",
        "draft": "Brouillon",
        "scheduled_post": "Publication programmée",
        "multireddit": "Multireddit",
        "reddit_purchase": "Achat",
        "reddit_subscription": "Abonnement",
        "payout": "Versement",
        "award_given": "Récompense donnée",
        "award_received": "Récompense reçue",
    }

    def detect(self, root: Path) -> bool:
        for path in root.rglob("*.csv"):
            marker = _SIGNATURES.get(path.name.lower())
            if marker and marker in _header(path):
                return True
        return False

    def parse(self, root: Path) -> Iterator[NormalizedItem]:
        tables: Dict[str, List[Row]] = {}
        for path in sorted(root.rglob("*.csv")):
            name = path.name.lower()
            if not path.is_file() or name in tables or name in _SKIPPED_FILES:
                continue
            try:
                tables[name] = _read_csv(path)
            except Exception:
                continue
        # The textless twins, merged into their full file before anything reads
        # it (the owner's guess included).
        for full, twin in _TWINS.items():
            if full in tables or twin in tables:
                tables[full] = _with_twin(tables, full, twin)
        owner = self._owner(tables)

        handlers: Dict[str, Callable[[List[Row]], Iterator[NormalizedItem]]] = {
            "posts.csv": self._each(self._post),
            "comments.csv": self._each(self._comment),
            "messages_archive.csv": lambda rows: self._messages(rows, owner),
            "chat_history.csv": self._each(self._chat_message),
            "announcements.csv": self._each(self._announcement),
            "post_votes.csv": self._each(lambda r: self._vote(r, "post_vote")),
            "comment_votes.csv": self._each(lambda r: self._vote(r, "comment_vote")),
            "poll_votes.csv": self._each(self._poll_vote),
            "saved_posts.csv": self._each(lambda r: self._saved(r, "saved_post", SAVED)),
            "saved_comments.csv": self._each(lambda r: self._saved(r, "saved_comment", SAVED)),
            "hidden_posts.csv": self._each(lambda r: self._saved(r, "hidden_post", HIDDEN)),
            "subscribed_subreddits.csv": self._each(lambda r: self._community_item(r, "subreddit_subscription")),
            "moderated_subreddits.csv": self._each(lambda r: self._community_item(r, "moderated_subreddit")),
            "approved_submitter_subreddits.csv": self._each(lambda r: self._community_item(r, "approved_submitter")),
            "friends.csv": self._each(self._friend),
            "ip_logs.csv": self._each(self._ip_log),
            "user_preferences.csv": self._settings,
            "sensitive_ads_preferences.csv": self._ad_preferences,
        }
        consumed = set(_TWINS.values()) | _PROFILE_FILES | set(_INFERENCE_FILES)

        profile = self._profile(tables)
        if profile is not None:
            yield profile
        yield from self._inferences(tables)

        for name, rows in tables.items():
            if name in consumed:
                continue
            try:
                if name in handlers:
                    yield from handlers[name](rows)
                elif name in _HEADER_ONLY_FILES:
                    yield from self._each(lambda r, spec=_HEADER_ONLY_FILES[name]: self._header_only(r, *spec))(rows)
                else:
                    yield from self._parse_generic(name, rows)
            except Exception:
                continue

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
    def _owner(tables: Dict[str, List[Row]]) -> Optional[str]:
        """'u/<account name>' from statistics.csv; without it, the one
        participant every private message has -- or nobody, if that is not a
        single person (one message, or every message between the same two)."""
        for row in tables.get("statistics.csv", []):
            if (_get(row, "statistic") or "").lower() == "account name" and _get(row, "value"):
                return _user(row["value"])
        common: Optional[set] = None
        for row in tables.get("messages_archive.csv", []):
            people = {name for name in (_user(row.get("from")), _user(row.get("to"))) if name}
            common = people if common is None else common & people
        return next(iter(common)) if common and len(common) == 1 else None

    # -- What the account wrote --------------------------------------------------
    # posts.csv -- confirmed header: id, permalink, date, ip, subreddit,
    # gildings, title, url, body. `url` is the post's own path for a text post
    # ("/r/.../comments/..."), what it links to for a link or image post. `ip`
    # is filled on recent rows only.

    def _post(self, row: Row) -> Optional[NormalizedItem]:
        title, body, when = _get(row, "title"), _get(row, "body"), _parse_date(row.get("date"))
        if not title and not body and not when:
            return None
        url = _get(row, "url")
        permalink = _link(row.get("permalink"))
        shared = url if url and url.startswith(("http://", "https://")) and url.rstrip("/") != (permalink or "").rstrip("/") else None
        return NormalizedItem(
            item_type="post", category=POSTS, timestamp=when, title=title, content=body,
            context=_community(row.get("subreddit")),
            fields=_fields(**{
                "Lien": permalink, "Lien partagé": shared, "Adresse IP": _get(row, "ip"), "Récompenses": _awards(row),
            }),
            raw=row,
        )

    # comments.csv -- confirmed header: id, permalink, date, ip, subreddit,
    # gildings, gildings_silver, gildings_supergold, link (the post's URL),
    # parent (the comment answered; empty for a reply to the post itself),
    # body, media (empty on every row of the real export).

    def _comment(self, row: Row) -> Optional[NormalizedItem]:
        body, when = _get(row, "body"), _parse_date(row.get("date"))
        if not body and not when:
            return None
        permalink = _link(row.get("permalink"))
        parent = _get(row, "parent")
        reply_to = None
        # The comment answered, linked the way the export links this one -- its
        # own id swapped for the parent's. A parent written "t3_..." would be
        # the post: a top-level comment.
        match = _PERMALINK_RE.match(permalink or "")
        if parent and match and match.group("comment") and not parent.lower().startswith("t3_"):
            reply_to = f"{permalink[:match.start('comment')]}{_thing_id(parent)}/"
        return NormalizedItem(
            item_type="comment", category=COMMENTS, timestamp=when, content=body,
            context=_community(row.get("subreddit")) or _permalink(permalink)[0],
            fields=_fields(**{
                "Lien": permalink, "Publication": _link(row.get("link")), "En réponse à": reply_to,
                "Média": _get(row, "media"), "Adresse IP": _get(row, "ip"), "Récompenses": _awards(row),
            }),
            raw=row,
        )

    # -- Messages ------------------------------------------------------------------
    # messages_archive.csv -- confirmed header: id, permalink, thread_id, date,
    # ip, from, to, subject, body. Every message of the real export opens its
    # own thread (thread_id empty): that a reply names its thread's first
    # message there is how Reddit threads messages, not something a row of
    # the real export has shown.

    def _messages(self, rows: List[Row], owner: Optional[str]) -> Iterator[NormalizedItem]:
        by_id = {row["id"]: row for row in rows if row.get("id")}
        for row in rows:
            try:
                sender, recipient = _user(row.get("from")), _user(row.get("to"))
                body = _get(row, "body")
                if not body and not sender:
                    continue
                first = by_id.get(_thing_id(row.get("thread_id")) or "") or row
                yield NormalizedItem(
                    item_type="message", category=MESSAGES, timestamp=_parse_date(row.get("date")),
                    title=_get(row, "subject"), content=body, author=sender,
                    context=self._correspondent(first, owner),
                    fields=_fields(**{
                        "À": recipient, "Lien": _link(row.get("permalink")), "Adresse IP": _get(row, "ip"),
                    }),
                    raw=row,
                )
            except Exception:
                continue

    @staticmethod
    def _correspondent(message: Row, owner: Optional[str]) -> Optional[str]:
        sender, recipient = _user(message.get("from")), _user(message.get("to"))
        if sender and sender != owner:
            return sender
        return recipient or sender

    # chat_history.csv -- confirmed header, no row on the real export:
    # message_id, created_at, updated_at, username, message,
    # thread_parent_message_id, channel_url, subreddit, channel_name,
    # conversation_type. A chat is its channel.

    def _chat_message(self, row: Row) -> Optional[NormalizedItem]:
        content, sender = _get(row, "message"), _user(row.get("username"))
        if not content and not sender:
            return None
        channel_url = _get(row, "channel_url")
        return NormalizedItem(
            item_type="message", category=MESSAGES, timestamp=_parse_date(row.get("created_at")),
            content=content, author=sender,
            context=_get(row, "channel_name") or channel_url or _community(row.get("subreddit")),
            fields=_fields(**{
                "Communauté": _community(row.get("subreddit")), "Type de conversation": _get(row, "conversation_type"),
                "Lien": channel_url if channel_url and channel_url.startswith(("http://", "https://")) else None,
            }),
            raw=row,
        )

    # announcements.csv -- confirmed header: announcement_id, sent_at, read_at
    # (empty while unread), from_id, from_username, subject, body, url.

    def _announcement(self, row: Row) -> Optional[NormalizedItem]:
        subject, body = _get(row, "subject"), _get(row, "body")
        if not subject and not body:
            return None
        return NormalizedItem(
            item_type="announcement", category=ANNOUNCEMENTS, timestamp=_parse_date(row.get("sent_at")),
            title=subject, content=body, author=_user(row.get("from_username")),
            fields=_fields(**{"Lien": _get(row, "url"), "Lue le": _stamp(row.get("read_at"))}),
            raw=row,
        )

    # -- Votes and saves (undated: see the module docstring) -----------------------
    # post_votes.csv / comment_votes.csv -- confirmed header: id, permalink,
    # direction. saved_posts.csv / saved_comments.csv / hidden_posts.csv: id,
    # permalink.

    def _vote(self, row: Row, item_type: str) -> Optional[NormalizedItem]:
        permalink = _link(row.get("permalink"))
        if not permalink:
            return None
        community, _post, title = _permalink(permalink)
        direction = _get(row, "direction")
        return NormalizedItem(
            item_type=item_type, category=VOTES, title=title, context=community,
            fields=_fields(**{"Vote": _VOTE_LABELS.get((direction or "").lower(), direction), "Lien": permalink}),
            raw=row,
        )

    def _saved(self, row: Row, item_type: str, category: str) -> Optional[NormalizedItem]:
        permalink = _link(row.get("permalink"))
        if not permalink:
            return None
        community, _post, title = _permalink(permalink)
        return NormalizedItem(
            item_type=item_type, category=category, title=title, context=community,
            fields=_fields(**{"Lien": permalink}), raw=row,
        )

    # poll_votes.csv -- confirmed header: post_id ("t3_..."), user_selection
    # (the option's internal id), text (the option chosen), image_url,
    # is_prediction, stake_amount.

    def _poll_vote(self, row: Row) -> Optional[NormalizedItem]:
        option, post = _get(row, "text"), _thing_id(row.get("post_id"))
        if not option and not post:
            return None
        prediction = (_get(row, "is_prediction") or "").lower() == "true"
        return NormalizedItem(
            item_type="poll_vote", category=VOTES, content=option,
            fields=_fields(**{
                "Sondage": f"https://www.reddit.com/comments/{post}/" if post else None,
                "Prédiction": "Oui" if prediction else None, "Mise": _get(row, "stake_amount"),
                "Image": _get(row, "image_url"),
            }),
            raw=row,
        )

    # -- Communities and people --------------------------------------------------
    # subscribed_subreddits.csv, moderated_subreddits.csv,
    # approved_submitter_subreddits.csv -- confirmed header: subreddit.
    # friends.csv -- confirmed header, no row on the real export: username,
    # note. A Reddit "friend" is someone followed, one way: the Réseau tab's
    # "Abonnements".

    def _community_item(self, row: Row, item_type: str) -> Optional[NormalizedItem]:
        community = _community(row.get("subreddit"))
        if not community:
            return None
        return NormalizedItem(item_type=item_type, category=COMMUNITIES, title=community, context=community, raw=row)

    def _friend(self, row: Row) -> Optional[NormalizedItem]:
        name = _user(row.get("username"))
        if not name:
            return None
        return NormalizedItem(item_type="following", category=NETWORK, title=name, content=_get(row, "note"), raw=row)

    # -- The account ---------------------------------------------------------------
    # statistics.csv -- confirmed: statistic, value rows ("account name",
    # "export time", "is_deleted", "registration date", "email verified",
    # "email address"). The single-value files folded in with it, all
    # confirmed headers: account_gender.csv (account_gender), birthdate.csv
    # (source, verification_method, birthdate, age_greater_than_equal_to,
    # age_less_than, last_updated -- no row), linked_phone_number.csv
    # (phone_number), linked_identities.csv (issuer_id, subject_id),
    # stripe.csv (stripe_account_id), persona.csv (persona_inquiry_id), and
    # ip_logs.csv's registration row. Dated by the registration, so the
    # account's creation is on the timeline.

    def _profile(self, tables: Dict[str, List[Row]]) -> Optional[NormalizedItem]:
        try:
            stats: Dict[str, str] = {}
            for row in tables.get("statistics.csv", []):
                name, value = _get(row, "statistic"), _get(row, "value")
                if name and value:
                    stats[name.lower()] = value

            def first(file: str, column: str) -> Optional[str]:
                return next((_get(row, column) for row in tables.get(file, []) if _get(row, column)), None)

            birth = next(iter(tables.get("birthdate.csv", [])), {})
            fields = _fields(**{
                "E-mail": stats.get("email address"),
                "E-mail vérifié": _yes_no(stats.get("email verified")),
                "Compte supprimé": _yes_no(stats.get("is_deleted")),
                "IP d'inscription": next(
                    (_get(row, "ip") for row in tables.get("ip_logs.csv", [])
                     if (_get(row, "date") or "").lower() == _REGISTRATION_IP), None,
                ),
                "Genre": first("account_gender.csv", "account_gender"),
                "Date de naissance": _get(birth, "birthdate"),
                "Vérification de l'âge": _get(birth, "verification_method"),
                "Téléphone": first("linked_phone_number.csv", "phone_number"),
                "Comptes liés": " · ".join(
                    issuer for issuer in (_get(row, "issuer_id") for row in tables.get("linked_identities.csv", [])) if issuer
                ),
                "Compte Stripe": first("stripe.csv", "stripe_account_id"),
                "Vérification Persona": first("persona.csv", "persona_inquiry_id"),
                **{name.capitalize(): value for name, value in stats.items() if name not in _STATISTICS},
            })
            name = _user(stats.get("account name"))
            if not name and not fields:
                return None
            return NormalizedItem(
                item_type="profile", category=ACCOUNT, timestamp=_parse_date(stats.get("registration date")),
                title=name, content="Compte Reddit", fields=fields,
                raw={file: tables[file] for file in sorted(_PROFILE_FILES) if tables.get(file)},
            )
        except Exception:
            return None

    # ip_logs.csv -- confirmed header: date, ip. Its registration row is the
    # profile's; the others are what Reddit logged recently.

    def _ip_log(self, row: Row) -> Optional[NormalizedItem]:
        ip = _get(row, "ip")
        if not ip or (_get(row, "date") or "").lower() == _REGISTRATION_IP:
            return None
        return NormalizedItem(
            item_type="ip_log", category=SECURITY, timestamp=_parse_date(row.get("date")),
            title="Adresse IP enregistrée", fields={"Adresse IP": ip}, raw=row,
        )

    # user_preferences.csv -- confirmed: preference, value rows (122 on the
    # real export, Reddit's own setting names). One item, every setting a field.
    # sensitive_ads_preferences.csv -- confirmed: type, preference rows.

    def _settings(self, rows: List[Row]) -> Iterator[NormalizedItem]:
        fields = _fields(**{_get(row, "preference"): _get(row, "value") for row in rows if _get(row, "preference")})
        if fields:
            yield NormalizedItem(item_type="account_settings", category=ACCOUNT, title="Préférences du compte",
                                 fields=fields, raw=rows)

    def _ad_preferences(self, rows: List[Row]) -> Iterator[NormalizedItem]:
        fields = _fields(**{
            _SENSITIVE_AD_TOPICS.get(topic.lower(), topic): _SENSITIVE_AD_VALUES.get((value or "").lower(), value)
            for topic, value in ((_get(row, "type"), _get(row, "preference")) for row in rows) if topic
        })
        if fields:
            yield NormalizedItem(item_type="ad_preferences", category=ADS, title="Catégories publicitaires sensibles",
                                 fields=fields, raw=rows)

    # inferred_age_ads.csv ("Ads - Inferred User Age"), inferred_gender_ads.csv
    # ("Ads - Inferred User Gender", no row), inferred_age.csv (under_18,
    # under_16, under_13, persona_estimated_age) -- confirmed headers. One
    # inference per filled column, its value under "Valeur" like LinkedIn's,
    # which dedup leaves out of the fingerprint: an inference Reddit revises is
    # the same record.

    def _inferences(self, tables: Dict[str, List[Row]]) -> Iterator[NormalizedItem]:
        for file in _INFERENCE_FILES:
            for row in tables.get(file, []):
                for column, value in row.items():
                    if not value:
                        continue
                    yield NormalizedItem(
                        item_type="inference", category=INFERENCES,
                        title=_INFERENCE_LABELS.get(column.lower(), humanize(column)),
                        fields={"Valeur": _yes_no(value)}, raw=row,
                    )

    # -- Header-only and unknown files -------------------------------------------

    def _header_only(self, row: Row, item_type: str, category: str, date_column: str,
                     title_column: Optional[str]) -> Optional[NormalizedItem]:
        fields = _fields(**row)
        if not fields:
            return None
        return NormalizedItem(
            item_type=item_type, category=category, timestamp=_parse_date(row.get(date_column)),
            title=_get(row, title_column) if title_column else None,
            context=_community(row.get("subreddit")), fields=fields, raw=row,
        )

    # One item per row: every filled column kept as a field, dated by the
    # first date-like column (by name) that parses. The item type is the
    # file's name, so a file Reddit adds later shows up on its own.

    def _parse_generic(self, name: str, rows: Iterable[Row]) -> Iterator[NormalizedItem]:
        item_type = _slug(Path(name).stem)
        for row in rows:
            fields = _fields(**row)
            if not fields:
                continue
            timestamp = None
            for column, value in fields.items():
                lowered = column.lower()
                if "date" in lowered or "time" in lowered or lowered.endswith("_at"):
                    timestamp = _parse_date(value)
                    if timestamp:
                        break
            preview = " · ".join(f"{k} : {v}" for k, v in list(fields.items())[:3])
            yield NormalizedItem(
                item_type=item_type, category=OTHER, timestamp=timestamp,
                title=humanize(item_type), content=preview[:500], fields=fields, raw=row,
            )
