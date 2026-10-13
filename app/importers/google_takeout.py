"""Google Takeout is split across one folder per Google product, each with
its own file format and JSON shape (unlike Facebook/Instagram, which reuse a
handful of generic wrapper shapes across almost everything) -- there's no
single generic parser that covers most of it, so each category needs its own
small, dedicated method here.

File paths and JSON field names are grounded in the actively-maintained
open source parser https://github.com/purarue/google_takeout_parser rather
than guessed: notably, the real "My Activity" filename has changed over time
and is "My Activity.json" (with a space) in current exports, "MyActivity.json"
(no space) in older ones -- both are matched here, since guessing wrong
about a filename means the corresponding module silently returns nothing.

Folder AND file names are also localized to the export's language (confirmed
against real English/German/French exports); the underlying JSON's internal
keys are not. Every `_parse_*` method below that keys off an exact filename
therefore lists one candidate per confirmed language -- widen that list
first when a category from a non-English export comes back empty, rather
than assuming the module or JSON schema itself is broken.

Add a new category by adding another `_parse_*` method and registering it
in `parse()`; Takeout adds/renames categories over time, so treat any
service not covered here as a gap to close later, not a fixed scope.

"My Activity" is most of a real export (87k of 95k items on the one this
was checked against), one folder per Google service but the same sentence
shapes everywhere: "Application utilisée : Gmail", "Vous avez recherché
…", "Vous avez regardé …". Filed by service, it splits into some thirty
categories that say little ("Google Play Store" is mostly app launches,
"Recherche" mixes searches, opened results and notifications). Each entry
is therefore classified by what it records (`classify_activity`): its
category is one of a handful of usage families (Applications, Recherches,
Vidéos & TV...), its item_type the action, its title the thing acted on
(the app, the query, the video, the page, the destination) and its context
the service that logged it.
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from app.importers.base import BaseImporter, NormalizedItem
from app.importers.registry import register
from app.importers.utils import iter_files, load_json, guess_media_type, resolve_media_uri

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────
# What a "My Activity" entry records
# ────────────────────────────────────────────────

# Usage families: the categories Google items are filed under. OTHER is
# the catch-all, never given a colour of its own (see stats.series_slots).
APPS = "Applications"
SEARCH = "Recherches"
VIDEO = "Vidéos & TV"
WEB = "Web"
PLACES = "Trajets & lieux"
ASSISTANT = "Assistant & IA"
TRACKING = "Suivi par des tiers"
OTHER = "Autres"
NOTES = "Keep"
PHOTOS = "Google Photos"

# Service folder (lower-cased, see _service_name) -> the family its entries
# belong to unless their sentence says otherwise. French names confirmed on
# a real export, English ones their Takeout counterparts.
_SERVICE_FAMILIES = {
    "google play store": APPS, "android": APPS, "google play console": APPS, "google play": APPS,
    "recherche": SEARCH, "search": SEARCH, "recherche d'images": SEARCH, "image search": SEARCH,
    "google lens": SEARCH, "lens": SEARCH, "recherche de vidéos": SEARCH, "video search": SEARCH,
    "hôtels": SEARCH, "hotels": SEARCH, "vols": SEARCH, "flights": SEARCH, "voyage": SEARCH, "travel": SEARCH,
    "google shopping": SEARCH, "shopping": SEARCH,
    "youtube": VIDEO, "youtube music": VIDEO, "android tv": VIDEO, "google tv": VIDEO,
    "google play films et tv": VIDEO, "google play movies & tv": VIDEO,
    "chrome": WEB, "aide": WEB, "help": WEB, "google developers": WEB, "developers": WEB,
    "discover": WEB, "actualités": WEB, "google news": WEB,
    "maps": PLACES, "google maps": PLACES,
    "assistant": ASSISTANT, "applications gemini": ASSISTANT, "gemini apps": ASSISTANT, "gemini": ASSISTANT,
    "mode ia": ASSISTANT, "ai mode": ASSISTANT, "voice match": ASSISTANT,
    # Activity third-party apps and sites report through Google's analytics
    # and advertising SDKs: "Vous avez utilisé Scorely" logged by Scorely.
    "google analytics": TRACKING, "solutions publicitaires": TRACKING, "ads": TRACKING,
}


def _rule(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


_VISITED = r"(?:vous avez consulté|a consulté|visited|viewed)"
_USED = r"(?:vous avez utilisé|a utilisé|used)"

# (families the rule applies to -- None: any --, sentence pattern, family
# the entry is filed under -- None: its service's --, item_type). First
# match wins; group "e" is the thing acted on. French wording confirmed on a
# real export, English from Takeout's English sentences.
_VERB_RULES: List[Tuple[Optional[tuple], re.Pattern, Optional[str], str]] = [
    ((TRACKING,), _rule(rf"^{_VISITED}\s+(?P<e>.+)$"), None, "tracked_site"),
    ((TRACKING,), _rule(rf"^(?:{_USED}|application utilisée\s*:)\s*(?P<e>.+)$"), None, "tracked_app"),
    (None, _rule(r"^(?:\d+|une|a)\s+notifications?\b"), OTHER, "notification"),
    (None, _rule(r"^application utilisée\s*:\s*(?P<e>.+)$"), APPS, "app_used"),
    ((ASSISTANT,), _rule(r"^(?:vous avez recherché|searched for)\s*(?P<e>.*)$"), None, "ai_prompt"),
    (None, _rule(r"^(?:vous avez recherché|searched for)\s*(?P<e>.*)$"), SEARCH, "search"),
    (None, _rule(r"^(?:recherche effectuée avec|searched with) google lens"), SEARCH, "search"),
    (None, _rule(r"^(?:a consulté l'image sur|viewed image (?:on|from))\s+(?P<e>.+)$"), WEB, "image_view"),
    ((VIDEO,), _rule(r"^(?:vous avez regardé|a regardé|watched)\s+(?P<e>.+)$"), None, "video_watched"),
    ((VIDEO,), _rule(r"^(?:n'a pas aimé|vous n'avez pas aimé|disliked)\s+(?P<e>.+)$"), None, "video_disliked"),
    ((VIDEO,), _rule(r"^(?:a aimé|vous avez aimé|liked)\s+(?P<e>.+)$"), None, "video_liked"),
    ((VIDEO,), _rule(r"^(?:vous vous êtes abonnée? à|subscribed to)\s+(?P<e>.+)$"), None, "channel_subscribed"),
    ((VIDEO,), _rule(rf"^{_VISITED}\s+(?P<e>.+)$"), None, "video_page"),
    ((PLACES,), _rule(r"^(?:itinéraire pour|directions to)\s+(?P<e>.+)$"), None, "directions"),
    ((PLACES,), _rule(r"^(?:lieu découvert|explored)\b"), None, "place_discovered"),
    ((PLACES,), _rule(rf"^{_VISITED}\s+(?P<e>.+)$"), None, "place_viewed"),
    ((ASSISTANT,), _rule(r"^(?:vous avez dit|said)\s+(?P<e>.+)$"), None, "assistant_command"),
    ((ASSISTANT,), _rule(r"^(?:prompt\s*:|prompted)\s*(?P<e>.+)$"), None, "ai_prompt"),
    ((ASSISTANT,), _rule(r"^(?:empreinte vocale|voice model|voice match)"), None, "voice_match"),
    ((APPS,), _rule(r"^(?:a utilisé la fonctionnalité|used the feature)\s+(?P<e>.+)$"), None, "app_feature"),
    ((APPS,), _rule(rf"^{_VISITED}\s+(?P<e>.+)$"), None, "store_page"),
    # "Vous avez utilisé la Google Play Console": a Google service used,
    # not an app launched on the phone.
    ((APPS,), _rule(rf"^{_USED}\s+(?:la |le |les |l'|the )?(?P<e>.+)$"), None, "service_used"),
    # Chrome's own record of a visit: the same pages as its browsing history
    # (BrowserHistory.json), which the usage views prefer when present.
    (("chrome",), _rule(rf"^{_VISITED}\s+(?P<e>.+)$"), WEB, "chrome_visit"),
    (None, _rule(rf"^{_VISITED}\s+(?P<e>.+)$"), WEB, "page_visit"),
    (None, _rule(r"^(?:a utilisé|used)\s+(?P<e>chrome)$"), APPS, "app_used"),
    (None, _rule(r"^(?:vous avez traduit|translated)\b"), None, "translation"),
]

# The thing acted on, where it names an app: Play Store listing titles read
# "Spotify: Music and Podcasts" or "Fuel Finder - Price comparator", and pages
# reported by an SDK end with " - Apps on Google Play".
_APP_TYPES = {"app_used", "service_used", "tracked_app", "store_page"}
_STORE_SUFFIX_RE = re.compile(r"\s+[-–]\s+(?:apps on google play|applications? sur google play|applis sur google play)$", re.I)
_TAGLINE_RE = re.compile(r":\s+|\s+[-–|]\s+")
_MAX_ENTITY = 300


def _service_name(folder: str) -> str:
    """A service's folder name as Google writes it: Takeout puts a
    non-breaking space in brand names and an underscore for an apostrophe
    ("Recherche d_images")."""
    return folder.replace("\xa0", " ").replace("_", "'").strip()


def _clean_text(text: Optional[str]) -> str:
    return " ".join((text or "").replace("\xa0", " ").split())


def app_name(name: str) -> str:
    """"Spotify: Music and Podcasts" -> "Spotify"."""
    name = _STORE_SUFFIX_RE.sub("", name.strip())
    return _TAGLINE_RE.split(name, 1)[0].strip() or name


def classify_activity(service: str, text: Optional[str]) -> Tuple[str, str, Optional[str]]:
    """(category, item_type, entity) of one "My Activity" entry: the usage
    family it belongs to, what it records, and what it was about -- the
    app, query, video, page, destination or command, None when the
    sentence names nothing. An entry whose sentence isn't recognised stays
    in its service's family as a plain "activity"."""
    key = _service_name(service).lower()
    family = _SERVICE_FAMILIES.get(key, OTHER)
    sentence = _clean_text(text)
    for families, pattern, target, item_type in _VERB_RULES:
        if families and family not in families and key not in families:
            continue
        match = pattern.match(sentence)
        if not match:
            continue
        entity = (match.groupdict().get("e") or "").strip() or None
        if entity and item_type in _APP_TYPES:
            entity = app_name(entity)
        if entity and entity.startswith(("http://", "https://")):
            # Only a link (a video no longer available, a page without a
            # title): nothing to rank it under.
            entity = None
        return target or family, item_type, entity[:_MAX_ENTITY] if entity else None
    if family == PLACES and sentence:
        # Maps lists a place looked at under its bare name, no verb.
        return PLACES, "place_viewed", sentence[:_MAX_ENTITY]
    return family, "activity", None


