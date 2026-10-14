"""Display metadata for the supported platforms, and which analysis views a
platform's data supports.

The import modules (app/importers/) know how to READ an export; this is how
the UI presents the resulting platform: its name, icon, brand accent (used
for page chrome only, never as a chart series color), where to request the
export, and which dashboards make sense given the item types it holds.
"""
from dataclasses import dataclass
from typing import Iterable, List

from app import importers
from app.stats import (
    CAREER_ITEM_TYPES, DISH_VIEW_TYPES, FORMULA_VIEW_TYPES, LIBRARY_VIEW_TYPES, NETWORK_ITEM_TYPES,
    PURCHASE_LINE_ITEM_TYPES, READING_ITEM_TYPES, USAGE_VIEW_TYPES,
)


@dataclass(frozen=True)
class Platform:
    key: str
    label: str
    icon: str
    color: str
    export_hint: str = ""
    # A translation key for `export_hint` (app/i18n.py). A platform's label is
    # its own name -- "Amazon" in every language -- so only the hint, which is
    # a sentence, needs one.
    export_hint_key: str = ""


_PLATFORMS = {
    "amazon": Platform(
        "amazon", "Amazon", "bi-amazon", "#ff9900",
        "Compte › Paramètres de confidentialité › Demander vos données",
        "export.amazon",
    ),
    "facebook": Platform(
        "facebook", "Facebook", "bi-facebook", "#1877f2",
        "Espace comptes › Télécharger vos informations (format JSON)",
        "export.facebook",
    ),
    "instagram": Platform(
        "instagram", "Instagram", "bi-instagram", "#e1306c",
        "Espace comptes › Télécharger vos informations (format JSON)",
        "export.instagram",
    ),
    "google": Platform("google", "Google", "bi-google", "#4285f4", "takeout.google.com"),
    "linkedin": Platform(
        "linkedin", "LinkedIn", "bi-linkedin", "#0a66c2",
        "Préférences › Confidentialité des données › Obtenir une copie de vos données",
        "export.linkedin",
    ),
    "seazon": Platform(
        "seazon", "Seazon", "bi-basket", "#00b388",
        "Mon compte › Mes données personnelles › Demander mes données (RGPD)",
        "export.seazon",
    ),
    "zalando": Platform(
        "zalando", "Zalando", "bi-shop", "#ff6900",
        "Mon compte › Mes données › Télécharger mes données",
        "export.zalando",
    ),
}

# Shown greyed out, as coming next, until an import module reads their export.
_PLANNED: tuple = ()


def get(key: str) -> Platform:
    return _PLATFORMS.get(key) or Platform(key, (key or "?").capitalize(), "bi-box-seam", "#6c757d")


def is_supported(key: str) -> bool:
    return importers.get(key) is not None


def supported() -> List[Platform]:
    """Every platform an import module exists for, in registration order."""
    return [get(importer.key) for importer in importers.all_importers()]


def planned() -> List[Platform]:
    """Platforms announced but not importable yet."""
    return [get(key) for key in _PLANNED if not is_supported(key)]


@dataclass(frozen=True)
class View:
    key: str
    label: str
    icon: str
    path: str
    # The translation key for `label` (app/i18n.py); the UI renders it through
    # the `view_label` helper rather than reading `label` directly, so a tab
    # name can differ by language. `label` stays the default-locale string so a
    # caller with no request (a test, a script) still gets something readable.
    label_key: str = ""


OVERVIEW = View("overview", "Vue d'ensemble", "bi-grid-1x2", "", "view.overview")
PURCHASES = View("purchases", "Achats", "bi-bag", "/purchases", "view.purchases")
# Seazon's own two: a box there is a formula (so many plates) paid as one
# amount, not a basket, and the dishes repeat week after week -- neither is
# something the Amazon-shaped Achats tab can show (see app/stats.py's Seazon
# section).
FORMULAS = View("formulas", "Formules", "bi-box2", "/formulas", "view.formulas")
DISHES = View("dishes", "Plats", "bi-egg-fried", "/dishes", "view.dishes")
READING = View("reading", "Lecture & écoute", "bi-book", "/reading", "view.reading")
LIBRARY = View("library", "Bibliothèque", "bi-bookshelf", "/library", "view.library")
USAGE = View("usage", "Usages", "bi-compass", "/usage", "view.usage")
ACTIVITY = View("activity", "Activité", "bi-activity", "/activity", "view.activity")
POSTS = View("posts", "Publications", "bi-images", "/posts", "view.posts")
MESSAGING = View("messaging", "Messagerie", "bi-chat-square-text", "/messaging", "view.messaging")
CONVERSATIONS = View("conversations", "Conversations", "bi-chat-dots", "/conversations", "view.conversations")
NETWORK = View("network", "Réseau", "bi-people", "/network", "view.network")
CAREER = View("career", "Parcours", "bi-briefcase", "/career", "view.career")


def available_views(item_types: Iterable[str]) -> List[View]:
    """The tabs a platform page shows, driven by the data actually imported
    rather than by platform name -- an Amazon export without any Kindle or
    Audible session gets no "Lecture" tab."""
    types = set(item_types)
    views = [OVERVIEW]
    if types & set(PURCHASE_LINE_ITEM_TYPES):
        views.append(PURCHASES)
    if types & set(FORMULA_VIEW_TYPES):
        views.append(FORMULAS)
    if types & set(DISH_VIEW_TYPES):
        views.append(DISHES)
    if types & set(READING_ITEM_TYPES):
        views.append(READING)
    if types & set(LIBRARY_VIEW_TYPES):
        views.append(LIBRARY)
    if types & set(USAGE_VIEW_TYPES):
        views.append(USAGE)
    if types:
        views.append(ACTIVITY)
    if "post" in types:
        views.append(POSTS)
    if "message" in types:
        views.extend((MESSAGING, CONVERSATIONS))
    if types & set(NETWORK_ITEM_TYPES):
        views.append(NETWORK)
    if types & set(CAREER_ITEM_TYPES):
        views.append(CAREER)
    return views
