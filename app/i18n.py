"""Translations: stable message keys, and the locale a request is answered in.

The UI was written in French, with the French strings used directly as display
text. This module is the mechanism that lets the same app answer in another
language, and it is deliberately dependency-free -- no gettext, no Babel, no
compiled catalogues -- because the app ships as a single container and a
translation is a few hundred short strings.

How a string is translated
--------------------------
A string the UI shows gets a **key** that names what it is, never what it says
(`nav.imports`, not `"Imports"`). The key is looked up in `CATALOGS[locale]`;
a key with no entry in a locale falls back, in order, to the default locale,
then to the key itself. Falling back to the key rather than to blank is on
purpose: a missing translation should be visible in the UI, not silently
render as an empty button.

That is also why a partially translated locale is safe to ship: nothing
disappears, some strings simply stay in the default language until someone
fills the catalogue in.

`test_every_key_exists_in_every_catalogue` keeps the catalogues in step: adding
a key to one locale without adding it to the others fails the suite, which is
the point. Note that the importers' own field labels (the label/value pairs in
`items.fields_json`) are *data* read back by label in `app/stats.py` and
`app/crud.py`, so they are not translatable without migrating stored rows.

Which locale a request gets
---------------------------
`negotiate()` resolves, in order: an explicit `?lang=` parameter, the
`TAKEBACK_LANG` setting, the `Accept-Language` header, then the default. The
resolved locale is held in a `ContextVar` for the duration of the request, so
Jinja's `_()` global and the Python-side display helpers agree without a
`locale` argument threaded through every call.
"""
from contextvars import ContextVar
from typing import Dict, Optional

DEFAULT_LOCALE = "fr"

# Locales with a catalogue. A locale that is listed here but only partly filled
# still works: see the fallback rules above.
LOCALES = ("fr", "en")

# The cookie a language switch would set, and the query parameter that overrides
# everything for one request (useful to check a translation without a setting).
LANG_COOKIE = "tb-lang"
LANG_PARAM = "lang"


# --- Catalogues --------------------------------------------------------------
#
# Keys name the string's role, grouped by where it appears. Keep them sorted
# within a group so a missing entry is easy to spot by eye.

CATALOGS: Dict[str, Dict[str, str]] = {
    "fr": {
        # Chrome: sidebar, topbar, the controls every page carries.
        "a11y.close": "Fermer",
        "a11y.menu": "Menu",
        "a11y.navigation": "Navigation",
        "nav.home": "Accueil",
        "nav.imports": "Imports",
        "nav.platforms": "Plateformes",
        "nav.search": "Recherche",
        "nav.soon": "Bientôt",
        "nav.soon_title": "L'import {platform} arrive bientôt",
        "nav.tools": "Outils",
        "privacy.toggle": "Masquer les données (partage d'écran)",
        "search.label": "Rechercher",
        "search.placeholder": "Rechercher partout…",
        "theme.toggle": "Basculer thème clair / sombre",

        # A platform's dashboard tabs, shown by app/platforms.py.
        "view.activity": "Activité",
        "view.career": "Parcours",
        "view.conversations": "Conversations",
        "view.dishes": "Plats",
        "view.formulas": "Formules",
        "view.library": "Bibliothèque",
        "view.messaging": "Messagerie",
        "view.network": "Réseau",
        "view.overview": "Vue d'ensemble",
        "view.posts": "Publications",
        "view.purchases": "Achats",
        "view.reading": "Lecture & écoute",
        "view.usage": "Usages",

        # Where to ask a platform for your export, shown on its page.
        "export.amazon": "Compte › Paramètres de confidentialité › Demander vos données",
        "export.facebook": "Espace comptes › Télécharger vos informations (format JSON)",
        "export.instagram": "Espace comptes › Télécharger vos informations (format JSON)",
        "export.linkedin": "Préférences › Confidentialité des données › Obtenir une copie de vos données",
        "export.seazon": "Mon compte › Mes données personnelles › Demander mes données (RGPD)",
        "export.zalando": "Mon compte › Mes données › Télécharger mes données",
    },
    "en": {
        "a11y.close": "Close",
        "a11y.menu": "Menu",
        "a11y.navigation": "Navigation",
        "nav.home": "Home",
        "nav.imports": "Imports",
        "nav.platforms": "Platforms",
        "nav.search": "Search",
        "nav.soon": "Soon",
        "nav.soon_title": "Importing {platform} is coming soon",
        "nav.tools": "Tools",
        "privacy.toggle": "Blur the data (screen sharing)",
        "search.label": "Search",
        "search.placeholder": "Search everywhere…",
        "theme.toggle": "Switch light / dark theme",

        "view.activity": "Activity",
        "view.career": "Career",
        "view.conversations": "Conversations",
        "view.dishes": "Dishes",
        "view.formulas": "Formulas",
        "view.library": "Library",
        "view.messaging": "Messaging",
        "view.network": "Network",
        "view.overview": "Overview",
        "view.posts": "Posts",
        "view.purchases": "Purchases",
        "view.reading": "Reading & listening",
        "view.usage": "Usage",

        "export.amazon": "Account › Privacy settings › Request your data",
        "export.facebook": "Accounts Center › Download your information (JSON format)",
        "export.instagram": "Accounts Center › Download your information (JSON format)",
        "export.linkedin": "Settings › Data privacy › Get a copy of your data",
        "export.seazon": "My account › My personal data › Request my data (GDPR)",
        "export.zalando": "My account › My data › Download my data",
    },
}