_REDIRECT_PARAMS = ("q", "url", "imgrefurl")


def unwrap_link(url: Optional[str]) -> Optional[str]:
    """The page a Google redirect points to ("https://www.google.com/url?q=
    https://example.com/..." -> "https://example.com/..."): "My Activity"
    links an opened search result or Chrome visit through one."""
    if not url:
        return url
    parsed = urlparse(url)
    if parsed.hostname and "google." in parsed.hostname and parsed.path in ("/url", "/imgres"):
        params = parse_qs(parsed.query)
        for name in _REDIRECT_PARAMS:
            target = (params.get(name) or [None])[0]
            if target and target.startswith(("http://", "https://")):
                return target
    return url


def site_of(url: Optional[str]) -> Optional[str]:
    """"https://www.example.com/a" -> "example.com"."""
    host = urlparse(url).hostname if url else None
    if not host:
        return None
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


_GOOGLE_HOST_RE = re.compile(r"(?:^|\.)google\.[a-z.]+$")


def is_google_results_page(url: Optional[str]) -> bool:
    """A Google results page ("https://www.google.com/search?q=…"): the
    visible side of a search "My Activity" already records as one."""
    parsed = urlparse(url) if url else None
    return bool(parsed and parsed.hostname and _GOOGLE_HOST_RE.search(parsed.hostname)
                and parsed.path.startswith("/search"))


# Item types whose `author` is the site of the page they are about.
_SITE_AUTHOR_TYPES = {"page_visit", "chrome_visit", "image_view", "tracked_site", "results_page"}
# ...and whose `author` is the channel, the first line under the title.
_CHANNEL_AUTHOR_TYPES = {"video_watched", "video_liked", "video_disliked"}


