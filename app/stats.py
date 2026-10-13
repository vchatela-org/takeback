"""Statistics behind the platform dashboards: bucketed time series, a
weekday/hour heatmap, an approximate "time spent" estimate, and the
reading/purchase/messaging/network/posts aggregates.

All derived in Python from flat rows fetched once from the DB, rather than a
granularity-specific SQL query per view: bucketing "by week" in particular is
awkward to express portably in SQL (SQLite's strftime has no ISO-week mode
matching Python's), and a platform's dated-item count is small enough (tens
of thousands, rarely more) to just process in Python once per page load.

Every time series comes with `ranges` (the exact [start, end] of each bucket)
so a click on a bar can open the items behind it, and with a stable `slot`
per series: chart colors follow the entity (a department, a category) by its
rank over the platform's WHOLE history, so narrowing the period never
repaints a series. Past the first TOP_SERIES entities, the tail folds into
one "Autres" series rather than a 9th generated color.
"""
import json
import re
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from app.importers import google_takeout as google
from app.importers.meta_common import humanize

Event = Tuple[datetime, Optional[str]]
# (id, timestamp, item_type, context, content, fields_json) -- the shape
# crud.get_dashboard_rows returns, consumed by the reading/purchase
# dashboards below.
DashboardRow = Tuple[int, Optional[datetime], str, Optional[str], Optional[str], Optional[str]]

GRANULARITIES = ("hour", "day", "week", "month", "year")
GRANULARITY_LABELS = {
    "day": "Par jour", "week": "Par semaine", "month": "Par mois", "year": "Par an", "hour": "Par heure",
}

READING_ITEM_TYPES = ["reading_session", "listening_session"]
KINDLE_FINISHED_TYPE = "kindle_finished"
KINDLE_ANNOTATION_TYPES = ["kindle_highlight", "kindle_note", "kindle_bookmark"]
LIBRARY_ITEM_TYPES = ["kindle_book", "kindle_document"]
# Any of these shows the library tab; with the sessions, what a book of
# the library is made of.
LIBRARY_VIEW_TYPES = LIBRARY_ITEM_TYPES + [KINDLE_FINISHED_TYPE] + KINDLE_ANNOTATION_TYPES
LIBRARY_ROW_TYPES = ["reading_session"] + LIBRARY_VIEW_TYPES
# Seazon's meal orders are a purchase line type of their own rather than
# Amazon's shared `order_item`: they hold none of what an Order History row
# is made of (no department, no ASIN, no product condition), and keeping them
# apart is what lets the Seazon purchases page offer its own dimensions and
# its own wording instead of Amazon's (see _PURCHASE_DIMENSIONS_BY_SOURCE).
SEAZON_ORDER_TYPE = "seazon_order"
# Money given back on a Seazon order, its own return type too: the purchases
# page subtracts it from the spend, so what it calls "net" is the figure
# Seazon's own profile states.
SEAZON_REFUND_TYPE = "seazon_refund"
PURCHASE_LINE_ITEM_TYPES = ["order_item", "digital_order", "audible_order", SEAZON_ORDER_TYPE]
RETURN_ITEM_TYPES = ["return", "return_status", SEAZON_REFUND_TYPE]
PURCHASE_ITEM_TYPES = PURCHASE_LINE_ITEM_TYPES + RETURN_ITEM_TYPES
# What gates Seazon's own two tabs: a box (the formula it was paid as) and
# the dishes that went in it. Amazon holds neither type.
FORMULA_VIEW_TYPES = [SEAZON_ORDER_TYPE]
DISH_VIEW_TYPES = ["meal"]

OTHER_LABEL = "Autres"
# Categorical palette slots used by named series; the rest fold into
# OTHER_LABEL (drawn in a neutral gray, see static/app.js).
TOP_SERIES = 7
# A gap-free time axis (empty months shown as empty, not skipped) is the
# honest one, but "by hour" over ten years would be 87k bars -- past this
# many buckets the axis only lists buckets that have data.
MAX_FILLED_BUCKETS = 1500

# Consecutive activity within this many minutes counts as the same
# "session" when estimating time spent. Facebook/Instagram/Amazon exports
# carry no real session-duration data (unlike a phone's Screen Time), so
# this is a heuristic approximation shown to the user as such -- never
# presented as a precise measurement.
SESSION_GAP_MINUTES = 15
# A session's last event still implies a little more reading/typing time
# beyond its own instant, and a lone event with nothing else nearby still
# took a moment -- both floored here rather than reporting 0.
MIN_SESSION_MINUTES = 1

WEEKDAY_NAMES_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]


def _bucket_key(dt: datetime, granularity: str) -> datetime:
    if granularity == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    if granularity == "day":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity == "week":
        day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return day - timedelta(days=day.weekday())  # Monday of that week
    if granularity == "month":
        return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if granularity == "year":
        return dt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"unknown granularity: {granularity!r}")


def _next_bucket(bucket: datetime, granularity: str) -> datetime:
    if granularity == "hour":
        return bucket + timedelta(hours=1)
    if granularity == "day":
        return bucket + timedelta(days=1)
    if granularity == "week":
        return bucket + timedelta(days=7)
    if granularity == "month":
        return bucket.replace(year=bucket.year + 1, month=1) if bucket.month == 12 else bucket.replace(month=bucket.month + 1)
    if granularity == "year":
        return bucket.replace(year=bucket.year + 1)
    raise ValueError(f"unknown granularity: {granularity!r}")


_LABEL_FORMATS = {
    "hour": "%d/%m/%Y %Hh",
    "day": "%d/%m/%Y",
    "week": "sem. du %d/%m/%Y",
    "month": "%m/%Y",
    "year": "%Y",
}


def _axis(bucket_set: Iterable[datetime], granularity: str) -> Tuple[List[datetime], Dict]:
    """Sorted buckets (gap-filled when that stays reasonable) plus the
    labels/ranges every time-series payload carries."""
    buckets = sorted(set(bucket_set))
    if len(buckets) > 1:
        filled = []
        current = buckets[0]
        while current <= buckets[-1] and len(filled) <= MAX_FILLED_BUCKETS:
            filled.append(current)
            current = _next_bucket(current, granularity)
        if len(filled) <= MAX_FILLED_BUCKETS:
            buckets = filled
    label_fmt = _LABEL_FORMATS[granularity]
    return buckets, {
        "labels": [b.strftime(label_fmt) for b in buckets],
        "ranges": [
            [b.isoformat(timespec="seconds"), (_next_bucket(b, granularity) - timedelta(seconds=1)).isoformat(timespec="seconds")]
            for b in buckets
        ],
    }


def series_slots(ranking: Iterable[str]) -> Dict[str, int]:
    """name -> palette slot for the first TOP_SERIES names of `ranking`. A
    catch-all category literally named "Autres" never takes one: it joins
    the gray "Autres" series instead of being drawn as an entity."""
    slots: Dict[str, int] = {}
    for name in ranking:
        if name == OTHER_LABEL:
            continue
        if name not in slots:
            slots[name] = len(slots)
        if len(slots) == TOP_SERIES:
            break
    return slots


def _fold(name: str, slots: Dict[str, int]) -> str:
    return name if name in slots else OTHER_LABEL


def _series_list(names: Iterable[str], slots: Dict[str, int], values, members: Dict[str, set]) -> List[Dict]:
    """[{label, slot, data, keys}] in slot order, "Autres" last. `keys` are
    the raw values a click on the series filters by: the series' own name,
    or every name folded into "Autres"."""
    ordered = sorted(set(names), key=lambda n: slots.get(n, TOP_SERIES))
    return [
        {
            "label": name,
            "slot": slots.get(name, "other"),
            "data": values(name),
            "keys": sorted(members.get(name, ())) if name == OTHER_LABEL else [name],
        }
        for name in ordered
    ]


def build_timeseries(events: List[Event], granularity: str, ranking: List[str]) -> Dict:
    """{"labels", "ranges", "series": [{label, slot, data, keys}]} -- one
    count per (bucket, category), zero-filled so every series has the same
    length as `labels` (a Chart.js stacked bar needs that)."""
    return build_count_series(((ts, category, 1) for ts, category in events), granularity, ranking)


def build_count_series(counts_in: Iterable[Tuple[datetime, Optional[str], int]], granularity: str,
                       ranking: List[str]) -> Dict:
    """build_timeseries from pre-aggregated (timestamp, category, count)
    rows -- e.g. items per year counted by SQLite."""
    slots = series_slots(ranking)
    bucket_set = set()
    counts: Dict[Tuple[datetime, str], int] = {}
    members: Dict[str, set] = {}

    for ts, category, n in counts_in:
        cat = category or "Autre"
        name = _fold(cat, slots)
        members.setdefault(name, set()).add(cat)
        bucket = _bucket_key(ts, granularity)
        bucket_set.add(bucket)
        counts[(bucket, name)] = counts.get((bucket, name), 0) + n

    buckets, axis = _axis(bucket_set, granularity)
    axis["series"] = _series_list(
        members.keys(), slots, lambda name: [counts.get((b, name), 0) for b in buckets], members,
    )
    return axis


def build_heatmap(events: List[Event]) -> List[List[int]]:
    """7 (weekday, Monday=0) x 24 (hour) grid of raw event counts, for a
    "which day/hour am I usually active" view independent of any chosen
    granularity. An event dated without a time of day (has_time_of_day) has
    no hour to go in: left out rather than piled into "0h" -- most LinkedIn
    dates are days or months only."""
    grid = [[0] * 24 for _ in range(7)]
    for ts, _ in events:
        if has_time_of_day(ts):
            grid[ts.weekday()][ts.hour] += 1
    return grid


def estimate_time_spent_minutes(events: List[Event]) -> float:
    """Sessionizes consecutive events (gap <= SESSION_GAP_MINUTES apart)
    and sums each session's span. This is the best signal available from
    timestamps alone, not a real duration measurement -- a single message
    sent and a 20-minute scrolling spree can both leave just one timestamp
    behind, so this systematically underestimates passive/reading time and
    should be presented to the user as an approximation. A date without a
    time of day says nothing about when the activity happened that day: left
    out, rather than counted as a minute at midnight."""
    timestamps = sorted(ts for ts, _ in events if has_time_of_day(ts))
    if not timestamps:
        return 0.0
    total = timedelta()
    session_start = session_end = timestamps[0]
    for ts in timestamps[1:]:
        if ts - session_end <= timedelta(minutes=SESSION_GAP_MINUTES):
            session_end = ts
        else:
            span_minutes = max((session_end - session_start).total_seconds() / 60, MIN_SESSION_MINUTES)
            total += timedelta(minutes=span_minutes)
            session_start = session_end = ts
    span_minutes = max((session_end - session_start).total_seconds() / 60, MIN_SESSION_MINUTES)
    total += timedelta(minutes=span_minutes)
    return total.total_seconds() / 60


def format_duration_minutes(total_minutes: float) -> str:
    """"3 j 4 h 12 min" style, dropping units too small to be meaningful at
    the current magnitude (no "0 j" prefix for a 2-hour estimate)."""
    total_minutes = round(total_minutes)
    days, rem = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days} j")
    if hours or days:
        parts.append(f"{hours} h")
    parts.append(f"{minutes} min")
    return " ".join(parts)


def most_active(events: List[Event], top_n: int = 3) -> Tuple[list, list]:
    """Top days-of-week and top hours by raw event count -- a simple,
    robust one-line complement to the heatmap."""
    weekday_counts = Counter(ts.weekday() for ts, _ in events)
    hour_counts = Counter(ts.hour for ts, _ in events if has_time_of_day(ts))
    return weekday_counts.most_common(top_n), hour_counts.most_common(top_n)


_LEADING_NUMBER_RE = re.compile(r"^-?\d+(?:[.,]\d+)?")


def extract_leading_number(value: Optional[str]) -> Optional[float]:
    """Pulls the numeric prefix out of a formatted field string
    ("17.10 EUR" -> 17.10, "10.0 min" -> 10.0, "12" -> 12.0) -- every
    numeric field the Amazon importer formats for display starts with the
    number, so one regex covers all of them instead of a parser per field.
    These fields are stored pre-formatted for the item-detail view with no
    separate raw copy, so a dashboard that needs to sum them parses the
    display string back rather than the importer keeping two copies of the
    same value just for this."""
    if not value:
        return None
    m = _LEADING_NUMBER_RE.match(value.strip())
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def extract_currency(value: Optional[str]) -> Optional[str]:
    """"17.10 EUR" -> "EUR"; None when the amount carries no currency code."""
    if not value:
        return None
    parts = str(value).strip().split()
    return parts[-1].upper() if len(parts) > 1 and parts[-1].isalpha() else None


def in_period(rows: List[Sequence], date_from: Optional[datetime], date_to: Optional[datetime]) -> List[Sequence]:
    """Rows within [date_from, date_to]. Undated rows only survive an
    unbounded period: they can't be placed in a chosen one. Any row shape
    whose second element is its timestamp (DashboardRow, TitledRow) can be
    cut this way, which is how the Seazon pages filter what they read."""
    if not date_from and not date_to:
        return rows
    return [
        row for row in rows
        if row[1] is not None
        and (date_from is None or row[1] >= date_from)
        and (date_to is None or row[1] <= date_to)
    ]


