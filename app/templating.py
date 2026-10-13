"""The shared Jinja2 environment: filters (French number/money formatting),
and globals every page relies on (sidebar platform list, query-string
builder)."""
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode

from fastapi.templating import Jinja2Templates

from app import crud, i18n, importers, platforms, stats
from app.__version__ import __version__
from app.database import SessionLocal
from app.importers.meta_common import humanize

templates = Jinja2Templates(directory="templates")

_NARROW_NBSP = "\u202f"
_CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "CHF": "CHF", "CAD": "$ CA"}


def display_label(identifier: Optional[str]) -> str:
    """'ads_viewed' -> 'Ads viewed', but a label that is already written for
    humans ('Fire TV', 'Commandes numériques') is kept as-is rather than
    lower-cased by humanize(), and an item type an importer names
    ('app_used' -> 'Application ouverte') takes that name."""
    if not identifier:
        return ""
    # A translation for the current locale wins; the importers keep their own
    # labels as the default-locale source of truth, so a locale that translates
    # nothing behaves exactly as before.
    named = i18n.type_label(identifier) or importers.type_label(identifier)
    if named:
        return named
    if identifier != identifier.lower() or (" " in identifier and "_" not in identifier):
        return identifier
    return humanize(identifier)


def number(value, decimals: int = 0) -> str:
    """12345.6 -> "12 345,6" (French grouping and decimal comma)."""
    if value is None:
        return ""
    text = f"{value:,.{decimals}f}"
    return text.replace(",", _NARROW_NBSP).replace(".", ",")


def money(value, currency: Optional[str] = None, decimals: int = 2) -> str:
    """1234.5 -> "1 234,50 €" when the currency is known, "1 234,50"
    otherwise."""
    if value is None:
        return ""
    text = number(value, decimals)
    if currency:
        text += " " + _CURRENCY_SYMBOLS.get(currency, currency)
    return text


def duration(minutes) -> str:
    """75 -> "1 h 15", 40 -> "40 min" -- as the charts write durations."""
    if minutes is None:
        return ""
    hours, rest = divmod(round(minutes), 60)
    if not hours:
        return f"{rest} min"
    return f"{number(hours)} h {rest:02d}" if rest else f"{number(hours)} h"


def date_fr(value: Optional[datetime], with_time: bool = False) -> str:
    if not value:
        return ""
    return value.strftime("%d/%m/%Y %H:%M" if with_time else "%d/%m/%Y")


def period(first: Optional[datetime], last: Optional[datetime]) -> str:
    """"2016 → 2025", or a single year."""
    if not first or not last:
        return "—"
    if first.year == last.year:
        return str(first.year)
    return f"{first.year} → {last.year}"


def url_with(request, _path: Optional[str] = None, **overrides) -> str:
    """The current URL with some query parameters replaced (None/"" drops
    one, a list repeats it) -- keeps every other active filter when a
    preset, a sort header or a page link changes just one of them."""
    params = [(k, v) for k, v in request.query_params.multi_items() if k not in overrides]
    for key, value in overrides.items():
        if value is None or value == "":
            continue
        if isinstance(value, (list, tuple)):
            params.extend((key, str(v)) for v in value if v not in (None, ""))
        else:
            params.append((key, str(value)))
    path = _path or request.url.path
    return path + ("?" + urlencode(params) if params else "")


def nav_platforms():
    """Sidebar entries: every supported platform, flagged with whether it
    has any imported data yet."""
    with SessionLocal() as db:
        with_data = set(crud.get_sources_with_data(db))
    entries = [(p, p.key in with_data) for p in platforms.supported()]
    return sorted(entries, key=lambda e: not e[1])


def view_label(view) -> str:
    """A dashboard tab's name in the request's language (app/i18n.py), falling
    back to the label the View carries."""
    return i18n.translate(view.label_key) if view.label_key else view.label


def export_hint(platform) -> str:
    """Where to ask a platform for your export, in the request's language."""
    return i18n.translate(platform.export_hint_key) if platform.export_hint_key else platform.export_hint


templates.env.globals.update(
    app_version=__version__,
    platform=platforms.get,
    nav_platforms=nav_platforms,
    planned_platforms=platforms.planned,
    url_with=url_with,
    period=period,
    # The item type a platform's own order lines use, for the "what was
    # ordered" link a return/refund row carries.
    purchase_line_type=stats.purchase_line_type,
    # Translations: `_` is the message lookup, `locale` the language the page is
    # being rendered in (templates set <html lang> from it).
    _=i18n.translate,
    locale=i18n.get_locale,
    view_label=view_label,
    export_hint=export_hint,
)
templates.env.filters.update(
    humanize=display_label,
    num=number,
    money=money,
    duration=duration,
    date_fr=date_fr,
)