def _normalized_path_str(path: Path) -> str:
    """Google renders every brand-name folder with a non-breaking space
    instead of a regular one ("Google\xa0Play\xa0Store", "Google\xa0Photos",
    "YouTube\xa0Music"...) -- invisible in Explorer/Finder, but it silently
    breaks a plain `"google play store" in str(path).lower()` check since
    U+00A0 != U+0020. Confirmed against a real export where this made the
    Play Store folder invisible to its own substring check. Use this
    wherever a path is matched by a lowercased literal string."""
    return str(path).replace("\xa0", " ").lower()


def _parse_any_timestamp(value: Any) -> Optional[datetime]:
    """Takeout mixes epoch seconds, milliseconds, microseconds (as int or as
    numeric string) and ISO 8601 strings depending on the category -- this
    covers all of them via one magnitude-based heuristic rather than each
    category method reimplementing its own guess.

    Always the server's local time, without a timezone: an epoch reads as
    local time, and an ISO string carrying an offset ("...Z") is converted
    to it -- the same clock for every category, so a Chrome visit and the
    "My Activity" record of it land on the same hour."""
    if value is None:
        return None
    if isinstance(value, str):
        if value.strip().isdigit():
            return _parse_any_timestamp(int(value))
        try:
            return _local(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    if isinstance(value, (int, float)):
        if value > 10**14:
            seconds = value / 1_000_000  # microseconds
        elif value > 10**11:
            seconds = value / 1000  # milliseconds
        else:
            seconds = value
        try:
            return datetime.fromtimestamp(seconds)
        except (ValueError, OSError, OverflowError):
            return None
    return None


def _local(dt: datetime) -> datetime:
    """An aware datetime as the server's naive local time; a naive one as is."""
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


@register
class GoogleTakeoutImporter(BaseImporter):
    key = "google"
    label = "Google Takeout"
    type_labels = {
        "activity": "Activité",
        "app_used": "Application ouverte",
        "app_feature": "Fonctionnalité Android",
        "service_used": "Service Google utilisé",
        "app_install": "Application installée",
        "store_page": "Page Play Store",
        "search": "Recherche",
        "page_visit": "Page consultée",
        "chrome_visit": "Page consultée (Chrome)",
        "image_view": "Image consultée",
        "browser_history": "Historique Chrome",
        "results_page": "Page de résultats Google",
        "video_watched": "Vidéo regardée",
        "video_liked": "Vidéo aimée",
        "video_disliked": "Vidéo pas aimée",
        "channel_subscribed": "Abonnement à une chaîne",
        "video_page": "Page YouTube consultée",
        "youtube_like": "Vidéo likée (playlist)",
        "directions": "Itinéraire",
        "place_viewed": "Lieu consulté",
        "place_discovered": "Lieu découvert",
        "place_visit": "Lieu visité",
        "assistant_command": "Commande vocale",
        "ai_prompt": "Prompt IA",
        "voice_match": "Empreinte vocale",
        "tracked_app": "Appli tierce",
        "tracked_site": "Site tiers",
        "notification": "Notification",
        "translation": "Traduction",
        "keep_note": "Note Keep",
        "photo": "Photo",
    }

    def detect(self, root: Path) -> bool:
        if any(root.rglob("archive_browser.html")):
            return True
        if any(root.rglob("My Activity")):
            return True
        if any(p.name.lower() in ("myactivity.json",) for p in root.rglob("*.json")):
            return True
        if any(root.glob("Takeout")) or root.name == "Takeout":
            return True
        if any(root.rglob("*.supplemental-metadata.json")):
            return True
        return False

    def parse(self, root: Path) -> Iterator[NormalizedItem]:
        yield from self._parse_my_activity(root)
        yield from self._parse_semantic_location_history(root)
        yield from self._parse_chrome_history(root)
        yield from self._parse_keep(root)
        yield from self._parse_youtube_likes(root)
        yield from self._parse_play_store_installs(root)
        yield from self._parse_photos(root)

    # -- My Activity: generic across dozens of services --------------------
    # {"header": "Search", "title": "Searched for X", "titleUrl": "...",
    #  "time": "2023-01-01T12:00:00Z", "locationInfos": [...], ...}
    # one file per service, e.g. "My Activity/Search/My Activity.json" --
    # every sub-service folder repeats a file named after the TOP "My
    # Activity" folder itself, and that top folder name is localized: a
    # real French export confirmed the top folder is "Mon activité" and
    # (by the same pattern already confirmed in English and German exports)
    # the per-service file inside is "Mon activité.json". The JSON's
    # internal keys (header/title/titleUrl/time/...) stay in English in
    # every export regardless of locale, so nothing else here needs to
    # change to support a new language -- just add its folder name below.

    def _parse_my_activity(self, root: Path) -> Iterator[NormalizedItem]:
        seen = set()
        for filename in ("My Activity.json", "MyActivity.json", "Mon activité.json"):
            for path in iter_files(root, filename):
                if path in seen:
                    continue
                seen.add(path)
                service = path.parent.name
                try:
                    data = load_json(path)
                except Exception:
                    continue
                if not isinstance(data, list):
                    continue
                for entry in data:
                    if not isinstance(entry, dict):
                        continue
                    try:
                        yield self._normalize_activity(
                            header=entry.get("header"),
                            title=entry.get("title"),
                            title_url=entry.get("titleUrl"),
                            timestamp=_parse_any_timestamp(entry.get("time")),
                            products=[str(p) for p in (entry.get("products") or [])],
                            locations=[
                                loc.get("name") for loc in (entry.get("locationInfos") or [])
                                if isinstance(loc, dict) and loc.get("name")
                            ],
                            service=service,
                            raw=entry,
                            subtitles=[
                                s.get("name") for s in (entry.get("subtitles") or [])
                                if isinstance(s, dict) and s.get("name")
                            ],
                            details=[
                                d.get("name") for d in (entry.get("details") or [])
                                if isinstance(d, dict) and d.get("name")
                            ],
                        )
                    except Exception:
                        continue

        # A real French export confirmed "Mon activité" can be requested in
        # HTML instead of JSON (Takeout lets you pick the format per
        # category) -- in that case there is no .json file here at all, so
        # this has to be a second, independent pass over the .html shape
        # rather than a fallback triggered by the JSON pass finding nothing.
        yield from self._parse_my_activity_html(root)

    def _normalize_activity(
        self, *, header: Optional[str], title: Optional[str], title_url: Optional[str],
        timestamp: Optional[datetime], products: list, locations: list,
        service: str, raw: Any, details: Optional[list] = None, subtitles: Optional[list] = None,
        body: Optional[str] = None,
    ) -> NormalizedItem:
        """`title` is the entry's sentence ("Vous avez regardé …"), kept as
        the content unless a fuller `body` is given (a Gemini prompt and its
        answer); `subtitles` the lines under it (a video's channel, a
        route's start and end). See classify_activity for the rest."""
        service = _service_name(service)
        category, item_type, entity = classify_activity(service, title)
        link = unwrap_link(title_url)
        subtitles = [s for s in (_clean_text(s) for s in (subtitles or [])) if s]
        if item_type in ("page_visit", "chrome_visit"):
            if is_google_results_page(link):
                item_type = "results_page"
            elif site_of(link) == "local.google.com":
                # A business opened from the results: a place looked at.
                category, item_type = PLACES, "place_viewed"

        author = None
        if item_type in _CHANNEL_AUTHOR_TYPES and subtitles:
            author = subtitles[0]
        elif item_type in _SITE_AUTHOR_TYPES:
            author = site_of(link)
        if item_type == "tracked_app" and header and _clean_text(header) != service:
            # The SDK's own name for the app, fuller than the sentence's.
            entity = app_name(_clean_text(header))

        fields: Dict[str, Any] = {"Service": service}
        header = _clean_text(header)
        if header and header not in (service, entity):
            fields["Rubrique"] = header
        if link:
            fields["Lien"] = link
        if author and item_type in _SITE_AUTHOR_TYPES:
            fields["Site"] = author
        if author and item_type in _CHANNEL_AUTHOR_TYPES:
            fields["Chaîne"] = author
        elif subtitles:
            fields["Précisions"] = " · ".join(subtitles)
        if locations:
            fields["Lieu"] = ", ".join(_clean_text(loc) for loc in locations)
        if products:
            fields["Produit"] = ", ".join(_clean_text(p) for p in products)
        if details:
            fields["Détail"] = "; ".join(_clean_text(d) for d in details)

        return NormalizedItem(
            item_type=item_type,
            category=category,
            timestamp=timestamp,
            title=entity,
            content=body or title,
            author=author,
            context=service,
            fields=fields,
            media=[],
            raw=raw,
        )

    # -- My Activity (HTML variant) ------------------------------------------
    # Structurally grounded in purarue/google_takeout_parser's HTML parser
    # (google_takeout_parser/parse_html/activity.py): each activity is a
    # `<div class="outer-cell ...">` containing a header (`<p
    # class="mdl-typography--title">`), a "content-cell ... body-1" cell
    # holding the title/link followed by a trailing plain-text timestamp
    # after the last <br>, and a "content-cell ... caption" cell holding
    # "Products:"/"Details:" groups. These CSS class names are Google's
    # fixed internal markup, not localized text, so this structural part
    # applies to any export language -- ONLY the trailing timestamp's text
    # format is locale-dependent, and unlike the rest, hasn't been verified
    # against real French content (the anonymizer empties all HTML, by
    # design, so there was nothing safe to inspect); _parse_html_activity_dt
    # tries several candidate formats and returns None rather than raising
    # or guessing when it doesn't recognize one, so a real formatting
    # mismatch loses only the date on an entry, never the entry itself.

    @staticmethod
    def _html_parser_backend() -> str:
        """lxml is dramatically faster than the stdlib html.parser on the
        multi-thousand-entry files a real "My Activity" export produces
        (years of search/YouTube/Discover history) -- used when available,
        falling back to html.parser (always present) otherwise so this
        never hard-fails on a build that doesn't have lxml."""
        try:
            import lxml  # noqa: F401
            return "lxml"
        except ImportError:
            return "html.parser"

    def _parse_my_activity_html(self, root: Path) -> Iterator[NormalizedItem]:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            # No noise on a deployment that legitimately has no HTML "My
            # Activity" files -- but this is also the exact failure mode
            # when the image was built before beautifulsoup4 was added to
            # requirements.txt, which otherwise looks identical to "there's
            # nothing to parse" from the outside (0 items, no error). One
            # explicit log line turns that ambiguity into a fact.
            logger.warning(
                "Google Takeout: beautifulsoup4 not installed, "
                "skipping HTML 'My Activity' parsing entirely"
            )
            return

        parser_backend = self._html_parser_backend()
        logger.info("Google Takeout: using '%s' as the HTML parser backend", parser_backend)

        seen = set()
        files_found = 0
        divs_found = 0
        items_yielded = 0
        items_failed = 0
        first_failures: list = []
        # Counts per failure reason across EVERY entry, not just a
        # handful of samples -- a structural mismatch tends to affect all
        # several thousand entries in a file identically, so knowing which
        # single reason dominates (and by how much) is far more conclusive
        # than 2-5 examples, and needs no further back-and-forth to get.
        reason_counts: dict = {}

        for filename in ("My Activity.html", "MyActivity.html", "MonActivité.html"):
            for path in iter_files(root, filename):
                if path in seen:
                    continue
                seen.add(path)
                files_found += 1
                service = path.parent.name
                # A real "My Activity" export can hold years of search/
                # YouTube/Discover history -- easily tens of thousands of
                # entries in one file. The final summary log below only
                # fires once every file is fully done, which on a file that
                # size can look identical to "hung" for several minutes;
                # this logs BEFORE the slow part starts so progress is
                # visible immediately rather than only at the very end.
                logger.info("Google Takeout: parsing %s (%s)...", path, service)
                try:
                    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), parser_backend)
                except Exception as exc:
                    logger.warning("Google Takeout: could not parse HTML file %s: %r", path, exc)
                    continue
                divs = soup.find_all("div", class_=lambda c: c and "outer-cell" in c.split())
                logger.info("Google Takeout: %s -- %d entries to process", path, len(divs))
                divs_found += len(divs)
                for i, div in enumerate(divs, start=1):
                    try:
                        item, reason = self._parse_activity_div(div, service)
                    except Exception as exc:
                        items_failed += 1
                        reason_counts[f"exception:{type(exc).__name__}"] = (
                            reason_counts.get(f"exception:{type(exc).__name__}", 0) + 1
                        )
                        if len(first_failures) < 5:
                            first_failures.append(repr(exc))
                        continue
                    if reason:
                        reason_counts[reason] = reason_counts.get(reason, 0) + 1
                    if item is not None:
                        items_yielded += 1
                        yield item
                    else:
                        items_failed += 1
                    if i % 2000 == 0:
                        logger.info("Google Takeout: %s -- %d/%d entries processed", path, i, len(divs))

        if files_found:
            logger.info(
                "Google Takeout: My Activity HTML -- %d file(s), %d entr(y/ies) found, "
                "%d parsed, %d failed/skipped, reason counts=%s%s",
                files_found, divs_found, items_yielded, items_failed, reason_counts,
                f" (sample errors: {first_failures})" if first_failures else "",
            )
        else:
            logger.info(
                "Google Takeout: My Activity HTML -- no file matched "
                "('My Activity.html' / 'MyActivity.html' / 'MonActivité.html')"
            )

    def _parse_activity_div(self, div: Any, service: str) -> tuple:
        """Returns (item, reason): reason is None on a clean parse, a short
        machine-readable code identifying exactly why on a hard failure
        (item is None then), or "date_unparsed" when the item parsed fine
        but its trailing date text didn't match any known format. The
        caller tallies these into full counts across every entry instead of
        only ever seeing a handful of samples -- essential on a real export
        where a structural mismatch can affect all several thousand
        entries identically."""
        header_el = div.find("p", class_=lambda c: c and "mdl-typography--title" in c.split())
        header = header_el.get_text(strip=True) if header_el else None

        subtitle_cell = None
        caption_cell = None
        for cell in div.find_all("div", class_=lambda c: c and "content-cell" in c.split()):
            classes = cell.get("class") or []
            # A real export confirmed there's a SECOND cell that also
            # carries "mdl-typography--body-1" alongside "...--text-right"
            # -- a near-empty right-alignment spacer, not the real content.
            # Without this exclusion it was overwriting subtitle_cell with
            # that empty spacer on every single entry (matching purarue's
            # own "these are used for spacing on the right" comment, which
            # this method had dropped when it was first written).
            if "mdl-typography--text-right" in classes:
                continue
            if "mdl-typography--body-1" in classes:
                subtitle_cell = cell
            elif "mdl-typography--caption" in classes:
                caption_cell = cell

        if subtitle_cell is None:
            self._log_activity_div_structure(div, reason="no_subtitle_cell")
            return None, "no_subtitle_cell"

        # The timestamp is the last MEANINGFUL child node of the cell (a
        # plain text node) -- matches purarue's approach of popping the
        # literal last child, but first trims any trailing whitespace-only
        # text node or stray <br> tag, since a real export isn't guaranteed
        # to end the cell exactly on the date text (an extra trailing <br>
        # here used to make the popped "last child" a Tag instead of the
        # date string, silently returning None for every single entry).
        children = list(subtitle_cell.children)
        while children and (
            (isinstance(children[-1], str) and not str(children[-1]).strip())
            or getattr(children[-1], "name", None) == "br"
        ):
            children.pop()
        if not children:
            self._log_activity_div_structure(div, reason="subtitle_cell_empty_after_trim")
            return None, "subtitle_cell_empty_after_trim"

        dt_text: Optional[str] = None
        timestamp: Optional[datetime] = None
        title: Optional[str] = None
        title_url: Optional[str] = None
        subtitles: list = []
        body: Optional[str] = None
        reason: Optional[str] = None

        if isinstance(children[-1], str):
            # The common shape: a title line (sentence + optional link),
            # optional lines under it (a YouTube video's channel, a Maps
            # route's start and end), then the date as plain trailing text.
            dt_raw_el = children.pop()
            dt_text = str(dt_raw_el).strip()
            timestamp = self._parse_html_activity_dt(dt_text)
            groups = self._group_html_by_br(children)
            if groups:
                title, title_url = self._flatten_html_group(groups[0])
            subtitles = [text for text, _url in (self._flatten_html_group(g) for g in groups[1:]) if text]
            if timestamp is None:
                # A Gemini entry can end on "1 fichier joint." with its date
                # on a line above: the last line is then just another one.
                for i, line in enumerate(subtitles):
                    timestamp = self._parse_html_activity_dt(line)
                    if timestamp is not None:
                        subtitles = subtitles[:i] + subtitles[i + 1:] + ([dt_text] if dt_text else [])
                        dt_text = line
                        break
            reason = None if timestamp is not None else "date_unparsed"
        else:
            # Some categories (confirmed on a real export: Gemini app
            # conversation transcripts under "Applications Gemini") render
            # rich, multi-paragraph content here instead -- the prompt, its
            # date on a line of its own, then the answer as headings, lists,
            # tables, code blocks -- with no plain trailing date node to pop.
            # The first line is the entry's sentence, the first line reading
            # as a date its date, and the whole text its content: get_text()
            # flattens it however deeply nested the markup is.
            groups = self._group_html_by_br(children)
            if groups:
                title, title_url = self._flatten_html_group(groups[0])
            for group in groups[1:]:
                if all(getattr(node, "name", None) is None for node in group):
                    line = "".join(str(node) for node in group).strip()
                    timestamp = self._parse_html_activity_dt(line)
                    if timestamp is not None:
                        dt_text = line
                        break
            text = " ".join(
                str(node) if isinstance(node, str) else node.get_text(" ", strip=True)
                for node in children
            )
            body = text.replace("\xa0", " ").strip() or None
            if title_url is None:
                link_el = subtitle_cell.find("a")
                title_url = link_el.get("href") if link_el else None
            if timestamp is None:
                last_tag = getattr(children[-1], "name", None)
                reason = f"no_trailing_date(tag={last_tag!r})"
                self._log_activity_div_structure(div, reason=reason)

        caption = self._parse_html_caption(caption_cell) if caption_cell is not None else {}
        products = caption.get("products", [])
        details = caption.get("details", [])
        locations = caption.get("locations", [])

        item = self._normalize_activity(
            header=header, title=title, title_url=title_url, timestamp=timestamp,
            products=products, locations=locations, details=details, service=service, subtitles=subtitles,
            body=body,
            raw={"header": header, "title": title, "titleUrl": title_url, "subtitles": subtitles, "body": body,
                 "products": products, "details": details, "locations": locations, "raw_time_text": dt_text},
        )
        # Neither branch above is a hard failure -- the item is always
        # returned/counted as parsed here. `reason` is still surfaced
        # ("date_unparsed" / "no_trailing_date(...)") purely as a
        # second-order signal so the summary log can distinguish "working
        # but missing dates on this shape" from "broken", without another
        # log round-trip.
        return item, reason

    _structure_log_count = 0

    @classmethod
    def _log_activity_div_structure(cls, div: Any, reason: str) -> None:
        """Every entry in a real export returning None here (as opposed to
        raising) means some structural assumption is wrong -- e.g. Google
        changed a CSS class or the cell's trailing markup since the
        reference implementation this was grounded in was last updated.
        Logs ONLY class attributes, tag names, and child NODE TYPES -- never
        actual text content -- so this is safe to run against real
        (non-anonymized) data and safe to paste back. Capped at 5 total logs
        across the whole run so a structural mismatch affecting every entry
        in a multi-thousand-entry file doesn't flood the log."""
        if cls._structure_log_count >= 5:
            return
        cls._structure_log_count += 1
        try:
            header_el = div.find("p", class_=lambda c: c and "mdl-typography--title" in c.split())
            content_cells = div.find_all("div", class_=lambda c: c and "content-cell" in c.split())
            all_divs = div.find_all("div")
            subtitle_children_shape = None
            for cell in content_cells:
                classes = cell.get("class") or []
                if "mdl-typography--text-right" in classes:
                    continue
                if "mdl-typography--body-1" in classes:
                    subtitle_children_shape = [
                        (getattr(child, "name", None) or "text") for child in cell.children
                    ]
                    break
            logger.warning(
                "Google Takeout: activity div structure sample (reason: %s) -- "
                "outer class=%r, header found=%s, content-cell count=%d, "
                "content-cell classes=%s, all div classes (first 10)=%s, "
                "subtitle_cell children node types=%s",
                reason,
                div.get("class"),
                header_el is not None,
                len(content_cells),
                [c.get("class") for c in content_cells],
                [d.get("class") for d in all_divs[:10]],
                subtitle_children_shape,
            )
        except Exception as exc:
            logger.warning("Google Takeout: could not log activity div structure: %r", exc)

    @staticmethod
    def _group_html_by_br(children: list) -> list:
        """Splits a cell's direct children into groups separated by <br>
        tags -- needed because a single subtitle line can mix plain text
        and an <a> link (e.g. 'Recherché ' + <a>restaurants</a>), so a
        naive split on NavigableString boundaries alone would break it up
        wrong. Mirrors purarue's _group_by_brs."""
        groups: list = []
        current: list = []
        for child in children:
            name = getattr(child, "name", None)
            if name == "br":
                groups.append(current)
                current = []
            else:
                current.append(child)
        if current:
            groups.append(current)
        return groups

    @staticmethod
    def _flatten_html_group(group: list) -> tuple:
        text = ""
        url = None
        for node in group:
            name = getattr(node, "name", None)
            if name == "a":
                text += node.get_text()
                href = node.get("href")
                if href:
                    url = href
            elif name is not None:
                text += node.get_text(" ")
            else:
                text += str(node)
        return (text.replace("\xa0", " ").strip() or None), url

    # Caption headers, English and French (confirmed on a real export, where
    # they read "Produits\xa0:", "Lieux\xa0:", "Informations détaillées\xa0:")
    # -> the list their lines go to. Any other bold header ("Why is this
    # here?" / "Que fait cette information ici ?") starts a group that is
    # ignored, rather than its explanation being read as a product.
    _CAPTION_HEADERS = {
        "products": "products", "produits": "products",
        "details": "details", "informations détaillées": "details",
        "locations": "locations", "lieux": "locations",
    }

    def _parse_html_caption(self, cap_cell: Any) -> Dict[str, list]:
        """{"products"/"details"/"locations": [lines]}: each is rendered as
        a bold header line followed by one or more value lines until the
        next bold header -- see purarue's _split_by_caption_headers for the
        same shape."""
        groups = self._group_html_by_br(list(cap_cell.children))
        result: Dict[str, list] = {}
        current = None
        for group in groups:
            # A whitespace-only text node (a stray newline/indent between a
            # <br> and the next tag) would otherwise land at group[0] and
            # hide the actual <b>Header:</b> tag behind it, so the header
            # never gets recognized -- filtered out before the header check.
            meaningful = [g for g in group if not (isinstance(g, str) and not g.strip())]
            if not meaningful:
                continue
            first = meaningful[0]
            if getattr(first, "name", None) == "b":
                label = first.get_text(strip=True).replace("\xa0", " ").rstrip(" :").lower()
                current = self._CAPTION_HEADERS.get(label)
                meaningful = meaningful[1:]
                if not meaningful:
                    continue
            text, _ = self._flatten_html_group(meaningful)
            if text and current:
                result.setdefault(current, []).append(" ".join(text.split()))
        return result

    # strptime's %b/%B are locale-dependent (the running process's C locale,
    # not the export's language), so "mars"/"aout" never match them no
    # matter the format string -- confirmed by testing against a French
    # date directly. Built by hand instead: covers English (used pre-2018
    # and possibly in an English-locale export) and French month names,
    # abbreviated and full, with or without the trailing period Google
    # sometimes adds ("sept." vs "sept").
    _MONTH_NAMES = {
        "jan": 1, "january": 1, "janv": 1, "janvier": 1,
        "feb": 2, "february": 2, "fevr": 2, "fevrier": 2,
        "mar": 3, "march": 3, "mars": 3,
        "apr": 4, "april": 4, "avr": 4, "avril": 4,
        "may": 5, "mai": 5,
        "jun": 6, "june": 6, "juin": 6,
        "jul": 7, "july": 7, "juil": 7, "juillet": 7,
        "aug": 8, "august": 8, "aout": 8,
        "sep": 9, "sept": 9, "september": 9, "septembre": 9,
        "oct": 10, "october": 10, "octobre": 10,
        "nov": 11, "november": 11, "novembre": 11,
        "dec": 12, "december": 12, "decembre": 12,
    }
    # Accented month names get their accents stripped before lookup (see
    # _strip_accents) so "février"/"août"/"décembre" match the
    # unaccented keys above without needing every accented variant listed.

    # French: "8 mars 2018, 17:14:40" or "...2018 à 17:14:40" (day first, 24h).
    _HTML_DT_RE_DAY_FIRST = re.compile(
        # The optional connector before the time ("à", or just a comma) is
        # restricted to non-digit characters -- a greedy `\S+` here used to
        # be able to backtrack into eating a leading hour digit (e.g. "17"
        # losing its "1") whenever there was no literal connector word to
        # anchor it to, silently corrupting the hour by up to 10.
        r"^(?P<day>\d{1,2})\s+(?P<month>[^\d,\s.]+)\.?\s+(?P<year>\d{4})[,]?\s*(?:[^\d\s]+\s+)?"
        r"(?P<hour>\d{1,2}):(?P<minute>\d{2}):(?P<second>\d{2})$",
        re.IGNORECASE,
    )
    # English: "Mar 8, 2018, 5:14:40 PM" (month first, 12h with AM/PM).
    _HTML_DT_RE_MONTH_FIRST = re.compile(
        r"^(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),\s+(?P<year>\d{4}),\s+"
        r"(?P<hour>\d{1,2}):(?P<minute>\d{2}):(?P<second>\d{2})\s+(?P<ampm>AM|PM)$",
        re.IGNORECASE,
    )

    @staticmethod
    def _strip_accents(s: str) -> str:
        import unicodedata
        return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))

    # UTC offset (hours) of the timezone abbreviations a "My Activity" date
    # ends with. Google writes EVERY date of an export with the offset in
    # force on the day it was taken -- a real export taken in October dates
    # March entries "CEST" too, an hour later than the local clock read
    # then -- so the date is converted from the offset it states, not read
    # as local time.
    _TZ_OFFSETS = {
        "UTC": 0, "GMT": 0, "WET": 0, "WEST": 1, "BST": 1, "CET": 1, "CEST": 2, "EET": 2, "EEST": 3,
        "EST": -5, "EDT": -4, "CST": -6, "CDT": -5, "MST": -7, "MDT": -6, "PST": -8, "PDT": -7,
    }

    def _parse_html_activity_dt(self, raw: str) -> Optional[datetime]:
        text = raw.replace(" ", " ").strip()
        text = "".join(" " if ch.isspace() else ch for ch in text)
        if not text:
            return None
        # A trailing timezone abbreviation (UTC, CET, PST...) is split off:
        # a known one converts the date to local time, an unknown one leaves
        # it as written. AM/PM must never be stripped here even though it
        # also looks like a short all-caps token: it's the format's own 12h
        # marker, not a timezone, and the English regex below requires it.
        body = text
        offset = None
        tokens = text.rsplit(" ", 1)
        if (
            len(tokens) == 2 and tokens[1].isalpha() and tokens[1].upper() == tokens[1]
            and 2 <= len(tokens[1]) <= 5 and tokens[1] not in ("AM", "PM")
        ):
            body = tokens[0]
            offset = self._TZ_OFFSETS.get(tokens[1])

        for regex in (self._HTML_DT_RE_DAY_FIRST, self._HTML_DT_RE_MONTH_FIRST):
            m = regex.match(body)
            if not m:
                continue
            month_key = self._strip_accents(m.group("month").strip(". ").lower())
            month = self._MONTH_NAMES.get(month_key)
            if month is None:
                continue
            hour = int(m.group("hour"))
            if m.groupdict().get("ampm"):
                ampm = m.group("ampm").upper()
                hour = (hour % 12) + (12 if ampm == "PM" else 0)
            try:
                written = datetime(
                    int(m.group("year")), month, int(m.group("day")),
                    hour, int(m.group("minute")), int(m.group("second")),
                )
            except ValueError:
                return None
            if offset is None:
                return written
            return _local(written.replace(tzinfo=timezone(timedelta(hours=offset))))
        return None

    # -- Semantic Location History: named place visits ----------------------
    # "Location History (Timeline)/Semantic Location History/<year>/<year>_
    # <MONTH>.json" -> {"timelineObjects": [{"placeVisit": {"location": {
    # "name":, "address":, "latitudeE7":, "longitudeE7":}, "duration": {
    # "startTimestamp":, "endTimestamp":}, "placeVisitType":}}, ...]}.
    # Raw per-second GPS pings (Records.json) are skipped deliberately: tens
    # to hundreds of thousands of anonymous coordinates per account would
    # dominate the database for near-zero browsing value next to the much
    # rarer, actually-named places this file already resolves them to.

    def _parse_semantic_location_history(self, root: Path) -> Iterator[NormalizedItem]:
        for path in root.rglob("*.json"):
            if "semantic location history" not in _normalized_path_str(path):
                continue
            try:
                data = load_json(path)
            except Exception:
                continue
            objects = data.get("timelineObjects") if isinstance(data, dict) else None
            if not isinstance(objects, list):
                continue
            for obj in objects:
                place_visit = obj.get("placeVisit") if isinstance(obj, dict) else None
                if not isinstance(place_visit, dict):
                    continue  # skip activitySegment (travel between places) for now
                try:
                    yield self._normalize_place_visit(place_visit)
                except Exception:
                    continue

    def _normalize_place_visit(self, place_visit: Dict[str, Any]) -> NormalizedItem:
        location = place_visit.get("location") or {}
        duration = place_visit.get("duration") or {}
        start = _parse_any_timestamp(duration.get("startTimestamp"))
        end = _parse_any_timestamp(duration.get("endTimestamp"))

        fields: Dict[str, Any] = {}
        if location.get("address"):
            fields["Adresse"] = location["address"]
        lat, lng = location.get("latitudeE7"), location.get("longitudeE7")
        if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
            fields["Coordonnées"] = f"{lat / 1e7:.6f}, {lng / 1e7:.6f}"
        if end:
            fields["Départ"] = end.strftime("%d/%m/%Y %H:%M")
        if place_visit.get("placeVisitType"):
            fields["Type"] = place_visit["placeVisitType"]

        return NormalizedItem(
            item_type="place_visit",
            category=PLACES,
            timestamp=start,
            title=location.get("name") or location.get("address"),
            content=None,
            author=None,
            context="Lieux visités",
            fields=fields or None,
            media=[],
            raw=place_visit,
        )

    # -- Chrome browsing history ---------------------------------------------
    # "Chrome/BrowserHistory.json" (or older "History.json") -> {"Browser
    # History": [{"time_usec":, "title":, "url":, "page_transition":}, ...]}.
    # A French export confirmed with real data names this "Historique.json"
    # instead -- the JSON's internal keys stay in English regardless of
    # locale (confirmed against Google's own Data Portability schema
    # reference), only this outer filename is translated, so no other
    # change is needed for non-English exports of this category.

    def _parse_chrome_history(self, root: Path) -> Iterator[NormalizedItem]:
        for filename in ("BrowserHistory.json", "History.json", "Historique.json"):
            for path in iter_files(root, filename):
                if "chrome" not in _normalized_path_str(path):
                    continue
                try:
                    data = load_json(path)
                except Exception:
                    continue
                entries = data.get("Browser History") if isinstance(data, dict) else None
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    try:
                        yield self._normalize_chrome_entry(entry)
                    except Exception:
                        continue

    def _normalize_chrome_entry(self, entry: Dict[str, Any]) -> NormalizedItem:
        site = site_of(entry.get("url"))
        fields = {"Service": "Chrome"}
        if site:
            fields["Site"] = site
        if entry.get("page_transition"):
            fields["Type"] = entry["page_transition"]
        return NormalizedItem(
            item_type="results_page" if is_google_results_page(entry.get("url")) else "browser_history",
            category=WEB,
            timestamp=_parse_any_timestamp(entry.get("time_usec")),
            title=entry.get("title"),
            content=entry.get("url"),
            author=site,
            context="Chrome",
            fields=fields,
            media=[],
            raw=entry,
        )

    # -- Keep notes -----------------------------------------------------------
    # "Keep/<note>.json" -> {"title":, "textContent":, "listContent": [{
    # "text":, "isChecked":}], "isPinned":, "isArchived":, "isTrashed":,
    # "createdTimestampUsec":}. One file per note, directly under "Keep/".

    def _parse_keep(self, root: Path) -> Iterator[NormalizedItem]:
        for path in iter_files(root, "*.json"):
            if path.parent.name != "Keep":
                continue
            try:
                data = load_json(path)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            try:
                yield self._normalize_keep_note(data)
            except Exception:
                continue

    def _normalize_keep_note(self, data: Dict[str, Any]) -> NormalizedItem:
        text = data.get("textContent")
        if not text:
            checklist = [
                c.get("text") for c in (data.get("listContent") or [])
                if isinstance(c, dict) and c.get("text")
            ]
            text = "\n".join(f"- {t}" for t in checklist) or None

        fields = {}
        for key, label in (("isPinned", "Épinglé"), ("isArchived", "Archivé"), ("isTrashed", "Corbeille")):
            if data.get(key):
                fields[label] = "Oui"

        return NormalizedItem(
            item_type="keep_note",
            category=NOTES,
            timestamp=_parse_any_timestamp(data.get("createdTimestampUsec")),
            title=data.get("title") or None,
            content=text,
            author=None,
            context="Keep",
            fields=fields or None,
            media=[],
            raw=data,
        )

    # -- YouTube liked videos ---------------------------------------------
    # "YouTube and YouTube Music/playlists/likes.json" -> [{"snippet": {
    # "title":, "description":, "publishedAt":}, "contentDetails": {
    # "videoId":}}, ...]

    def _parse_youtube_likes(self, root: Path) -> Iterator[NormalizedItem]:
        for path in iter_files(root, "likes.json"):
            if "playlists" not in _normalized_path_str(path):
                continue
            try:
                data = load_json(path)
            except Exception:
                continue
            if not isinstance(data, list):
                continue
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                try:
                    yield self._normalize_youtube_like(entry)
                except Exception:
                    continue

    def _normalize_youtube_like(self, entry: Dict[str, Any]) -> NormalizedItem:
        snippet = entry.get("snippet") or {}
        video_id = (entry.get("contentDetails") or {}).get("videoId")
        fields = {"Lien": f"https://www.youtube.com/watch?v={video_id}"} if video_id else None
        return NormalizedItem(
            item_type="youtube_like",
            category=VIDEO,
            timestamp=_parse_any_timestamp(snippet.get("publishedAt")),
            title=snippet.get("title"),
            content=snippet.get("description") or None,
            author=None,
            context="YouTube",
            fields=fields,
            media=[],
            raw=entry,
        )

    # -- Google Play Store app installs ---------------------------------
    # "Google Play Store/Installs.json" -> [{"install": {"doc": {"title":},
    # "deviceAttribute": {"deviceDisplayName":}, "firstInstallationTime":,
    # "lastUpdateTime":}}, ...]

    def _parse_play_store_installs(self, root: Path) -> Iterator[NormalizedItem]:
        for path in iter_files(root, "Installs.json"):
            if "google play store" not in _normalized_path_str(path):
                continue
            try:
                data = load_json(path)
            except Exception:
                continue
            if not isinstance(data, list):
                continue
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                try:
                    yield self._normalize_play_install(entry)
                except Exception:
                    continue

    def _normalize_play_install(self, entry: Dict[str, Any]) -> NormalizedItem:
        install = entry.get("install") or {}
        doc = install.get("doc") or {}
        device = install.get("deviceAttribute") or {}
        fields = {"Appareil": device["deviceDisplayName"]} if device.get("deviceDisplayName") else None
        return NormalizedItem(
            item_type="app_install",
            category=APPS,
            timestamp=_parse_any_timestamp(install.get("firstInstallationTime")),
            title=doc.get("title"),
            content=None,
            author=None,
            context="Google Play Store",
            fields=fields,
            media=[],
            raw=entry,
        )

    # -- Google Photos metadata sidecars -----------------------------------
    # "<Album>/<photo>.<ext>.supplemental-metadata.json" next to the actual
    # media file it describes.

    def _parse_photos(self, root: Path) -> Iterator[NormalizedItem]:
        for path in iter_files(root, "*.supplemental-metadata.json"):
            try:
                data = load_json(path)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            try:
                yield self._normalize_photo(root, path, data)
            except Exception:
                continue

    def _normalize_photo(self, root: Path, sidecar_path: Path, data: Dict[str, Any]) -> NormalizedItem:
        media_name = data.get("title") or sidecar_path.stem
        media_path = sidecar_path.parent / media_name
        media = []
        if media_path.is_file():
            rel = resolve_media_uri(root, str(media_path.relative_to(root)))
            if rel:
                media.append((rel, guess_media_type(rel)))

        taken = data.get("photoTakenTime") or {}

        return NormalizedItem(
            item_type="photo",
            category=PHOTOS,
            timestamp=_parse_any_timestamp(taken.get("timestamp")),
            title=data.get("title"),
            content=data.get("description") or None,
            author=None,
            context=sidecar_path.parent.name,
            media=media,
            raw=data,
        )