# ────────────────────────────────────────────────
# Reading & listening (Kindle / Audible)
# ────────────────────────────────────────────────

# item_type -> (display source label, field name holding minutes, palette slot)
_READING_SOURCES = {
    "reading_session": ("Kindle", "Durée de lecture", 0),
    "listening_session": ("Audible", "Durée d'écoute", 1),
}


# Devices past this many share the neutral "other" colour; Audible, which
# runs on no device, keeps the last slot.
READING_DEVICE_SLOTS = 7
AUDIBLE_SLOT = READING_DEVICE_SLOTS


def _fields(fields_json: Optional[str]) -> Dict:
    return json.loads(fields_json) if fields_json else {}


def reading_devices(rows: Sequence[tuple]) -> List[Dict]:
    """The Kindle devices that logged reading sessions, busiest first:
    [{value, label, count, slot}]. `value` is the device label stored on
    each item ("Appareil"); the slot is the device's colour, ranked over
    every session so it stays the same whatever the filters. Works on
    dashboard and library rows alike (item type third, fields last)."""
    counts: Dict[str, int] = {}
    for row in rows:
        if row[2] != "reading_session":
            continue
        device = _fields(row[-1]).get("Appareil")
        if device:
            counts[device] = counts.get(device, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [
        {"value": device, "label": device, "count": n, "slot": i if i < READING_DEVICE_SLOTS else "other"}
        for i, (device, n) in enumerate(ranked)
    ]


_DEVICE_ITEM_TYPES = ("reading_session", *KINDLE_ANNOTATION_TYPES)


def filter_reading_rows(rows: List[tuple], devices: List[str]) -> List[tuple]:
    """Keep what happened on these devices (no devices: everything): their
    sessions and annotations, and the books and documents (library entries,
    finished records) of the books touched on them. A listening session has
    no device and drops out, and so does a book nothing was done to on
    them. Dashboard and library rows alike."""
    if not devices:
        return rows
    wanted = set(devices)
    touched = {
        row[3] for row in rows
        if row[2] in _DEVICE_ITEM_TYPES and _fields(row[-1]).get("Appareil") in wanted
    }
    return [
        row for row in rows
        if (row[2] in _DEVICE_ITEM_TYPES and _fields(row[-1]).get("Appareil") in wanted)
        or (row[2] not in _DEVICE_ITEM_TYPES and row[2] != "listening_session" and row[3] in touched)
    ]


def reading_minutes(item_type: str, fields: Dict) -> float:
    return extract_leading_number(fields.get(_READING_SOURCES[item_type][1])) or 0.0


def build_reading_dashboard(rows: List[DashboardRow], granularity: str, devices: Optional[List[Dict]] = None) -> Dict:
    """rows: reading_session (Kindle) + listening_session (Audible) items,
    and the Kindle books finished. Sessions and minutes are bucketed and
    stacked Kindle vs. Audible; pages turned only exists for Kindle
    (audiobooks have no page count), finished books too.
    With `devices` (see reading_devices), Kindle is split into one series
    per device, each in its own colour.
    Totals cover every row regardless of whether it has a timestamp (a
    session with no date should still count toward "62 sessions total"
    even though it can't be placed on the chart)."""
    bucket_set = set()
    sessions_counts: Dict[Tuple[datetime, str], int] = {}
    minutes_totals: Dict[Tuple[datetime, str], float] = {}
    pages_totals: Dict[datetime, float] = {}
    finished_counts: Dict[datetime, int] = {}
    total_sessions = 0
    total_minutes = 0.0
    total_pages = 0.0
    total_finished = 0
    device_slots = {d["value"]: d["slot"] for d in devices or []}

    for _id, ts, item_type, _context, _content, fields_json in rows:
        if item_type == KINDLE_FINISHED_TYPE:
            total_finished += 1
            if ts is not None:
                bucket = _bucket_key(ts, granularity)
                bucket_set.add(bucket)
                finished_counts[bucket] = finished_counts.get(bucket, 0) + 1
            continue
        source_info = _READING_SOURCES.get(item_type)
        if source_info is None:
            continue
        fields = _fields(fields_json)
        source = fields.get("Appareil") if item_type == "reading_session" and device_slots else None
        source = source or source_info[0]
        minutes = reading_minutes(item_type, fields)
        pages = extract_leading_number(fields.get("Pages tournées")) if item_type == "reading_session" else None

        total_sessions += 1
        total_minutes += minutes
        if pages:
            total_pages += pages

        if ts is None:
            continue
        bucket = _bucket_key(ts, granularity)
        bucket_set.add(bucket)
        sessions_counts[(bucket, source)] = sessions_counts.get((bucket, source), 0) + 1
        minutes_totals[(bucket, source)] = minutes_totals.get((bucket, source), 0) + minutes
        if pages:
            pages_totals[bucket] = pages_totals.get(bucket, 0) + pages

    buckets, axis = _axis(bucket_set, granularity)
    # (series label, palette slot, item type, device) -- a Kindle series
    # per device when they are told apart, else one for the whole Kindle.
    sources = [("Kindle", _READING_SOURCES["reading_session"][2], "reading_session", None)]
    if device_slots:
        sources = [(d, slot, "reading_session", d) for d, slot in device_slots.items()]
        sources.append(("Kindle", "other", "reading_session", None))
    sources.append(("Audible", AUDIBLE_SLOT if device_slots else _READING_SOURCES["listening_session"][2],
                    "listening_session", None))
    present = {label for _bucket, label in sessions_counts}
    sources = [src for src in sources if src[0] in present]

    def series(values):
        return [
            {"label": label, "slot": slot, "data": values(label), "keys": [item_type], "device": device}
            for label, slot, item_type, device in sources
        ]

    axis.update({
        "sessions_series": series(lambda src: [sessions_counts.get((b, src), 0) for b in buckets]),
        "minutes_series": series(lambda src: [round(minutes_totals.get((b, src), 0), 1) for b in buckets]),
        "pages_series": [{
            "label": "Pages tournées", "slot": 0, "keys": ["reading_session"],
            "data": [round(pages_totals.get(b, 0)) for b in buckets],
        }],
        "finished_series": [{
            "label": "Livres terminés", "slot": 0, "keys": [KINDLE_FINISHED_TYPE],
            "data": [finished_counts.get(b, 0) for b in buckets],
        }],
        "total_sessions": total_sessions,
        "total_minutes": total_minutes,
        "total_pages": round(total_pages),
        "total_finished": total_finished,
    })
    return axis


def reading_days(rows: List[DashboardRow]) -> List[date]:
    """The days with any reading or listening time, in order."""
    return sorted({
        ts.date() for _id, ts, item_type, _c, _t, fields_json in rows
        if ts is not None and item_type in _READING_SOURCES
        and reading_minutes(item_type, json.loads(fields_json) if fields_json else {}) > 0
    })


def longest_streak(days: Iterable[date]) -> Optional[Dict]:
    """The longest run of consecutive days: {days, start, end}, the
    earliest of equal runs."""
    best = None
    run_start = previous = None
    for day in sorted(set(days)):
        if previous is None or day - previous != timedelta(days=1):
            run_start = day
        previous = day
        length = (day - run_start).days + 1
        if best is None or length > best["days"]:
            best = {"days": length, "start": run_start, "end": day}
    return best


def build_reading_heatmap(rows: List[DashboardRow]) -> List[List[int]]:
    """7 x 24 grid of reading/listening minutes, by the weekday and hour a
    session started -- the same rule as the panel a cell opens (sessions
    started at that hour), rather than spreading a session over the hours
    it spans."""
    grid = [[0.0] * 24 for _ in range(7)]
    for _id, ts, item_type, _c, _t, fields_json in rows:
        if ts is None or item_type not in _READING_SOURCES or not has_time_of_day(ts):
            continue
        grid[ts.weekday()][ts.hour] += reading_minutes(item_type, json.loads(fields_json) if fields_json else {})
    return [[round(minutes) for minutes in day] for day in grid]


def build_reading_titles(rows: List[DashboardRow], titles_by_asin: Dict[str, str], limit: Optional[int] = 15,
                         finished: Iterable[str] = (), devices: Optional[List[Dict]] = None) -> List[Dict]:
    """Most read/listened titles by time spent: [{context, label, source,
    item_type, minutes, sessions, finished}]. Sessions are grouped by
    `context` (the book's title, or its ASIN when the Kindle export names
    none, for Kindle; the product name for Audible); an ASIN is shown under
    the title of the matching purchase when one exists. `finished`: the
    contexts of the books finished, whenever that was. `devices` (see
    reading_devices): each title also gets `segments`, its minutes per
    device with the device's colour slot, for a bar split by device."""
    device_slots = {d["value"]: d["slot"] for d in devices or []}
    finished = set(finished)
    totals: Dict[Tuple[str, str], Dict] = {}
    for _id, _ts, item_type, context, _content, fields_json in rows:
        if item_type not in _READING_SOURCES or not context:
            continue
        fields = json.loads(fields_json) if fields_json else {}
        entry = totals.setdefault((item_type, context), {
            "context": context,
            "label": titles_by_asin.get(context, context),
            "source": _READING_SOURCES[item_type][0],
            "item_type": item_type,
            "minutes": 0.0,
            "sessions": 0,
            "finished": context in finished,
            "segments": {},
        })
        minutes = reading_minutes(item_type, fields)
        entry["minutes"] += minutes
        entry["sessions"] += 1
        if device_slots:
            device = fields.get("Appareil") if item_type == "reading_session" else None
            slot = device_slots.get(device, "other") if device else AUDIBLE_SLOT
            segment = entry["segments"].setdefault(device or entry["source"], {"slot": slot, "minutes": 0.0})
            segment["minutes"] += minutes
    ranked = sorted(totals.values(), key=lambda e: -e["minutes"])[:limit]
    for entry in ranked:
        entry["minutes"] = round(entry["minutes"], 1)
        entry["segments"] = [
            {"label": label, "slot": seg["slot"], "minutes": round(seg["minutes"], 1)}
            for label, seg in sorted(entry["segments"].items(), key=lambda kv: -kv[1]["minutes"])
        ]
    return ranked


# ────────────────────────────────────────────────
# Kindle library: every book owned, sent to the Kindle or read
# ────────────────────────────────────────────────

# (id, timestamp, item_type, context, content, author, fields_json) --
# crud.get_library_rows.
LibraryRow = Tuple[int, Optional[datetime], str, Optional[str], Optional[str], Optional[str], Optional[str]]

# Where a book comes from, in palette order (the arrivals chart colours by
# it); a book read without being in the library (a loan, a file the export
# doesn't list) has no item of its own: "other".
LIBRARY_KINDS = {"kindle_book": "Livres Kindle", "kindle_document": "Documents personnels", "other": "Hors bibliothèque"}
# status -> (label, palette slot): hues of their own, so that on the same
# page no colour stands for both a kind of book and a status; the books
# never opened in neutral gray.
LIBRARY_STATUSES = {
    "finished": ("Terminés", 2),
    "started": ("Commencés", 3),
    "unopened": ("Jamais ouverts", "other"),
}


def _earliest(a: Optional[datetime], b: Optional[datetime]) -> Optional[datetime]:
    return b if a is None else a if b is None else min(a, b)


def _latest(a: Optional[datetime], b: Optional[datetime]) -> Optional[datetime]:
    return b if a is None else a if b is None else max(a, b)


def build_library(rows: List[LibraryRow], titles_by_asin: Dict[str, str]) -> List[Dict]:
    """One entry per book -- the `context` all its Kindle items share --
    with where it comes from, when it arrived, the reading time and
    sessions, when it was first/last read and finished, its annotations
    and a status: finished (a "finished" record), started (any session or
    annotation), or never opened (in the library, nothing else). A book
    named by its ASIN alone shows under the title of its purchase."""
    books: Dict[str, Dict] = {}
    for _id, ts, item_type, context, _content, author, fields_json in rows:
        if not context or item_type not in LIBRARY_ROW_TYPES:
            continue
        book = books.setdefault(context, {
            "context": context, "title": titles_by_asin.get(context, context), "author": None, "kind": "other",
            "added": None, "minutes": 0.0, "sessions": 0, "first_read": None, "last_read": None,
            "is_finished": False, "finished": None, "annotations": 0, "last_activity": None,
        })
        book["author"] = book["author"] or author
        if item_type in LIBRARY_ITEM_TYPES:
            # A document sent for a book also bought is still the book.
            if book["kind"] != "kindle_book":
                book["kind"] = item_type
            book["added"] = _earliest(book["added"], ts)
        elif item_type == "reading_session":
            book["minutes"] += reading_minutes(item_type, json.loads(fields_json) if fields_json else {})
            book["sessions"] += 1
            book["first_read"] = _earliest(book["first_read"], ts)
            book["last_read"] = _latest(book["last_read"], ts)
        elif item_type == KINDLE_FINISHED_TYPE:
            book["is_finished"] = True
            book["finished"] = _latest(book["finished"], ts)
        else:
            book["annotations"] += 1
        book["last_activity"] = _latest(book["last_activity"], ts)

    for book in books.values():
        if book["is_finished"]:
            book["status"] = "finished"
        elif book["sessions"] or book["annotations"]:
            book["status"] = "started"
        else:
            book["status"] = "unopened"
        book["kind_label"] = LIBRARY_KINDS[book["kind"]]
        book["minutes"] = round(book["minutes"], 1)
    return list(books.values())


def library_breakdown(books: List[Dict]) -> Dict:
    """Horizontal stacked bars: one per kind of book, split by status."""
    kinds = [kind for kind in LIBRARY_KINDS if any(b["kind"] == kind for b in books)]
    series = []
    for status, (label, slot) in LIBRARY_STATUSES.items():
        data = [sum(1 for b in books if b["kind"] == kind and b["status"] == status) for kind in kinds]
        if any(data):
            series.append({"label": label, "slot": slot, "data": data, "keys": [status]})
    return {"labels": [LIBRARY_KINDS[k] for k in kinds], "kinds": kinds, "series": series}


def build_library_arrivals(rows: List[LibraryRow], granularity: str) -> Dict:
    """Books and documents entering the library over time, stacked by kind."""
    arrivals = build_count_series(
        ((ts, item_type, 1) for _id, ts, item_type, *_rest in rows if item_type in LIBRARY_ITEM_TYPES and ts),
        granularity, LIBRARY_ITEM_TYPES,
    )
    for series in arrivals["series"]:
        series["label"] = LIBRARY_KINDS.get(series["keys"][0], series["label"])
    return arrivals


# ────────────────────────────────────────────────
# Purchases & returns (Amazon)
# ────────────────────────────────────────────────

# item_type -> dashboard category
_PURCHASE_TYPES = {
    "order_item": "Commandes",
    "digital_order": "Commandes numériques",
    "audible_order": "Commandes numériques",
    SEAZON_ORDER_TYPE: "Commandes de repas",
}
_RETURN_AMOUNT_FIELD = {"return": "Montant remboursé", "return_status": "Montant", SEAZON_REFUND_TYPE: "Montant remboursé"}

# Only "order_item" (retail) carries a real product department (Order
# History's "Department" column, stored under fields["Département"] by the
# importer) -- Digital Content Orders/Audible Purchase History have no
# equivalent column, so those are grouped under one label rather than
# guessed at. "Numérique" is deliberately just another department value
# here, not a special exclusion -- digital purchases genuinely have no
# finer-grained department of their own, so lumping them under one bucket
# alongside "Electronics"/"Books"/etc. is the accurate answer, not a
# workaround.
_DIGITAL_CATEGORY_LABEL = "Numérique"
# A Seazon order has no department either: its importer labels what the order
# is ("Repas") under its own field name.
_SEAZON_CATEGORY_FIELD = "Catégorie"

# Dimensions purchases can be split by -- UI key -> the label the purchase
# list and the charts show for it. "none" and "category" are handled
# separately in purchase_group ("none" collapses everything into one bucket
# regardless of field content; "category" folds digital purchases into
# "Numérique" rather than "Non renseigné" -- we DO know what those are even
# without a Department column). Every other dimension only exists for retail
# purchases (Order History's Shipping/Billing Address, Payment Method Type,
# Website, Product Condition columns -- Digital Content Orders/Audible
# Purchase History have no equivalents), so a digital purchase falls under
# "Non renseigné" for those instead of being dropped.
#
# The dimension key doubles as the URL/dict key (kept stable), and the label
# is what the UI shows; _PURCHASE_GROUP_FIELDS separately says which stored
# field a dimension reads -- the two used to be one dict, which only worked
# while every label happened to be spelled exactly like its field name
# ("category" goes by a different name per platform, see below).
# "category", "formula" and "none" are resolved by purchase_category /
# formula_name / purchase_group instead of reading one field verbatim.
_PURCHASE_GROUP_FIELDS = {
    "shipping_address": "Adresse de livraison",
    "billing_address": "Adresse de facturation",
    "payment_method": "Mode de paiement",
    "status": "Statut",
    "website": "Site",
    "product_condition": "État du produit",
}

PURCHASE_GROUP_DIMENSIONS = {
    "category": "Département",
    "none": "Aucun (total)",
    **_PURCHASE_GROUP_FIELDS,
}

# Dimensions only one platform offers, and what its page calls them. Kept
# out of PURCHASE_GROUP_DIMENSIONS on purpose: a dimension belongs to the
# platform whose data can fill it, and offering "Par formule" to Amazon would
# only ever answer "Non renseigné".
_PURCHASE_EXTRA_DIMENSIONS_BY_SOURCE = {
    "seazon": {"formula": "Formule"},
}

# What one platform's purchase page offers, and what it calls the category
# dimension: Amazon's Order History carries a real Department column plus two
# addresses, a payment method, a website and a product condition, while a
# Seazon order carries the formula it was paid as, the address it was shipped
# to, a payment provider and a status -- offering Seazon the Amazon dimensions
# would only ever fill them with "Non renseigné", and its own "Catégorie" is
# the single word "Repas" for every order it ever placed (the Formula tab is
# where a Seazon order's own shape is read, see build_formula_dashboard). A
# source absent from these tables gets the full Amazon-shaped set, unchanged.
_PURCHASE_DIMENSIONS_BY_SOURCE = {
    "seazon": ("formula", "none", "shipping_address", "payment_method", "status"),
}
_PURCHASE_CATEGORY_LABELS_BY_SOURCE = {"seazon": "Catégorie"}
# The item type a platform's own order lines use, for the one link that has
# to name it: a return's "what was ordered" column (templates/browse.html).
_PURCHASE_LINE_TYPE_BY_SOURCE = {"seazon": SEAZON_ORDER_TYPE}

_NO_GROUPING_LABEL = "Total"
_UNSET_LABEL = "Non renseigné"

# -- Seazon: the formula, and the dishes that went in it ---------------------
#
# A Seazon box is not a basket of products but N plates of a chosen formula
# ("10 plats", "14 plats"), charged as one amount for the whole box: that is
# what the export's `count` states, and what no Amazon dimension describes.
# Its dishes are items of their own (`meal`), each knowing which dish, how
# many portions of it, and whether the box ever arrived.

SEAZON_PLATES_FIELD = "Nombre de repas"      # seazon_order: plates in the box
SEAZON_PORTIONS_FIELD = "Quantité"           # meal: portions of that dish
SEAZON_MEAL_STATE_FIELD = "Commande"         # meal: the state of its box
# The importer's _ORDER_STATES["shipped"]: the box arrived, so its plates were
# eaten. A "Payée" box is charged but still to come, an "Annulée" one was
# never delivered -- neither is something you have eaten.
SEAZON_DELIVERED_LABEL = "Livrée"


def seazon_plate_count(fields: Dict) -> Optional[int]:
    """How many plates the box holds, or None when the export didn't state it."""
    try:
        plates = int(fields.get(SEAZON_PLATES_FIELD))
    except (TypeError, ValueError):
        return None
    return plates if plates > 0 else None


def formula_name(fields: Dict) -> str:
    """"10 plats" -- the group a box falls under in every formula view, from
    the Achats page's dimension to the Formules tab, so the two agree."""
    plates = seazon_plate_count(fields)
    return f"{plates} plats" if plates else _UNSET_LABEL


def seazon_portions(fields: Dict) -> int:
    """How many portions of one dish a box held: a dish picked three times in
    one box was eaten three times. The importer writes the field only above 1,
    so an absent one means one."""
    try:
        portions = int(fields.get(SEAZON_PORTIONS_FIELD))
    except (TypeError, ValueError):
        return 1
    return portions if portions > 0 else 1


def seazon_delivered(fields: Dict) -> bool:
    """Whether the box holding this dish was delivered -- only those dishes
    were actually eaten (see SEAZON_DELIVERED_LABEL)."""
    return fields.get(SEAZON_MEAL_STATE_FIELD) == SEAZON_DELIVERED_LABEL


def purchase_group_dimensions(source: Optional[str] = None) -> Dict[str, str]:
    """The dimensions one platform's purchase page offers, in display order,
    under that platform's own wording."""
    dimensions = {
        **PURCHASE_GROUP_DIMENSIONS,
        **_PURCHASE_EXTRA_DIMENSIONS_BY_SOURCE.get(source or "", {}),
    }
    if source in _PURCHASE_CATEGORY_LABELS_BY_SOURCE:
        dimensions["category"] = _PURCHASE_CATEGORY_LABELS_BY_SOURCE[source]
    keys = _PURCHASE_DIMENSIONS_BY_SOURCE.get(source)
    return {key: dimensions[key] for key in keys} if keys else dimensions


def purchase_line_type(source: Optional[str] = None) -> str:
    """The item type this platform's purchase lines use."""
    return _PURCHASE_LINE_TYPE_BY_SOURCE.get(source or "", "order_item")


def purchase_category(item_type: str, fields: Dict) -> str:
    if item_type == "order_item":
        return fields.get("Département") or "Sans département"
    if item_type == SEAZON_ORDER_TYPE:
        return fields.get(_SEAZON_CATEGORY_FIELD) or "Sans catégorie"
    return _DIGITAL_CATEGORY_LABEL


def purchase_group(item_type: str, fields: Dict, dimension: str) -> str:
    """The single dispatch point every purchase view uses to resolve which
    group a purchase line falls under for a given dimension -- kept as one
    function specifically so the charts, the drill-down panel and the list
    can never silently disagree about what a dimension means."""
    if dimension == "none":
        return _NO_GROUPING_LABEL
    if dimension == "category":
        return purchase_category(item_type, fields)
    if dimension == "formula":
        return formula_name(fields)
    field_name = _PURCHASE_GROUP_FIELDS.get(dimension)
    return (fields.get(field_name) or _UNSET_LABEL) if field_name else _UNSET_LABEL


def purchase_amount(fields: Dict) -> float:
    return extract_leading_number(fields.get("Montant")) or 0.0


def detect_currency(rows: List[DashboardRow]) -> Optional[str]:
    """The one currency every purchase amount is in, or None when they mix
    several (or carry none) -- totals are then shown as plain numbers, with
    the existing caveat that currencies aren't converted."""
    currencies = set()
    for _id, _ts, item_type, _context, _content, fields_json in rows:
        if item_type not in _PURCHASE_TYPES or not fields_json:
            continue
        code = extract_currency(json.loads(fields_json).get("Montant"))
        if code:
            currencies.add(code)
    return currencies.pop() if len(currencies) == 1 else None


def purchase_group_ranking(rows: List[DashboardRow], dimension: str) -> List[str]:
    """Groups of a dimension by total spend, highest first, over the rows
    given (the whole history, for stable chart colors)."""
    totals: Dict[str, float] = {}
    for _id, _ts, item_type, _context, _content, fields_json in rows:
        if item_type not in _PURCHASE_TYPES:
            continue
        fields = json.loads(fields_json) if fields_json else {}
        group = purchase_group(item_type, fields, dimension)
        totals[group] = totals.get(group, 0) + purchase_amount(fields)
    return [g for g, _ in sorted(totals.items(), key=lambda kv: -kv[1])]


def build_purchase_dashboard(rows: List[DashboardRow], granularity: str, group_by: str, ranking: List[str]) -> Dict:
    """rows: order_item/digital_order/audible_order/seazon_order (purchases)
    and return/return_status/seazon_refund (returns) items. Order/return counts are
    DISTINCT `context` (the Order ID) per bucket, not row counts -- an
    order spans one row per item, so counting rows would inflate "number
    of orders" by the average items-per-order ratio. Counting distinct
    context across BOTH return item_types together also means a return
    recorded in both Refund Details.csv and Returns Status.csv for the
    same order isn't double-counted.

    Spend is stacked by whichever `group_by` dimension the user picked
    (PURCHASE_GROUP_DIMENSIONS, same set the flat build_group_breakdown
    pivots on) -- the two charts must agree on what a dimension means."""
    slots = series_slots(ranking)
    bucket_set = set()
    spend_totals: Dict[Tuple[datetime, str], float] = {}
    orders_seen: Dict[datetime, set] = {}
    returns_seen: Dict[datetime, set] = {}
    return_amount_totals: Dict[datetime, float] = {}
    members: Dict[str, set] = {}
    total_spend = 0.0
    total_refunded = 0.0
    total_lines = 0
    all_order_ids = set()
    all_return_ids = set()

    for _id, ts, item_type, context, _content, fields_json in rows:
        fields = json.loads(fields_json) if fields_json else {}

        if item_type in _PURCHASE_TYPES:
            amount = purchase_amount(fields)
            group = purchase_group(item_type, fields, group_by)
            name = _fold(group, slots)
            members.setdefault(name, set()).add(group)
            total_spend += amount
            total_lines += 1
            if context:
                all_order_ids.add(context)
        elif item_type in _RETURN_AMOUNT_FIELD:
            amount = extract_leading_number(fields.get(_RETURN_AMOUNT_FIELD[item_type])) or 0.0
            total_refunded += amount
            if context:
                all_return_ids.add(context)
        else:
            continue

        if ts is None:
            continue
        bucket = _bucket_key(ts, granularity)
        bucket_set.add(bucket)

        if item_type in _PURCHASE_TYPES:
            spend_totals[(bucket, name)] = spend_totals.get((bucket, name), 0) + amount
            if context:
                orders_seen.setdefault(bucket, set()).add(context)
        else:
            # A return has no department/address/payment method of its
            # own (those belong to the original purchase, not the return
            # record) -- it genuinely can't be attributed to a spend
            # group, so it's tracked per time bucket only, as a separate
            # "Retours" series the chart can subtract from the stack
            # rather than mis-attributing it to whichever group happens
            # to be drawn first.
            return_amount_totals[bucket] = return_amount_totals.get(bucket, 0) + amount
            if context:
                returns_seen.setdefault(bucket, set()).add(context)

    buckets, axis = _axis(bucket_set, granularity)

    # Running totals at the same buckets: what had been spent by the end of
    # each one, gross and net of refunds (the page's "Déduire" switch picks).
    cumulative, cumulative_net = [], []
    running = running_net = 0.0
    for b in buckets:
        spent = sum(spend_totals.get((b, name), 0) for name in members)
        running += spent
        running_net += spent - return_amount_totals.get(b, 0)
        cumulative.append(round(running, 2))
        cumulative_net.append(round(running_net, 2))

    axis.update({
        "spend_series": _series_list(
            members.keys(), slots, lambda name: [round(spend_totals.get((b, name), 0), 2) for b in buckets], members,
        ),
        "cumulative_series": cumulative,
        "cumulative_net_series": cumulative_net,
        # Negative on purpose: stacked directly alongside spend_series in
        # Chart.js, a negative value extends the stack downward from zero,
        # so toggling this series in/out visually adds or removes exactly
        # the refunded amount from each bucket's total height -- no
        # separate "net spend" number to keep in sync with the chart.
        "returns_amount_series": [round(-return_amount_totals.get(b, 0), 2) for b in buckets],
        "orders_series": [len(orders_seen.get(b, ())) for b in buckets],
        "returns_series": [len(returns_seen.get(b, ())) for b in buckets],
        "total_spend": round(total_spend, 2),
        "total_refunded": round(total_refunded, 2),
        "total_orders": len(all_order_ids),
        "total_lines": total_lines,
        "total_returns": len(all_return_ids),
    })
    return axis


def has_time_of_day(ts: datetime) -> bool:
    """False for a timestamp at exactly midnight: an export column holding
    a plain date (no time) parses to 00:00:00, and a real purchase landing
    on that exact second is too unlikely to be worth telling apart -- such
    rows can be placed on a weekday but not at an hour."""
    return (ts.hour, ts.minute, ts.second, ts.microsecond) != (0, 0, 0, 0)


def build_purchase_rhythm(rows: List[DashboardRow], group_by: str, ranking: List[str]) -> Dict:
    """When purchases happen, whatever the period: spend per day of the
    week (stacked by the same `group_by` dimension and colour slots as the
    spend-over-time chart) and distinct orders per weekday x hour. Hours
    are the export's (UTC for Amazon). An order whose date carries no time
    counts on its weekday but is left out of the hour grid -- piling those
    into "0h" would invent a midnight shopping habit -- and reported as
    `untimed_orders` instead."""
    slots = series_slots(ranking)
    spend: Dict[Tuple[int, str], float] = {}
    members: Dict[str, set] = {}
    grid: List[List[set]] = [[set() for _ in range(24)] for _ in range(7)]
    untimed: set = set()

    for id_, ts, item_type, context, _content, fields_json in rows:
        if item_type not in _PURCHASE_TYPES or ts is None:
            continue
        fields = json.loads(fields_json) if fields_json else {}
        group = purchase_group(item_type, fields, group_by)
        name = _fold(group, slots)
        members.setdefault(name, set()).add(group)
        weekday = ts.weekday()
        spend[(weekday, name)] = spend.get((weekday, name), 0) + purchase_amount(fields)
        order = context or f"#{id_}"
        if has_time_of_day(ts):
            grid[weekday][ts.hour].add(order)
        else:
            untimed.add(order)

    heatmap = [[len(cell) for cell in row] for row in grid]
    return {
        "labels": WEEKDAY_NAMES_FR,
        "spend_series": _series_list(
            members.keys(), slots, lambda name: [round(spend.get((d, name), 0), 2) for d in range(7)], members,
        ),
        "heatmap": heatmap,
        "heatmap_max": max((max(row) for row in heatmap), default=0),
        "untimed_orders": len(untimed),
    }


# A leap year, so every calendar day of any year (29/02 included) has a
# slot on the year-over-year axis.
_CALENDAR_YEAR = 2000
# Year-over-year emphasis: the latest year in the accent slot, the one
# before it as the comparison, older years as gray context lines.
_YEAR_SLOTS = (0, 1)


def build_yearly_cumulative(rows: List[DashboardRow], all_years: List[int],
                            date_from: Optional[datetime], date_to: Optional[datetime],
                            coverage_end: Optional[datetime]) -> Dict:
    """Spend accumulated day by day within each calendar year, every year
    on the same 01/01 -> 31/12 axis -- "by this date, am I spending more
    than last year?". Gross and net of refunds, like the other cumulative.

    A year's line only spans the days the data can speak for: it starts at
    `date_from` and stops at `date_to` or at `coverage_end` (the latest
    dated purchase or refund of the whole history, the best available proxy
    for when the export was taken) -- the latest year stops where the data
    does instead of flat-lining to 31/12. Colour slots come from `all_years`
    (the whole history, newest first), so narrowing the period never
    repaints a year."""
    start = date(_CALENDAR_YEAR, 1, 1)
    days = [start + timedelta(days=i) for i in range(366)]

    def day_index(ts: datetime) -> int:
        return (date(_CALENDAR_YEAR, ts.month, ts.day) - start).days

    spend: Dict[int, List[float]] = {}
    refunds: Dict[int, List[float]] = {}
    for _id, ts, item_type, _context, _content, fields_json in rows:
        if ts is None:
            continue
        fields = json.loads(fields_json) if fields_json else {}
        if item_type in _PURCHASE_TYPES:
            amount, target = purchase_amount(fields), spend
        elif item_type in _RETURN_AMOUNT_FIELD:
            amount, target = extract_leading_number(fields.get(_RETURN_AMOUNT_FIELD[item_type])) or 0.0, refunds
        else:
            continue
        target.setdefault(ts.year, [0.0] * 366)[day_index(ts)] += amount
        spend.setdefault(ts.year, [0.0] * 366)

    end = min((d for d in (date_to, coverage_end) if d is not None), default=None)
    slots = {year: _YEAR_SLOTS[i] for i, year in enumerate(all_years[:len(_YEAR_SLOTS)])}
    series = []
    for year in sorted(spend, reverse=True):
        first = day_index(date_from) if date_from and date_from.year == year else 0
        # 29/02 of a common year simply carries 28/02's total forward.
        last = day_index(end) if end and end.year == year else 365
        gross, net = [], []
        running = running_net = 0.0
        year_refunds = refunds.get(year, [0.0] * 366)
        for i in range(366):
            running += spend[year][i]
            running_net += spend[year][i] - year_refunds[i]
            inside = first <= i <= last
            gross.append(round(running, 2) if inside else None)
            net.append(round(running_net, 2) if inside else None)
        series.append({
            "label": str(year), "year": year, "slot": slots.get(year, "other"),
            "data": gross, "net": net, "keys": [str(year)],
        })
    return {
        "labels": [d.strftime("%d/%m") for d in days],
        "days": [d.strftime("%m-%d") for d in days],
        "series": series,
    }


def build_group_breakdown(rows: List[DashboardRow], dimension: str) -> List[Dict]:
    """Total spend per group of one dimension (not time-bucketed) --
    [{label, value, count}], highest spend first so a bar chart built
    straight from it reads top-to-bottom by rank."""
    totals: Dict[str, Dict] = {}
    for _id, _ts, item_type, _context, _content, fields_json in rows:
        if item_type not in _PURCHASE_TYPES:
            continue
        fields = json.loads(fields_json) if fields_json else {}
        group = purchase_group(item_type, fields, dimension)
        entry = totals.setdefault(group, {"label": group, "value": 0.0, "count": 0})
        entry["value"] += purchase_amount(fields)
        entry["count"] += 1
    ranked = sorted(totals.values(), key=lambda e: -e["value"])
    for entry in ranked:
        entry["value"] = round(entry["value"], 2)
    return ranked


def build_purchase_list(rows: List[DashboardRow], dimension: str = "category") -> List[Dict]:
    """Flattens purchase line items into plain dicts (id, timestamp,
    category, group, product, amount, order_id) ready for sorting/
    filtering/pagination in the route -- the aggregate dashboard never
    needs per-row detail, the purchase list is only per-row detail."""
    result = []
    for id_, ts, item_type, context, content, fields_json in rows:
        if item_type not in _PURCHASE_TYPES:
            continue
        fields = json.loads(fields_json) if fields_json else {}
        result.append({
            "id": id_,
            "timestamp": ts,
            "item_type": item_type,
            "category": purchase_category(item_type, fields),
            "group": purchase_group(item_type, fields, dimension),
            "product": content,
            "amount": extract_leading_number(fields.get("Montant")),
            "quantity": fields.get("Quantité"),
            "order_id": context,
        })
    return result


def _within(ts: Optional[datetime], date_from: Optional[datetime], date_to: Optional[datetime]) -> bool:
    """Same rule as in_period, for one timestamp."""
    if not date_from and not date_to:
        return True
    return ts is not None and (date_from is None or ts >= date_from) and (date_to is None or ts <= date_to)


# ────────────────────────────────────────────────
# Seazon: the formulas, and the dishes that went in them
# ────────────────────────────────────────────────
#
# What a Seazon export is read for: a box holds N plates of a chosen formula,
# charged as one amount whatever dishes fill it, and the same dish comes back
# week after week. The two questions that follow -- what did a plate cost
# then, and how many times did I eat this dish -- are what these read, where
# Amazon's purchase dashboard (spend, basket average, top purchases) has
# nothing to say about a Seazon account.

# (id, timestamp, title, fields_json) -- crud.get_titled_rows. The dish is the
# title; how many portions of it the box held, and whether that box ever
# arrived, are fields.
TitledRow = Tuple[int, Optional[datetime], Optional[str], Optional[str]]


def build_formula_dashboard(rows: List[DashboardRow], granularity: str, ranking: List[str]) -> Dict:
    """A Seazon platform's boxes read by formula: how many 10-plate boxes, how
    many 14-plate ones, what each cost, and what a plate cost over time.

    rows: `seazon_order` items (crud.get_dashboard_rows) -- one row per box, so
    nothing here has to be counted by distinct `context` the way the purchase
    dashboard does. Returns `formulas` (one entry per formula, most boxes
    first: boxes, plates, spend, price per box, price per PLATE -- the figure
    the price of a box is really about, since one amount covers the whole box
    whatever mix of dishes it holds -- and the date of the last one), `boxes`
    (stacked bars: how many boxes per bucket) and `unit_price` (one line per
    formula: what a plate cost in that bucket, None where the formula had no
    box -- a gap, never a free plate)."""
    slots = series_slots(ranking)
    totals: Dict[str, Dict] = {}
    box_counts: Dict[Tuple[datetime, str], int] = {}
    plate_counts: Dict[Tuple[datetime, str], int] = {}
    spends: Dict[Tuple[datetime, str], float] = {}
    bucket_set = set()

    for _id, ts, item_type, _context, _content, fields_json in rows:
        if item_type != SEAZON_ORDER_TYPE:
            continue
        fields = json.loads(fields_json) if fields_json else {}
        formula = formula_name(fields)
        plates = seazon_plate_count(fields) or 0
        amount = purchase_amount(fields)
        entry = totals.setdefault(
            formula, {"label": formula, "boxes": 0, "plates": 0, "spend": 0.0, "last": None},
        )
        entry["boxes"] += 1
        entry["plates"] += plates
        entry["spend"] += amount
        if ts and (entry["last"] is None or ts > entry["last"]):
            entry["last"] = ts
        if ts is None:
            continue
        bucket = _bucket_key(ts, granularity)
        bucket_set.add(bucket)
        box_counts[(bucket, formula)] = box_counts.get((bucket, formula), 0) + 1
        plate_counts[(bucket, formula)] = plate_counts.get((bucket, formula), 0) + plates
        spends[(bucket, formula)] = spends.get((bucket, formula), 0.0) + amount

    for entry in totals.values():
        entry["spend"] = round(entry["spend"], 2)
        entry["per_box"] = round(entry["spend"] / entry["boxes"], 2) if entry["boxes"] else None
        entry["per_plate"] = round(entry["spend"] / entry["plates"], 2) if entry["plates"] else None
    formulas = sorted(totals.values(), key=lambda entry: (-entry["boxes"], entry["label"]))

    buckets, axis_labels = _axis(bucket_set, granularity)
    members = {formula: [formula] for formula in totals}

    def price_per_plate(formula: str) -> List[Optional[float]]:
        values = []
        for bucket in buckets:
            plates = plate_counts.get((bucket, formula), 0)
            values.append(round(spends.get((bucket, formula), 0.0) / plates, 2) if plates else None)
        return values

    return {
        "formulas": formulas,
        "boxes": {**axis_labels, "series": _series_list(
            totals, slots, lambda formula: [box_counts.get((b, formula), 0) for b in buckets], members,
        )},
        "unit_price": {**axis_labels, "series": _series_list(totals, slots, price_per_plate, members)},
        "total_boxes": sum(entry["boxes"] for entry in formulas),
        "total_plates": sum(entry["plates"] for entry in formulas),
        "total_spend": round(sum(entry["spend"] for entry in formulas), 2),
    }


def build_dish_ranking(rows: List[TitledRow], limit: Optional[int] = None) -> Dict:
    """A Seazon platform's dishes, most eaten first.

    rows: `meal` items (crud.get_titled_rows). A dish is counted by PORTIONS,
    not by boxes -- three portions of the same dish in one box were eaten three
    times -- and only from a box that was delivered; `undelivered` keeps beside
    it the picks of the boxes cancelled before payment or before delivery,
    which were never eaten (a dish only ever picked in one of those is listed
    with nothing eaten, rather than dropped: it was ordered and never came).
    Spelling variants are one dish, the rule build_ranking already uses.

    Returns {top: [{label, key, count, undelivered}], distinct, total,
    undelivered_total}: `total` and `undelivered_total` are portions, and
    `distinct` counts the dishes actually eaten -- a dish only ever picked for
    a box that never arrived is listed (with nothing eaten) but is not a
    recipe that was tasted."""
    spellings: Dict[str, Counter] = {}
    eaten: Dict[str, int] = {}
    missed: Dict[str, int] = {}
    total = undelivered_total = 0
    for _id, _ts, title, fields_json in rows:
        if not title or not title.strip():
            continue
        fields = json.loads(fields_json) if fields_json else {}
        portions = seazon_portions(fields)
        key = usage_key(title)
        spellings.setdefault(key, Counter())[" ".join(title.split())] += portions
        if seazon_delivered(fields):
            eaten[key] = eaten.get(key, 0) + portions
            total += portions
        else:
            missed[key] = missed.get(key, 0) + portions
            undelivered_total += portions
    ranked = sorted(
        set(eaten) | set(missed), key=lambda key: (-eaten.get(key, 0), -missed.get(key, 0), key),
    )[:limit]
    return {
        "top": [{"label": spellings[key].most_common(1)[0][0], "key": key,
                 "count": eaten.get(key, 0), "undelivered": missed.get(key, 0)} for key in ranked],
        "distinct": len(eaten),
        "total": total,
        "undelivered_total": undelivered_total,
    }


def build_dish_trend(rows: List[TitledRow], granularity: str, leaders: List[Dict]) -> Dict:
    """The most eaten dishes over time: one bar per (bucket, dish) counting the
    PORTIONS eaten, and only from delivered boxes, so a bar is something that
    was actually eaten. `leaders` come from build_dish_ranking over the whole
    history, so a dish keeps its colour whatever the period shown."""
    counts = []
    for _id, ts, title, fields_json in rows:
        if not ts or not title or not title.strip():
            continue
        fields = json.loads(fields_json) if fields_json else {}
        if seazon_delivered(fields):
            counts.append((ts, usage_key(title), seazon_portions(fields)))
    return _leaders_trend(counts, granularity, leaders)


def select_formula_orders(
    rows: List[DashboardRow], formula: Optional[str] = None,
    date_from: Optional[datetime] = None, date_to: Optional[datetime] = None,
) -> List[int]:
    """Ids of the boxes of one formula over a period (`formula` left out: every
    box), most recent first -- what a bar of the boxes chart or of the
    price-per-plate chart opens."""
    selected = []
    for id_, ts, item_type, _context, _content, fields_json in rows:
        if item_type != SEAZON_ORDER_TYPE or not _within(ts, date_from, date_to):
            continue
        if formula is not None and formula_name(json.loads(fields_json) if fields_json else {}) != formula:
            continue
        selected.append((ts or datetime.min, id_))
    return [id_ for _ts, id_ in sorted(selected, reverse=True)]


def select_meals(
    rows: List[TitledRow], key: Optional[str] = None, exclude: Iterable[str] = (),
    date_from: Optional[datetime] = None, date_to: Optional[datetime] = None, delivered_only: bool = True,
) -> List[int]:
    """Ids of the dishes behind one entry of the dish list (`key`), behind
    every dish but the ones a chart drew (`exclude`, its leaders -- the
    "Autres" series), or behind every dish at all, over a period, most recent
    first: what a dish opens in the side panel. `delivered_only` is the rule
    the ranking counts by, so a panel opened from it shows exactly what was
    counted."""
    excluded = set(exclude)
    selected = []
    for id_, ts, title, fields_json in rows:
        if not _within(ts, date_from, date_to):
            continue
        dish = usage_key(title) if title and title.strip() else None
        if key is not None and dish != key:
            continue
        if excluded and (dish is None or dish in excluded):
            continue
        if delivered_only and not seazon_delivered(json.loads(fields_json) if fields_json else {}):
            continue
        selected.append((ts or datetime.min, id_))
    return [id_ for _ts, id_ in sorted(selected, reverse=True)]


# ────────────────────────────────────────────────
# Messaging (Facebook / Instagram messages)
# ────────────────────────────────────────────────

# (id, timestamp, author, context) -- crud.get_message_rows; `context` is
# the conversation (thread title).
MessageRow = Tuple[int, Optional[datetime], Optional[str], Optional[str]]

# direction -> (label, palette slot): the account owner keeps the first
# slot on every messaging chart, everyone else the second.
MESSAGE_DIRECTIONS = {"sent": ("Envoyés", 0), "received": ("Reçus", 1)}
REPLY_SERIES_LABELS = {"sent": "Vous", "received": "Vos contacts"}

# A message right after someone else's in the same conversation counts as
# a reply only within this delay: past it, the conversation is being
# picked back up rather than answered, and would only drown the medians.
REPLY_WINDOW = timedelta(hours=24)
# Upper bound (seconds, exclusive) and label of each reply-delay band.
REPLY_BANDS = [
    (60, "< 1 min"), (5 * 60, "1–5 min"), (15 * 60, "5–15 min"), (3600, "15–60 min"),
    (3 * 3600, "1–3 h"), (12 * 3600, "3–12 h"), (24 * 3600 + 1, "12–24 h"),
]

# Conversation series of the "active conversations" chart, slots clear of
# the sent/received pair drawn on the same page.
_ONGOING_THREADS = ("Déjà en cours", 2)
_NEW_THREADS = ("Nouvelles", 3)


# Which conversations the messaging page looks at: key -> label. Every
# filter keeps or drops whole conversations, never part of one.
THREAD_KINDS = {"private": "Conversations privées", "group": "Conversations de groupe"}


def message_direction(author: Optional[str], me: Optional[str]) -> str:
    return "sent" if me is not None and author == me else "received"


def group_threads(rows: List[MessageRow]) -> set:
    """The conversations more than two people wrote in, over the rows given
    (the whole history: a group chat stays one whatever the period)."""
    authors: Dict[Optional[str], set] = {}
    for _id, _ts, author, context in rows:
        if author:
            authors.setdefault(context, set()).add(author)
    return {context for context, names in authors.items() if len(names) > 2}


def filter_threads(rows: List[MessageRow], kind: Optional[str]) -> List[MessageRow]:
    """Only the messages of private conversations, or of group ones."""
    if kind not in THREAD_KINDS:
        return rows
    groups = group_threads(rows)
    return [row for row in rows if (row[3] in groups) == (kind == "group")]


def reply_band(seconds: float) -> int:
    for index, (bound, _label) in enumerate(REPLY_BANDS):
        if seconds < bound:
            return index
    return len(REPLY_BANDS) - 1


def message_replies(rows: List[MessageRow], me: Optional[str]) -> List[Tuple[int, datetime, str, float]]:
    """(id, timestamp, direction, delay in seconds) of every reply: a
    message of mine right after someone else's in the same conversation
    ("sent"), or someone else's right after mine ("received"), within
    REPLY_WINDOW. Two other members of a group chat answering each other
    is neither -- not an exchange with the account owner. Consecutive
    messages pair up, so after a burst ("Tu viens ?" "Ce soir ?") the
    delay runs from the burst's last message, the one actually answered."""
    if me is None:
        return []
    threads: Dict[Optional[str], List[MessageRow]] = {}
    for row in rows:
        if row[1] is not None:
            threads.setdefault(row[3], []).append(row)
    replies = []
    for messages in threads.values():
        messages.sort(key=lambda r: (r[1], r[0]))
        for prev, cur in zip(messages, messages[1:]):
            prev_mine, cur_mine = prev[2] == me, cur[2] == me
            if prev_mine == cur_mine:
                continue
            delay = cur[1] - prev[1]
            if delay <= REPLY_WINDOW:
                replies.append((cur[0], cur[1], "sent" if cur_mine else "received", delay.total_seconds()))
    return replies


def first_messages(rows: List[MessageRow]) -> Dict[Optional[str], MessageRow]:
    """context -> the first dated message of that conversation, over the
    rows given (the whole history: a conversation is "new" the day it
    started, not the first day of the selected period)."""
    first: Dict[Optional[str], MessageRow] = {}
    for row in rows:
        if row[1] is None:
            continue
        current = first.get(row[3])
        if current is None or row[1] < current[1]:
            first[row[3]] = row
    return first


def format_delay(seconds: Optional[float]) -> str:
    """"45 s", "4 min 30 s", "25 min", "2 h 05"."""
    if seconds is None:
        return "—"
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 10:
        return f"{minutes} min {secs:02d} s" if secs else f"{minutes} min"
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d}"