# Item types whose display name is a French phrase rather than a mechanical
# humanisation of the identifier. `app/importers/*.py` keeps its `type_labels`
# as the source of truth for the default locale; this overrides them when a
# request asks for another language, so the importers need no change to gain a
# translation. A type absent here falls back to the importer's own label, then
# to `humanize(item_type)` -- the fallback chain the UI already had.
TYPE_LABELS: Dict[str, Dict[str, str]] = {
    "en": {
        # Google Takeout activity families and actions.
        "app_used": "App opened",
        "search": "Search",
        "video_watched": "Video watched",
        "directions": "Directions",
        "results_page": "Search results page",
        "place_viewed": "Place viewed",
        "assistant": "Assistant request",
        "third_party": "Third-party tracking",
        # Amazon / Kindle.
        "reading_session": "Reading session",
        "book_finished": "Book finished",
        "highlight": "Highlight",
        "note": "Note",
        "bookmark": "Bookmark",
        "personal_document": "Personal document",
        "achievement": "Reading badge",
        # Seazon.
        "seazon_order": "Order",
        "order_cancelled": "Cancelled slot",
        "seazon_refund": "Refund",
        "meal": "Dish",
        # Zalando.
        "zalando_order": "Ordered item",
        "zalando_return": "Returned item",
        "wishlist_item": "Wishlist item",
        "newsletter_preference": "Newsletter subscription",
        "size_fit": "Body measurements",
        # LinkedIn.
        "position": "Position",
        "education": "Education",
        "volunteering": "Volunteering",
        "skill": "Skill",
        "endorsement": "Endorsement",
        "recommendation": "Recommendation",
        "certification": "Certification",
        "course": "Course",
        "invitation": "Invitation",
        "connection": "Connection",
        "follower": "Follower",
        "following": "Following",
        "login": "Login",
        "share": "Share",
        "comment": "Comment",
        "reaction": "Reaction",
        "vote": "Vote",
    },
}

_current_locale: ContextVar[Optional[str]] = ContextVar("takeback_locale", default=None)


def _canonical(locale: Optional[str]) -> Optional[str]:
    """'en-GB' / 'EN' -> 'en', if that is a locale we have a catalogue for."""
    if not locale:
        return None
    base = locale.replace("_", "-").split("-")[0].strip().lower()
    return base if base in LOCALES else None


def negotiate(accept_language: Optional[str] = None, explicit: Optional[str] = None,
              default: Optional[str] = None) -> str:
    """The locale to answer in: an explicit choice, else the header, else the
    default. An `Accept-Language` list is honoured in its own order of
    preference, skipping entries with no catalogue."""
    chosen = _canonical(explicit)
    if chosen:
        return chosen
    for part in (accept_language or "").split(","):
        # "fr-CH, fr;q=0.9, en;q=0.8" -> take the tag and its quality. The
        # header is already ordered by quality, so the order is what decides;
        # the *value* only matters at zero, where it means "not acceptable" --
        # which has to be parsed as a number, since "q=0.9" also contains "q=0".
        tag, _, params = part.strip().partition(";")
        quality = 1.0
        for param in params.split(";"):
            name, _, value = param.strip().partition("=")
            if name == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 1.0
        if quality <= 0:
            continue
        chosen = _canonical(tag)
        if chosen:
            return chosen
    return _canonical(default) or DEFAULT_LOCALE


def set_locale(locale: Optional[str]) -> None:
    """Pin the locale for the current context (a request, or a test)."""
    _current_locale.set(_canonical(locale) or DEFAULT_LOCALE)


def get_locale() -> str:
    return _current_locale.get() or DEFAULT_LOCALE


def translate(key: str, locale: Optional[str] = None, **params) -> str:
    """The message for `key`, with `{name}` placeholders filled from `params`.

    Falls back to the default locale, then to the key itself -- see the module
    docstring for why a missing translation is shown rather than hidden.
    """
    locale = _canonical(locale) or get_locale()
    text = CATALOGS.get(locale, {}).get(key)
    if text is None and locale != DEFAULT_LOCALE:
        text = CATALOGS.get(DEFAULT_LOCALE, {}).get(key)
    if text is None:
        return key
    if params:
        try:
            return text.format(**params)
        except (KeyError, IndexError):
            # A message whose placeholders no longer match is a bug worth
            # seeing, not an exception worth 500-ing a page over.
            return text
    return text


def type_label(item_type: str, locale: Optional[str] = None) -> Optional[str]:
    """The translated display name for an item type, if the catalogue has one."""
    locale = _canonical(locale) or get_locale()
    return TYPE_LABELS.get(locale, {}).get(item_type)