def build_messaging_dashboard(all_rows: List[MessageRow], me: Optional[str], granularity: str,
                              date_from: Optional[datetime], date_to: Optional[datetime],
                              top_n: int = 12) -> Dict:
    """Who you talk with, how much and how fast, over the chosen period:
    sent vs received per bucket, active and new conversations per bucket,
    the busiest conversations, the weekday x hour grid of messages sent,
    and the reply-delay distribution both ways.

    `all_rows` is the platform's whole message history: when a conversation
    started and which ones are group chats (more than two people ever wrote
    in it) don't depend on the period looked at. "Me" is the best-effort
    account owner of the conversations page (crud.get_me_candidate)."""
    first = first_messages(all_rows)
    groups = group_threads(all_rows)

    bucket_set = set()
    counts: Dict[Tuple[datetime, str], int] = {}
    active: Dict[datetime, set] = {}
    per_thread: Dict[Optional[str], Dict[str, int]] = {}
    contacts = set()
    totals = {"sent": 0, "received": 0}
    heatmap = [[0] * 24 for _ in range(7)]
    for _id, ts, author, context in in_period(all_rows, date_from, date_to):
        direction = message_direction(author, me)
        totals[direction] += 1
        thread = per_thread.setdefault(context, {"sent": 0, "received": 0})
        thread[direction] += 1
        if author and author != me:
            contacts.add(author)
        if ts is None:
            continue
        bucket = _bucket_key(ts, granularity)
        bucket_set.add(bucket)
        counts[(bucket, direction)] = counts.get((bucket, direction), 0) + 1
        active.setdefault(bucket, set()).add(context)
        if direction == "sent":
            heatmap[ts.weekday()][ts.hour] += 1

    new_by_bucket: Counter = Counter()
    for _id, ts, _author, _context in first.values():
        if _within(ts, date_from, date_to):
            new_by_bucket[_bucket_key(ts, granularity)] += 1

    buckets, axis = _axis(bucket_set, granularity)
    axis["series"] = [
        {"label": label, "slot": slot, "keys": [direction],
         "data": [counts.get((b, direction), 0) for b in buckets]}
        for direction, (label, slot) in MESSAGE_DIRECTIONS.items()
    ]
    axis["thread_series"] = [
        {"label": _ONGOING_THREADS[0], "slot": _ONGOING_THREADS[1], "keys": ["ongoing"],
         "data": [len(active.get(b, ())) - new_by_bucket.get(b, 0) for b in buckets]},
        {"label": _NEW_THREADS[0], "slot": _NEW_THREADS[1], "keys": ["new"],
         "data": [new_by_bucket.get(b, 0) for b in buckets]},
    ]

    ranked = sorted(per_thread.items(), key=lambda kv: -(kv[1]["sent"] + kv[1]["received"]))[:top_n]
    top_threads = {
        "labels": [(context or "(sans titre)") + (" · groupe" if context in groups else "") for context, _ in ranked],
        "contexts": [context for context, _ in ranked],
        "series": [
            {"label": label, "slot": slot, "keys": [direction], "data": [counts_[direction] for _, counts_ in ranked]}
            for direction, (label, slot) in MESSAGE_DIRECTIONS.items()
        ],
    }

    delays: Dict[str, List[float]] = {"sent": [], "received": []}
    for _id, ts, direction, seconds in message_replies(all_rows, me):
        if _within(ts, date_from, date_to):
            delays[direction].append(seconds)
    reply_series = []
    for direction, (_label, slot) in MESSAGE_DIRECTIONS.items():
        band_counts = [0] * len(REPLY_BANDS)
        for seconds in delays[direction]:
            band_counts[reply_band(seconds)] += 1
        n = len(delays[direction])
        reply_series.append({
            "label": REPLY_SERIES_LABELS[direction], "slot": slot, "keys": [direction],
            # Shares, not counts: you and your contacts don't answer the
            # same number of times, only the shapes compare.
            "data": [round(c * 100 / n, 1) if n else 0 for c in band_counts],
        })

    axis.update({
        "top_threads": top_threads,
        "heatmap": heatmap,
        "heatmap_max": max((max(row) for row in heatmap), default=0),
        "reply_labels": [label for _bound, label in REPLY_BANDS],
        "reply_series": reply_series,
        "reply_counts": {d: len(v) for d, v in delays.items()},
        "reply_medians": {d: (statistics.median(v) if v else None) for d, v in delays.items()},
        "total_sent": totals["sent"],
        "total_received": totals["received"],
        "total_threads": len(per_thread),
        "group_threads": len(groups & per_thread.keys()),
        "new_threads": sum(new_by_bucket.values()),
        "contacts": len(contacts),
    })
    return axis


def period_threads(all_rows: List[MessageRow], me: Optional[str], direction: Optional[str],
                   date_from: Optional[datetime], date_to: Optional[datetime]) -> List[Dict]:
    """The conversations with messages over the period, busiest first, with
    how many messages each side wrote in them. `share` is the conversation's
    part of the period's messages (of `direction` when one is given), so the
    shares of the list add up to 100, give or take rounding."""
    groups = group_threads(all_rows)
    per_thread: Dict[Optional[str], Dict[str, int]] = {}
    for _id, _ts, author, context in in_period(all_rows, date_from, date_to):
        counts = per_thread.setdefault(context, {"sent": 0, "received": 0})
        counts[message_direction(author, me)] += 1

    def count(counts: Dict[str, int]) -> int:
        return counts[direction] if direction else counts["sent"] + counts["received"]

    total = sum(count(counts) for counts in per_thread.values())
    ranked = sorted(per_thread.items(), key=lambda kv: -count(kv[1]))
    return [
        {"context": context, "group": context in groups, "sent": counts["sent"], "received": counts["received"],
         "count": count(counts), "share": round(count(counts) * 100 / total, 1) if total else 0}
        for context, counts in ranked if count(counts)
    ]


def select_replies(all_rows: List[MessageRow], me: Optional[str], band: int, direction: Optional[str],
                   date_from: Optional[datetime], date_to: Optional[datetime]) -> List[int]:
    """Ids of the replies of one delay band (and direction), most recent
    first -- what a click on the reply-delay chart opens."""
    selected = [
        (ts, id_) for id_, ts, d, seconds in message_replies(all_rows, me)
        if reply_band(seconds) == band and (direction is None or d == direction) and _within(ts, date_from, date_to)
    ]
    return [id_ for _ts, id_ in sorted(selected, reverse=True)]


def select_first_messages(all_rows: List[MessageRow], date_from: Optional[datetime], date_to: Optional[datetime]) -> List[int]:
    """Ids of the opening message of every conversation that started within
    the period, most recent first."""
    selected = [(row[1], row[0]) for row in first_messages(all_rows).values() if _within(row[1], date_from, date_to)]
    return [id_ for _ts, id_ in sorted(selected, reverse=True)]


# ────────────────────────────────────────────────
# Network (Facebook friends, Instagram followers / following)
# ────────────────────────────────────────────────

# (id, timestamp, batch_id, item_type, title, fields_json) --
# crud.get_connection_rows.
ConnectionRow = Tuple[int, Optional[datetime], int, str, Optional[str], Optional[str]]

# The relationship lists a network is made of: role -> (label, item types).
# Several types per role: a list has had different file names across
# export versions, and between platforms.
NETWORK_ROLES = {
    "connections": ("Relations", ("connection",)),
    "friends": ("Amis", ("your_friends", "friends")),
    "followers": ("Abonnés", ("followers",)),
    "following": ("Abonnements", ("following", "who_you've_followed", "member_follow")),
    "companies": ("Entreprises suivies", ("company_follow",)),
}
NETWORK_ITEM_TYPES = [t for _label, types in NETWORK_ROLES.values() for t in types]
# Categories holding a platform's relationship records: Facebook/Instagram's
# "connections" folder, LinkedIn's "Réseau".
NETWORK_CATEGORIES = ["connections", "Réseau"]
# Fields of a relationship record that describe the person rather than name
# them (LinkedIn's employer and job title), never taken for their name.
_DESCRIPTIVE_FIELDS = {"Entreprise", "Poste", "E-mail"}

# French names of the relationship records found in the "connections"
# category (requests, blocks, close friends...); any other one shows under
# its humanized file name.
CONNECTION_TYPE_LABELS = {
    "your_friends": "Amis",
    "friends": "Amis",
    "followers": "Abonnés",
    "following": "Abonnements",
    "who_you've_followed": "Abonnements",
    "close_friends": "Amis proches",
    "removed_friends": "Amis retirés",
    "sent_friend_requests": "Demandes d'ami envoyées",
    "received_friend_requests": "Demandes d'ami reçues",
    "rejected_friend_requests": "Demandes d'ami refusées",
    "pending_follow_requests": "Demandes d'abonnement en attente",
    "recent_follow_requests": "Demandes d'abonnement récentes",
    "recently_unfollowed_profiles": "Désabonnements récents",
    "recently_unfollowed_accounts": "Désabonnements récents",
    "blocked_profiles": "Comptes bloqués",
    "blocked_accounts": "Comptes bloqués",
    "restricted_profiles": "Comptes restreints",
    "restricted_accounts": "Comptes restreints",
    "removed_suggestions": "Suggestions retirées",
    "hide_story_from": "Story masquée pour",
    "connection": "Relations",
    "invitation_received": "Invitations reçues",
    "invitation_sent": "Invitations envoyées",
    "invitation": "Invitations",
    "company_follow": "Entreprises suivies",
    "member_follow": "Membres suivis",
    "member_unfollow": "Membres plus suivis",
    "hashtag_follow": "Hashtags suivis",
    "hashtag_unfollow": "Hashtags plus suivis",
    "connection_note": "Notes sur vos relations",
}

# Who someone is to you, per reciprocity group.
RECIPROCITY_GROUPS = {
    "mutual": "Réciproques",
    "not_following_back": "Ne vous suivent pas en retour",
    "not_followed_back": "Vous ne les suivez pas en retour",
}
# Who left, per role, between an export and the latest one.
LOST_LABELS = {
    "connections": "Ne sont plus vos relations",
    "friends": "Ne sont plus vos amis",
    "followers": "Ne vous suivent plus",
    "following": "Vous ne suivez plus",
    "companies": "Entreprises plus suivies",
}


def connection_type_label(item_type: str) -> str:
    return CONNECTION_TYPE_LABELS.get(item_type) or humanize(item_type)


def connection_identity(title: Optional[str], fields: Dict) -> Tuple[Optional[str], Optional[str]]:
    """(name, profile link) of one relationship record. Depending on the
    list and the export version, Instagram puts the username in the record
    title, in the entry value, or only in the profile URL -- tried in that
    order."""
    url = value = None
    for key, v in (fields or {}).items():
        if not isinstance(v, str) or not v.strip():
            continue
        if v.startswith(("http://", "https://")):
            url = url or v
        elif value is None and key not in _DESCRIPTIVE_FIELDS:
            value = v.strip()
    name = (title or "").strip() or value
    if not name and url:
        name = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1] or None
    return name, url


def _identity_key(id_: int, name: Optional[str]) -> str:
    # The same account across lists and exports, whatever its spelling there
    # ("@Alice" in one, "alice" in another); a nameless record only matches
    # itself.
    return name.strip().lstrip("@").lower() if name else f"#{id_}"


def network_snapshot(rows: List[ConnectionRow], batches_by_type: Dict[str, List[int]]) -> Dict[str, Dict]:
    """role -> {label, item_type, batch_id, previous_batch_id, members,
    lost, undated} for every role (friends, followers, following) the
    platform has a list for.

    A relationship list is a snapshot, not a history: someone who stopped
    following you is simply absent from the next export. So the current
    network is the list of the LATEST import holding it (not every import
    merged, which would still count everyone who ever followed you), and
    the accounts found in an earlier import but not in the latest one are
    `lost` (None with a single import: nothing to compare). Accounts are
    matched by name, not by item fingerprint, so a cosmetic change between
    two export versions (a profile URL format) doesn't read as a departure.

    `members`/`lost`: key -> {id, name, url, since, batch_id, company},
    where `since` is the date the relationship started, `batch_id` the most
    recent import the account was found in and `company` its employer
    (LinkedIn connections only). A nameless record (a LinkedIn member who
    hides their profile from exports) can't be matched across exports, so
    it is never reported as lost."""
    snapshot: Dict[str, Dict] = {}
    for role, (label, types) in NETWORK_ROLES.items():
        present = [t for t in types if batches_by_type.get(t)]
        if not present:
            continue
        item_type = max(present, key=lambda t: batches_by_type[t][-1])
        batches = batches_by_type[item_type]
        latest = batches[-1]
        members: Dict[str, Dict] = {}
        older: Dict[str, Dict] = {}
        for id_, ts, batch_id, row_type, title, fields_json in rows:
            if row_type != item_type:
                continue
            fields = json.loads(fields_json) if fields_json else {}
            name, url = connection_identity(title, fields)
            person = {"id": id_, "name": name or "(inconnu)", "url": url, "since": ts, "batch_id": batch_id,
                      "company": fields.get("Entreprise")}
            key = _identity_key(id_, name)
            bucket = members if batch_id == latest else older
            # Two namesakes in one list (it happens among a thousand LinkedIn
            # connections) are told apart by their profile link.
            if key in bucket and url and bucket[key]["url"] and bucket[key]["url"] != url \
                    and bucket[key]["batch_id"] == batch_id:
                key = f"{key}|{url}"
            if batch_id == latest:
                members.setdefault(key, person)
            elif name and (key not in older or batch_id > older[key]["batch_id"]):
                older[key] = person
        snapshot[role] = {
            "role": role,
            "label": label,
            "item_type": item_type,
            "batch_id": latest,
            "previous_batch_id": batches[-2] if len(batches) > 1 else None,
            "members": members,
            "lost": {k: p for k, p in older.items() if k not in members} if len(batches) > 1 else None,
            "undated": sum(1 for p in members.values() if p["since"] is None),
        }
    return snapshot


def reciprocity(snapshot: Dict[str, Dict]) -> Optional[Dict[str, List[Dict]]]:
    """Followers vs following of the latest lists: who follows you back,
    who doesn't, whom you don't follow back. None without both lists."""
    if "followers" not in snapshot or "following" not in snapshot:
        return None
    followers, following = snapshot["followers"]["members"], snapshot["following"]["members"]
    return {
        "mutual": [following[k] for k in following.keys() & followers.keys()],
        "not_following_back": [following[k] for k in following.keys() - followers.keys()],
        "not_followed_back": [followers[k] for k in followers.keys() - following.keys()],
    }


def _company_key(name: str) -> str:
    # Members type their employer themselves: "Globex-Corp" and "Globex
    # - Corp" are the same company. Whitespace is collapsed first, so a
    # separator has at most one space on either side: " ?" is all it takes,
    # and unlike "\s*" it cannot backtrack quadratically.
    return re.sub(r" ?([-&/]) ?", r"\1", " ".join(name.split())).casefold()


def network_companies(snapshot: Dict[str, Dict], limit: int = 15) -> Optional[Dict]:
    """Where the current connections work (LinkedIn): {top: [{label,
    count}], companies, with_company, total}, the most represented employers
    first, each under its most frequent spelling. None when the latest list
    carries no employer at all (a network without that information, e.g.
    Instagram's)."""
    members = snapshot.get("connections", {}).get("members", {})
    spellings: Dict[str, Counter] = {}
    for p in members.values():
        if p.get("company"):
            spellings.setdefault(_company_key(p["company"]), Counter())[p["company"]] += 1
    if not spellings:
        return None
    counts = {names.most_common(1)[0][0]: sum(names.values()) for names in spellings.values()}
    return {
        "top": _ranked(Counter(counts), limit),
        "companies": len(counts),
        "with_company": sum(counts.values()),
        "total": len(members),
    }


def company_people(snapshot: Dict[str, Dict], company: str) -> List[Dict]:
    """The current connections working at `company`, however they spell it."""
    key = _company_key(company)
    members = snapshot.get("connections", {}).get("members", {})
    return [p for p in members.values() if p.get("company") and _company_key(p["company"]) == key]


def sort_people(people: Iterable[Dict]) -> List[Dict]:
    """Most recent relationship first, undated ones last, then by name."""
    return sorted(people, key=lambda p: (p["since"] is not None, p["since"] or datetime.min, p["name"].lower()), reverse=True)


def build_network_dashboard(rows: List[ConnectionRow], snapshot: Dict[str, Dict], granularity: str,
                            date_from: Optional[datetime], date_to: Optional[datetime]) -> Dict:
    """`timeline`: dated relationship records per bucket, stacked by type
    (every import: someone who followed you and left since did follow you
    on that date). `growth`: the size of the current network (latest
    lists) at the end of each bucket, counting each account from the date
    the relationship started -- people who left since aren't in it, which
    the page says. Both keep a type's colour, ranked over the whole
    history."""
    ranking = [t for t, _ in Counter(row[3] for row in rows if row[1] is not None).most_common()]
    slots = series_slots(ranking)
    events = [(row[1], row[3]) for row in in_period(rows, date_from, date_to) if row[1] is not None]
    timeline = build_timeseries(events, granularity, ranking)
    for series in timeline["series"]:
        if series["label"] != OTHER_LABEL:
            series["label"] = connection_type_label(series["label"])

    dated = {role: sorted(p["since"] for p in info["members"].values() if p["since"] is not None)
             for role, info in snapshot.items()}
    bucket_set = {_bucket_key(ts, granularity) for times in dated.values() for ts in times if _within(ts, date_from, date_to)}
    buckets, growth = _axis(bucket_set, granularity)
    growth["series"] = []
    for role, info in snapshot.items():
        running = sum(1 for ts in dated[role] if date_from and ts < date_from)
        added = Counter(_bucket_key(ts, granularity) for ts in dated[role] if _within(ts, date_from, date_to))
        data = []
        for b in buckets:
            running += added.get(b, 0)
            data.append(running)
        growth["series"].append({
            "label": info["label"], "slot": slots.get(info["item_type"], "other"), "data": data,
            "keys": [info["item_type"]], "batch_id": info["batch_id"],
        })
    return {"timeline": timeline, "growth": growth, "total_events": len(events)}


# ────────────────────────────────────────────────
# Posts (Facebook / Instagram)
# ────────────────────────────────────────────────

# (id, timestamp, content) -- crud.get_post_rows.
PostRow = Tuple[int, Optional[datetime], Optional[str]]

# What a post mainly is, by its media: kind -> (label, palette slot). A
# post mixing photos and a video counts as a video.
POST_KINDS = {"photo": ("Photo", 0), "video": ("Vidéo", 1), "text": ("Sans média", 2)}
MONTH_NAMES_FR = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
GALLERY_SIZE = 24


def post_kind(media: List[Tuple[str, str]]) -> str:
    types = {media_type for media_type, _url in media}
    if "video" in types:
        return "video"
    return "photo" if "photo" in types else "text"


def post_kind_counts(rows: List[PostRow], media_by_post: Dict[int, List[Tuple[str, str]]]) -> Counter:
    return Counter(post_kind(media_by_post.get(row[0], [])) for row in rows)


def filter_posts(rows: List[PostRow], media_by_post: Dict[int, List[Tuple[str, str]]], kinds: Iterable[str]) -> List[PostRow]:
    """Only the posts of the given media kinds (every post without any)."""
    wanted = set(kinds) & POST_KINDS.keys()
    if not wanted:
        return rows
    return [row for row in rows if post_kind(media_by_post.get(row[0], [])) in wanted]


def select_posts(rows: List[PostRow], media_by_post: Dict[int, List[Tuple[str, str]]], kind: Optional[str] = None,
                 month: Optional[int] = None, weekday: Optional[int] = None, hour: Optional[int] = None) -> List[int]:
    """Ids of the posts matching a chart slice (media kind, calendar month
    1-12, weekday, hour), most recent first."""
    selected = []
    for id_, ts, _content in rows:
        if kind and post_kind(media_by_post.get(id_, [])) != kind:
            continue
        if (month is not None or weekday is not None or hour is not None) and ts is None:
            continue
        if month is not None and ts.month != month:
            continue
        if weekday is not None and ts.weekday() != weekday:
            continue
        if hour is not None and ts.hour != hour:
            continue
        selected.append((ts or datetime.min, id_))
    return [id_ for _ts, id_ in sorted(selected, reverse=True)]


def build_posts_dashboard(rows: List[PostRow], media_by_post: Dict[int, List[Tuple[str, str]]], granularity: str) -> Dict:
    """rows: the posts of the period. Posts per bucket and per calendar
    month (seasonality, every year together), stacked by media kind with
    fixed colours; the weekday x hour grid; media totals; and the most
    recent posts with media for the gallery."""
    bucket_set = set()
    counts: Dict[Tuple[datetime, str], int] = {}
    month_counts: Dict[Tuple[int, str], int] = {}
    heatmap = [[0] * 24 for _ in range(7)]
    kinds_seen = set()
    totals = {"posts": 0, "photos": 0, "videos": 0, "text": 0}
    gallery = []
    for id_, ts, content in rows:
        media = media_by_post.get(id_, [])
        kind = post_kind(media)
        kinds_seen.add(kind)
        totals["posts"] += 1
        totals["photos"] += sum(1 for media_type, _url in media if media_type == "photo")
        totals["videos"] += sum(1 for media_type, _url in media if media_type == "video")
        totals["text"] += kind == "text"
        if media:
            thumb = next((url for media_type, url in media if media_type == "photo"), None)
            gallery.append({"id": id_, "timestamp": ts, "kind": kind, "thumb": thumb,
                            "media_count": len(media), "content": content})
        if ts is None:
            continue
        bucket = _bucket_key(ts, granularity)
        bucket_set.add(bucket)
        counts[(bucket, kind)] = counts.get((bucket, kind), 0) + 1
        month_counts[(ts.month, kind)] = month_counts.get((ts.month, kind), 0) + 1
        heatmap[ts.weekday()][ts.hour] += 1

    kinds = [(k, label, slot) for k, (label, slot) in POST_KINDS.items() if k in kinds_seen]
    buckets, axis = _axis(bucket_set, granularity)
    axis.update({
        "series": [{"label": label, "slot": slot, "keys": [k], "data": [counts.get((b, k), 0) for b in buckets]}
                   for k, label, slot in kinds],
        "month_labels": MONTH_NAMES_FR,
        "month_series": [{"label": label, "slot": slot, "keys": [k], "data": [month_counts.get((m, k), 0) for m in range(1, 13)]}
                         for k, label, slot in kinds],
        "heatmap": heatmap,
        "heatmap_max": max((max(row) for row in heatmap), default=0),
        "totals": totals,
        "gallery": sorted(gallery, key=lambda g: g["timestamp"] or datetime.min, reverse=True)[:GALLERY_SIZE],
        "gallery_total": len(gallery),
    })
    return axis


# ────────────────────────────────────────────────
# Career (LinkedIn): positions, studies, endorsements, recommendations
# ────────────────────────────────────────────────

# (id, timestamp, item_type, title, author, context, fields_json) --
# crud.get_career_rows.
CareerRow = Tuple[int, Optional[datetime], str, Optional[str], Optional[str], Optional[str], Optional[str]]

# What the career timeline draws: item_type -> (label, palette slot).
CAREER_PERIODS = {"position": ("Poste", 0), "education": ("Études", 1), "volunteering": ("Bénévolat", 2)}
# Endorsements both ways, the account owner's own in the first slot, like
# messages sent.
ENDORSEMENT_DIRECTIONS = {"endorsement_given": ("Données", 0), "endorsement_received": ("Reçues", 1)}
RECOMMENDATION_TYPES = ["recommendation_received", "recommendation_given"]
CAREER_ITEM_TYPES = [*CAREER_PERIODS, "certification", *ENDORSEMENT_DIRECTIONS, *RECOMMENDATION_TYPES, "learning_course"]
TOP_SKILLS = 15
TOP_PEOPLE = 10


def _month_start(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, 1)


def _add_months(dt: datetime, months: int) -> datetime:
    index = dt.year * 12 + dt.month - 1 + months
    return datetime(index // 12, index % 12 + 1, 1)


def format_months(months: int) -> str:
    """"12 ans 3 mois", "8 mois", "1 an"."""
    years, rest = divmod(months, 12)
    parts = [f"{years} an{'s' if years > 1 else ''}"] if years else []
    if rest or not years:
        parts.append(f"{rest} mois")
    return " ".join(parts)


def _period_end(item_type: str, start: datetime, fields: Dict, today: datetime) -> Tuple[datetime, bool]:
    """(end, ongoing) of a position, studies or volunteering, as the first
    day after it. Positions and volunteering end on a month, included
    ("Fin": "03/2022"), or not at all while still held. Studies give years:
    "2010 – 2013" spans from the start of 2010 to the start of 2013; studies
    without an end year are drawn one year long rather than as ongoing."""
    end_text = str(fields.get("Fin") or "")
    if item_type == "education":
        end = datetime(int(end_text), 1, 1) if end_text.isdigit() else None
        return (end if end and end > start else start.replace(year=start.year + 1)), False
    try:
        return _add_months(datetime.strptime(end_text, "%m/%Y"), 1), False
    except ValueError:
        return _add_months(_month_start(today), 1), True


def _place_on_year_axis(entries: List[Dict]) -> List[Dict]:
    """Sets `left`/`width` (percent of the axis) on entries spanning
    `start` -> `end`, on an axis of whole calendar years around them, and
    returns its year ticks ({label, left}), one every 1, 2 or 5 years."""
    axis_start = datetime(min(e["start"] for e in entries).year, 1, 1)
    last = max(e["end"] for e in entries)
    axis_end = datetime(last.year if (last.month, last.day) == (1, 1) else last.year + 1, 1, 1)
    total_days = max((axis_end - axis_start).days, 1)

    def position(dt: datetime) -> float:
        return round((dt - axis_start).days * 100 / total_days, 3)

    for entry in entries:
        entry["left"] = position(entry["start"])
        entry["width"] = round(position(entry["end"]) - entry["left"], 3)
    years = axis_end.year - axis_start.year
    step = 1 if years <= 12 else 2 if years <= 24 else 5
    return [{"label": y, "left": position(datetime(y, 1, 1))} for y in range(axis_start.year, axis_end.year + 1, step)]


def build_career_timeline(rows: List[CareerRow], today: datetime) -> Optional[Dict]:
    """Positions, studies and volunteering as bars on one calendar axis,
    most recent first: {entries, ticks, kinds, experience_months, positions,
    companies}, None without any. The whole history whatever the period
    chosen: a career reads whole. Experience counts the months at least one
    position covers, so two jobs held at once don't add up."""
    entries = []
    months_worked = set()
    companies = set()
    for id_, ts, item_type, title, _author, context, fields_json in rows:
        if item_type not in CAREER_PERIODS or ts is None:
            continue
        fields = json.loads(fields_json) if fields_json else {}
        start = _month_start(ts)
        end, ongoing = _period_end(item_type, start, fields, today)
        end = max(end, _add_months(start, 1))
        span = (end.year - start.year) * 12 + end.month - start.month
        if item_type == "education":
            when = f"{fields.get('Début')} – {fields.get('Fin')}" if fields.get("Fin") else str(start.year)
        else:
            until = "aujourd'hui" if ongoing else _add_months(end, -1).strftime("%m/%Y")
            when = f"{start:%m/%Y} – {until} · {format_months(span)}"
        if item_type == "position":
            months_worked.update(range(start.year * 12 + start.month, end.year * 12 + end.month))
            if context:
                companies.add(context.strip().lower())
        label, slot = CAREER_PERIODS[item_type]
        entries.append({
            "id": id_, "item_type": item_type, "kind": label, "slot": slot, "title": title or label,
            "org": context, "when": when, "start": start, "end": end, "ongoing": ongoing,
        })
    if not entries:
        return None

    ticks = _place_on_year_axis(entries)
    entries.sort(key=lambda e: (e["start"], e["end"]), reverse=True)
    present = {e["item_type"] for e in entries}
    return {
        "entries": entries,
        "ticks": ticks,
        "kinds": [{"label": label, "slot": slot} for t, (label, slot) in CAREER_PERIODS.items() if t in present],
        "experience_months": len(months_worked),
        "positions": sum(1 for e in entries if e["item_type"] == "position"),
        "companies": len(companies),
    }


def _ranked(counts: Counter, limit: int) -> List[Dict]:
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    return [{"label": name, "count": n} for name, n in ranked[:limit]]


def build_career_dashboard(rows: List[CareerRow], granularity: str,
                           date_from: Optional[datetime], date_to: Optional[datetime]) -> Dict:
    """The dated side of a career, over the chosen period: endorsements
    given and received per bucket, the skills most endorsed, who endorses
    you and whom you endorse the most, and the Learning courses viewed per
    bucket. `totals` counts each item type of the period."""
    totals: Counter = Counter()
    skills: Counter = Counter()
    endorsers: Counter = Counter()
    endorsees: Counter = Counter()
    endorsement_counts: Counter = Counter()
    course_counts: Counter = Counter()
    for _id, ts, item_type, title, author, context, _fields in rows:
        if not _within(ts, date_from, date_to):
            continue
        totals[item_type] += 1
        if item_type == "endorsement_received":
            if title:
                skills[title] += 1
            if author:
                endorsers[author] += 1
        elif item_type == "endorsement_given" and context:
            endorsees[context] += 1
        if ts is None:
            continue
        if item_type in ENDORSEMENT_DIRECTIONS:
            endorsement_counts[(_bucket_key(ts, granularity), item_type)] += 1
        elif item_type == "learning_course":
            course_counts[_bucket_key(ts, granularity)] += 1

    buckets, endorsements = _axis({b for b, _t in endorsement_counts}, granularity)
    endorsements["series"] = [
        {"label": label, "slot": slot, "keys": [item_type],
         "data": [endorsement_counts.get((b, item_type), 0) for b in buckets]}
        for item_type, (label, slot) in ENDORSEMENT_DIRECTIONS.items()
    ]
    course_buckets, learning = _axis(course_counts.keys(), granularity)
    learning["series"] = [{"label": "Cours consultés", "slot": 0, "keys": ["learning_course"],
                           "data": [course_counts.get(b, 0) for b in course_buckets]}]
    return {
        "endorsements": endorsements,
        "learning": learning,
        "skills": _ranked(skills, TOP_SKILLS),
        "skill_count": len(skills),
        "endorsers": _ranked(endorsers, TOP_PEOPLE),
        "endorser_count": len(endorsers),
        "endorsees": _ranked(endorsees, TOP_PEOPLE),
        "endorsee_count": len(endorsees),
        "totals": dict(totals),
    }


# ────────────────────────────────────────────────
# Usages (Google "My Activity"): what you do, whichever service logged it
# ────────────────────────────────────────────────

# (item_type, title or author, items) -- crud.get_value_counts: how often
# each value occurs among items of the given types, None included.
ValueCount = Tuple[str, Optional[str], int]

TOP_ENTITIES = 15
COVERAGE_ROWS = 15


@dataclass(frozen=True)
class UsageRanking:
    """A "most frequent" list of a usage family: which items it counts and
    by what -- their title (the app, the query, the video), their author (a
    video's channel, a page's site) or the words of their title."""
    title: str
    item_types: Tuple[str, ...]
    by: str
    noun: str
    # (item_type, the one standing in for it when the platform has none).
    stand_ins: Tuple[Tuple[str, str], ...] = ()

    @property
    def column(self) -> str:
        return "title" if self.by == "words" else self.by

    def types(self, present: Iterable[str]) -> List[str]:
        present = set(present)
        types = [t for t in self.item_types if t in present]
        types += [stand_in for missing, stand_in in self.stand_ins if missing not in present and stand_in in present]
        return types


@dataclass(frozen=True)
class UsageFamily:
    key: str
    category: str
    label: str  # what its headline figure counts: the first ranking's items
    icon: str
    rankings: Tuple[UsageRanking, ...]
    trend: bool = False  # draw the first ranking's leaders over time


_WEB_TYPES = ("page_visit", "image_view", "browser_history")
# Chrome's own "My Activity" record of a visit repeats its browsing history
# (the same visits, to the second, on the export this was checked against):
# counted only when that history wasn't exported.
_CHROME_STAND_IN = (("browser_history", "chrome_visit"),)
_ASSISTANT_TYPES = ("assistant_command", "ai_prompt")

USAGE_FAMILIES = {f.key: f for f in (
    UsageFamily("apps", google.APPS, "Ouvertures d'applis", "bi-phone", (
        UsageRanking("Applications les plus ouvertes", ("app_used",), "title", "applications"),
    ), trend=True),
    UsageFamily("search", google.SEARCH, "Recherches", "bi-search", (
        UsageRanking("Recherches les plus fréquentes", ("search",), "title", "différentes"),
        UsageRanking("Mots les plus recherchés", ("search",), "words", "mots"),
    )),
    UsageFamily("video", google.VIDEO, "Vidéos regardées", "bi-play-btn", (
        UsageRanking("Chaînes les plus regardées", ("video_watched",), "author", "chaînes"),
        UsageRanking("Vidéos les plus regardées", ("video_watched",), "title", "vidéos"),
    ), trend=True),
    UsageFamily("web", google.WEB, "Pages consultées", "bi-globe2", (
        UsageRanking("Sites les plus consultés", _WEB_TYPES, "author", "sites", _CHROME_STAND_IN),
        UsageRanking("Pages les plus consultées", _WEB_TYPES, "title", "pages", _CHROME_STAND_IN),
    ), trend=True),
    UsageFamily("places", google.PLACES, "Itinéraires", "bi-signpost-split", (
        UsageRanking("Destinations les plus demandées", ("directions",), "title", "destinations"),
        UsageRanking("Lieux les plus consultés", ("place_viewed",), "title", "lieux"),
    )),
    UsageFamily("assistant", google.ASSISTANT, "Demandes à l'assistant", "bi-stars", (
        UsageRanking("Demandes les plus fréquentes", _ASSISTANT_TYPES, "title", "différentes"),
        UsageRanking("Mots les plus fréquents", _ASSISTANT_TYPES, "words", "mots"),
    )),
    UsageFamily("tracking", google.TRACKING, "Signalements par des tiers", "bi-broadcast", (
        UsageRanking("Applis et sites qui renseignent Google", ("tracked_app", "tracked_site"), "title",
                     "applis et sites"),
    ), trend=True),
)}
USAGE_ITEM_TYPES = sorted({
    t for f in USAGE_FAMILIES.values() for r in f.rankings
    for t in (*r.item_types, *(stand_in for _missing, stand_in in r.stand_ins))
})
# What makes a platform's Usages tab: "search" alone doesn't (Amazon and
# LinkedIn searches have no family to sit in).
USAGE_VIEW_TYPES = sorted(set(USAGE_ITEM_TYPES) - {"search"})


def usage_families(present: Iterable[str]) -> List[UsageFamily]:
    """The families the platform has items for, in display order."""
    present = set(present)
    return [f for f in USAGE_FAMILIES.values() if f.rankings[0].types(present)]


def usage_key(value: str) -> str:
    """What a ranking groups by: case and spacing aside, "Claude" and
    "claude " are one search."""
    return " ".join(value.split()).casefold()


_WORD_RE = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*")
_ELISION_RE = re.compile(r"^(?:[cdjlmnst]|qu|jusqu|lorsqu|puisqu)['’]")
STOPWORDS = frozenset("""
    le la les un une des de du et ou en au aux pour par sur sous dans avec sans ce cet cette ces se sa son
    ses mon ma mes ton ta tes notre nos votre vos leur leurs je tu il elle on nous vous ils elles me te lui
    est sont être avoir ai as avez ont été qui que quoi quel quelle quels quelles comment pourquoi quand où
    ne pas plus moins très tout tous toute toutes faire fait entre si ni dont via
    the an and or of to in on at for with without by from is are was were be been it its this that these
    those how what when where why which who do does did can my me you your we our not no as into about vs
""".split())


def words_of(text: str) -> List[str]:
    """The meaningful words of a query, lower-cased: elisions dropped
    ("l'avion" -> "avion"), no stop words, numbers or single letters."""
    words = []
    for token in _WORD_RE.findall(text.casefold()):
        token = _ELISION_RE.sub("", token)
        if len(token) >= 2 and not token.isdigit() and token not in STOPWORDS:
            words.append(token)
    return words


def usage_matches(value: Optional[str], by: str, key: str) -> bool:
    """Whether an item's title/author falls under a ranking entry -- the
    same rule build_ranking counts with, for the drill-down panel."""
    if not value or not value.strip():
        return False
    if by == "words":
        return key in words_of(value)
    return usage_key(value) == key


def select_usage(rows: Iterable[Tuple[int, Optional[datetime], Optional[str]]], by: str,
                 key: Optional[str] = None, exclude: Iterable[str] = ()) -> List[int]:
    """Ids of the items behind a ranking entry (`key`), or behind every
    entity but the leaders of its chart (`exclude`), or of every item with a
    value -- most recent first. rows: (id, timestamp, title or author)."""
    excluded = set(exclude)
    selected = []
    for id_, ts, value in rows:
        if not value or not value.strip():
            continue
        if key is not None and not usage_matches(value, by, key):
            continue
        if excluded:
            found = set(words_of(value)) if by == "words" else {usage_key(value)}
            if found & excluded:
                continue
        selected.append((ts or datetime.min, id_))
    return [id_ for _ts, id_ in sorted(selected, reverse=True)]


def build_ranking(counts: Iterable[ValueCount], by: str, limit: Optional[int] = TOP_ENTITIES) -> Dict:
    """{top: [{label, key, count}], distinct, total}: the most frequent
    values, each under its most frequent spelling -- or, with by="words",
    the words of the values, each counted once per item using it. Items
    without a value only count in `total`."""
    spellings: Dict[str, Counter] = {}
    total = 0
    for _item_type, value, n in counts:
        total += n
        if not value or not value.strip():
            continue
        if by == "words":
            for word in set(words_of(value)):
                spellings.setdefault(word, Counter())[word] += n
        else:
            spellings.setdefault(usage_key(value), Counter())[" ".join(value.split())] += n
    totals = {key: sum(names.values()) for key, names in spellings.items()}
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    if limit is not None:
        ranked = ranked[:limit]
    return {
        "top": [{"label": spellings[key].most_common(1)[0][0], "key": key, "count": n} for key, n in ranked],
        "distinct": len(totals),
        "total": total,
    }


def build_usage_headlines(families: List[UsageFamily], present: Iterable[str],
                          title_counts: List[ValueCount], author_counts: List[ValueCount]) -> List[Dict]:
    """Each family's headline over the period: how many of its main items
    (the first ranking's) and how many distinct things they were about."""
    present = set(present)
    headlines = []
    for family in families:
        ranking = family.rankings[0]
        types = set(ranking.types(present))
        counts = author_counts if ranking.column == "author" else title_counts
        figures = build_ranking((c for c in counts if c[0] in types), ranking.by, limit=0)
        headlines.append({
            "key": family.key, "label": family.label, "icon": family.icon, "category": family.category,
            "count": figures["total"], "distinct": figures["distinct"], "noun": ranking.noun,
        })
    return headlines


def _leaders_trend(
    counts_in: Iterable[Tuple[datetime, Optional[str], int]], granularity: str, leaders: List[Dict],
) -> Dict:
    """A ranking's leaders over time, from pre-aggregated (timestamp, key,
    count) rows: one series per entity among the first TOP_SERIES of the WHOLE
    history (`leaders`, so an entity keeps its colour whatever the period),
    every other one folded into "Autres" -- hidden at first, since a long tail
    would flatten the leaders -- with `other` set rather than listing hundreds
    of keys. The count is what each view counts: items for a usage ranking,
    plates eaten for Seazon's dishes."""
    keys = [leader["key"] for leader in leaders[:TOP_SERIES]]
    labels = {leader["key"]: leader["label"] for leader in leaders}
    timeline = build_count_series(counts_in, granularity, keys)
    for series in timeline["series"]:
        if series["label"] == OTHER_LABEL:
            series.update(keys=[], other=True, hidden=True)
        else:
            series["label"] = labels.get(series["label"], series["label"])
    timeline["leader_keys"] = keys
    return timeline


def build_usage_trend(events: List[Tuple[datetime, Optional[str]]], granularity: str, leaders: List[Dict]) -> Dict:
    """A ranking's leaders over time, one item per event -- the weighted form
    is _leaders_trend, which this is the n=1 case of."""
    return _leaders_trend(
        ((ts, usage_key(value), 1) for ts, value in events if value and value.strip()), granularity, leaders,
    )


def build_hour_profile(counts: Iterable[Tuple[Optional[str], int, int]], ranking: List[str]) -> List[Dict]:
    """Items per hour of the day, per category -- the first TOP_SERIES of
    `ranking`, the rest in one "Autres" row: [{label, keys, counts, max,
    total, peak}] in ranking order. Each row is read against its own
    maximum: the question is when each kind of activity happens, not which
    is biggest (the other charts answer that)."""
    slots = series_slots(ranking)
    rows: Dict[str, Dict] = {}
    for category, hour, n in counts:
        name = _fold(category or "Autre", slots)
        row = rows.setdefault(name, {"label": name, "keys": set(), "counts": [0] * 24})
        if category:
            row["keys"].add(category)
        row["counts"][hour] += n
    ordered = sorted(rows.values(), key=lambda r: slots.get(r["label"], TOP_SERIES))
    for row in ordered:
        row["keys"] = sorted(row["keys"])
        row["max"] = max(row["counts"])
        row["total"] = sum(row["counts"])
        row["peak"] = row["counts"].index(row["max"])
    return ordered


def build_coverage(rows: Iterable[Tuple[Optional[str], Optional[str], int, Optional[datetime], Optional[datetime]]],
                   ranking: List[str], limit: int = COVERAGE_ROWS) -> Optional[Dict]:
    """How far back each service's history goes: one bar per `context` (the
    service that logged the items), from its first dated item to its last,
    coloured by the category most of its items fall in, the busiest
    services first. {entries, ticks, legend, hidden}, `hidden` counting the
    services past `limit`. rows: (context, category, items, first, last)."""
    slots = series_slots(ranking)
    services: Dict[str, Dict] = {}
    for context, category, n, first, last in rows:
        if not context:
            continue
        service = services.setdefault(context, {"label": context, "count": 0, "first": None, "last": None,
                                                "categories": Counter()})
        service["count"] += n
        service["categories"][category] += n
        if first and (service["first"] is None or first < service["first"]):
            service["first"] = first
        if last and (service["last"] is None or last > service["last"]):
            service["last"] = last
    dated = sorted((s for s in services.values() if s["first"]), key=lambda s: (-s["count"], s["label"]))
    shown = dated[:limit]
    if not shown:
        return None
    for service in shown:
        category = service.pop("categories").most_common(1)[0][0]
        service.update(category=category, slot=slots.get(category, "other"),
                       start=service["first"], end=service["last"] + timedelta(days=1))
    ticks = _place_on_year_axis(shown)
    present = {s["category"] for s in shown}
    legend = [{"label": c, "slot": slot} for c, slot in sorted(slots.items(), key=lambda kv: kv[1]) if c in present]
    if any(s["slot"] == "other" for s in shown):
        legend.append({"label": OTHER_LABEL, "slot": "other"})
    return {"entries": shown, "ticks": ticks, "legend": legend, "hidden": len(dated) - len(shown)}
