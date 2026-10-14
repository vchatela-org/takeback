import json
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple, Union

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, aliased

from app import importers
from app.models import ImportBatch, Item, ImportStatus, MediaFile, SyncRun
from app.stats import NETWORK_CATEGORIES

# A filter value that may be given once or several times in a query string
# (e.g. ?category=a&category=b) -- both shapes are accepted everywhere.
OneOrMany = Union[None, str, Sequence[str]]

# Exports write a missing date as timestamp 0, which reads back as
# 01/01/1970 (31/12/1969 west of Greenwich). No platform here existed then:
# the dashboards take any earlier date for "no date" -- left out of charts,
# heatmaps, year presets and covered periods -- unless asked to keep it
# (`include_1970`). The item itself stays, and is still counted and found.
NO_DATE_BEFORE = datetime(1971, 1, 1)


def _real_timestamp():
    """Item.timestamp, NULL when it is a 1970 placeholder -- for MIN/MAX
    over a query that must still count every item."""
    return case((Item.timestamp >= NO_DATE_BEFORE, Item.timestamp))


def _real_dates_only(query, include_1970: bool = False):
    """Restrict a query to dated items, placeholders out unless asked."""
    if include_1970:
        return query.filter(Item.timestamp.isnot(None))
    return query.filter(Item.timestamp >= NO_DATE_BEFORE)


def _undate(rows, include_1970: bool = False) -> list:
    """Row fetchers' results (timestamp second) with a 1970 placeholder
    read as no date: the dashboards then treat the row like any undated
    one -- counted in totals, kept off the time axis."""
    if include_1970:
        return rows
    return [
        (row[0], None, *row[2:]) if row[1] is not None and row[1] < NO_DATE_BEFORE else row
        for row in rows
    ]


def _as_list(value: OneOrMany) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return [v for v in value if v]


def _scope(query, source: Optional[str] = None, batch_id: Optional[int] = None):
    """Restrict a query to one import (every item it holds, duplicates of
    other imports included) or, without a batch_id, to the deduplicated view
    across all imports -- optionally of one platform. Items whose batch is
    still being parsed have is_duplicate NULL and stay out of the latter
    until their batch is finalized (see app/dedup.py)."""
    if batch_id:
        query = query.filter(Item.batch_id == batch_id)
    else:
        query = query.filter(Item.is_duplicate.is_(False))
    if source:
        query = query.filter(Item.source == source)
    return query


# ────────────────────────────────────────────────
# ImportBatch
# ────────────────────────────────────────────────

def create_batch(db: Session, original_filename: str) -> ImportBatch:
    batch = ImportBatch(original_filename=original_filename, status=ImportStatus.pending)
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def get_batch(db: Session, batch_id: int) -> Optional[ImportBatch]:
    return db.get(ImportBatch, batch_id)


def get_batches(db: Session, source: Optional[str] = None) -> List[ImportBatch]:
    query = db.query(ImportBatch)
    if source:
        query = query.filter(ImportBatch.source == source)
    return query.order_by(ImportBatch.created_at.desc()).all()


def delete_batch(db: Session, batch_id: int) -> Optional[str]:
    """Deletes a batch and its items; returns its source (so the caller can
    re-elect the kept copies of that platform's duplicates), or None if
    there was no such batch. Bulk deletes, not the ORM cascade, which would
    load every item of a large export into memory just to delete it."""
    batch = db.get(ImportBatch, batch_id)
    if not batch:
        return None
    source = batch.source or ""
    item_ids = select(Item.id).where(Item.batch_id == batch_id)
    db.query(MediaFile).filter(MediaFile.item_id.in_(item_ids)).delete(synchronize_session=False)
    db.query(Item).filter(Item.batch_id == batch_id).delete(synchronize_session=False)
    db.query(ImportBatch).filter(ImportBatch.id == batch_id).delete(synchronize_session=False)
    db.commit()
    return source


def get_batch_coverage(db: Session, source: str) -> Dict[int, dict]:
    """Per done batch of one platform: items held, how many of them the
    platform views keep (the rest are already in a more recent import), and
    the period its dated items span."""
    rows = (
        db.query(
            Item.batch_id, func.count(Item.id),
            func.sum(case((Item.is_duplicate.is_(False), 1), else_=0)),
            func.min(_real_timestamp()), func.max(_real_timestamp()),
        )
        .filter(Item.source == source)
        .group_by(Item.batch_id)
        .all()
    )
    return {
        batch_id: {"count": count, "kept": kept or 0, "first": first, "last": last}
        for batch_id, count, kept, first, last in rows
    }


def get_batch_stats(db: Session, batch_id: int) -> dict:
    """Items held by one batch, the period they span, and how many exist in
    no other import of the same platform (what this import brought that no
    other one has)."""
    count, first, last = (
        db.query(func.count(Item.id), func.min(_real_timestamp()), func.max(_real_timestamp()))
        .filter(Item.batch_id == batch_id)
        .one()
    )
    other = aliased(Item)
    elsewhere = (
        db.query(other.id)
        .filter(other.source == Item.source, other.fingerprint == Item.fingerprint, other.batch_id != Item.batch_id)
        .exists()
    )
    unique = db.query(func.count(Item.id)).filter(Item.batch_id == batch_id, ~elsewhere).scalar() or 0
    return {"count": count or 0, "first": first, "last": last, "unique": unique}


def count_copies(db: Session, item: Item) -> int:
    """How many imports hold this same item (1 when it's in only one)."""
    if not item.fingerprint:
        return 1
    return (
        db.query(func.count(Item.id))
        .filter(Item.source == item.source, Item.fingerprint == item.fingerprint)
        .scalar()
        or 1
    )


# ────────────────────────────────────────────────
# Platforms (every import of one source, deduplicated)
# ────────────────────────────────────────────────

def get_platform_summaries(db: Session) -> Dict[str, dict]:
    """source -> {count, first, last, imports, last_import} for every source
    that has at least one finished import."""
    summaries: Dict[str, dict] = {}
    for source, count, first, last in (
        db.query(Item.source, func.count(Item.id), func.min(_real_timestamp()), func.max(_real_timestamp()))
        .filter(Item.is_duplicate.is_(False))
        .group_by(Item.source)
        .all()
    ):
        summaries[source] = {"count": count, "first": first, "last": last, "imports": 0, "last_import": None}
    for source, imports, last_import in (
        db.query(ImportBatch.source, func.count(ImportBatch.id), func.max(ImportBatch.created_at))
        .filter(ImportBatch.status == ImportStatus.done, ImportBatch.source.isnot(None))
        .group_by(ImportBatch.source)
        .all()
    ):
        summary = summaries.setdefault(source, {"count": 0, "first": None, "last": None})
        summary["imports"] = imports
        summary["last_import"] = last_import
    return summaries


def get_platform_summary(db: Session, source: str) -> dict:
    count, first, last = (
        _scope(db.query(func.count(Item.id), func.min(_real_timestamp()), func.max(_real_timestamp())), source=source)
        .one()
    )
    imports, last_import = (
        db.query(func.count(ImportBatch.id), func.max(ImportBatch.created_at))
        .filter(ImportBatch.status == ImportStatus.done, ImportBatch.source == source)
        .one()
    )
    return {"count": count or 0, "first": first, "last": last, "imports": imports or 0, "last_import": last_import}


def get_item_types_by_source(db: Session, source: Optional[str] = None) -> Dict[str, set]:
    """source -> item types it holds (deduplicated view) -- what decides
    which dashboards a platform offers."""
    result: Dict[str, set] = {}
    for src, item_type in _scope(db.query(Item.source, Item.item_type), source=source).distinct().all():
        result.setdefault(src, set()).add(item_type)
    return result


def get_sources_with_data(db: Session) -> List[str]:
    rows = (
        db.query(ImportBatch.source)
        .filter(ImportBatch.status == ImportStatus.done, ImportBatch.source.isnot(None))
        .distinct()
        .all()
    )
    return [r[0] for r in rows]


def count_1970(db: Session, source: str) -> int:
    """Items of a platform dated with the 1970 placeholder (NO_DATE_BEFORE)
    -- what the "include 1970" switch would bring back, shown only when
    there are any."""
    return (
        _scope(db.query(func.count(Item.id)), source=source)
        .filter(Item.timestamp < NO_DATE_BEFORE)
        .scalar()
        or 0
    )


def get_years(db: Session, source: str, item_types: OneOrMany = None, include_1970: bool = False) -> List[int]:
    """Calendar years that have at least one dated item, most recent first --
    the one-click period presets on the platform pages."""
    query = _real_dates_only(_scope(db.query(func.strftime("%Y", Item.timestamp)), source=source), include_1970)
    types = _as_list(item_types)
    if types:
        query = query.filter(Item.item_type.in_(types))
    years = []
    for (year,) in query.distinct().all():
        try:
            years.append(int(year))
        except (TypeError, ValueError):
            continue
    return sorted(years, reverse=True)


# ────────────────────────────────────────────────
# Item
# ────────────────────────────────────────────────

def get_item(db: Session, item_id: int) -> Optional[Item]:
    return db.get(Item, item_id)


def get_sources(db: Session) -> List[str]:
    rows = db.query(Item.source).distinct().order_by(Item.source).all()
    return [r[0] for r in rows]


def get_item_types(db: Session, source: Optional[str] = None, category: Optional[str] = None) -> List[str]:
    query = db.query(Item.item_type).distinct()
    if source:
        query = query.filter(Item.source == source)
    if category:
        query = query.filter(Item.category == category)
    return sorted(r[0] for r in query.all())


def get_categories(db: Session, batch_id: Optional[int] = None, source: Optional[str] = None) -> List[str]:
    query = db.query(Item.category).filter(Item.category.isnot(None)).distinct()
    if batch_id:
        query = query.filter(Item.batch_id == batch_id)
    if source:
        query = query.filter(Item.source == source)
    return sorted(r[0] for r in query.all())


def get_category_counts(
    db: Session, batch_id: Optional[int] = None, source: Optional[str] = None,
) -> List[Tuple[Optional[str], str, int]]:
    """(category, item_type, count) rows for one batch or one platform, used
    to drive the "what's in here and how much" overviews."""
    rows = (
        _scope(db.query(Item.category, Item.item_type, func.count(Item.id)), source=source, batch_id=batch_id)
        .group_by(Item.category, Item.item_type)
        .order_by(Item.category, Item.item_type)
        .all()
    )
    return [(row[0], row[1], row[2]) for row in rows]


# What the generic activity charts can split a platform's items by.
ACTIVITY_DIMENSIONS = {"category": Item.category, "item_type": Item.item_type}


def get_dated_counts(
    db: Session, source: str, by: str = "category", categories: OneOrMany = None, include_1970: bool = False,
) -> List[Tuple[str, int]]:
    """(category or item type, dated items) of a platform, most first, over
    its whole history -- the content filter's choices, and the stable order
    chart colors are assigned in, so narrowing the period never repaints a
    series (narrowing the content does: what's left out frees its color)."""
    column = ACTIVITY_DIMENSIONS[by]
    query = _real_dates_only(_filter_items(db.query(column, func.count(Item.id)), source=source, category=categories),
                             include_1970)
    rows = query.group_by(column).order_by(func.count(Item.id).desc()).all()
    return [(row[0] or "Autre", row[1]) for row in rows]


def _filter_items(
    query,
    source: Optional[str] = None,
    category: OneOrMany = None,
    item_type: OneOrMany = None,
    batch_id: Optional[int] = None,
    context: Optional[str] = None,
    title: OneOrMany = None,
    q: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    weekday: Optional[int] = None,
    hour: Optional[int] = None,
    author: OneOrMany = None,
    not_author: Optional[str] = None,
    device: OneOrMany = None,
    group_chat: Optional[bool] = None,
    include_1970: bool = False,
):
    query = _scope(query, source=source, batch_id=batch_id)
    categories = _as_list(category)
    if categories:
        query = query.filter(Item.category.in_(categories))
    item_types = _as_list(item_type)
    if item_types:
        query = query.filter(Item.item_type.in_(item_types))
    if context:
        query = query.filter(Item.context == context)
    titles = _as_list(title)
    if titles:
        query = query.filter(Item.title.in_(titles))
    authors = _as_list(author)
    if authors:
        query = query.filter(Item.author.in_(authors))
    devices = _as_list(device)
    if devices:
        # Kindle items name their device in fields_json ("Appareil").
        query = query.filter(or_(*(
            Item.fields_json.contains('"Appareil": ' + json.dumps(d, ensure_ascii=False), autoescape=True)
            for d in devices
        )))
    if not_author:
        query = query.filter((Item.author != not_author) | Item.author.is_(None))
    if group_chat is not None:
        groups = _group_chat_contexts(source)
        query = query.filter(Item.context.in_(groups) if group_chat else ~Item.context.in_(groups))
    if date_from:
        query = query.filter(Item.timestamp >= date_from)
    if date_to:
        query = query.filter(Item.timestamp <= date_to)
    if (weekday is not None or hour is not None) and not include_1970:
        # A 1970 placeholder falls on no real weekday or hour.
        query = query.filter(Item.timestamp >= NO_DATE_BEFORE)
    if weekday is not None:
        # SQLite's %w counts from Sunday=0; the heatmap (Python's
        # weekday()) from Monday=0.
        query = query.filter(func.strftime("%w", Item.timestamp) == str((weekday + 1) % 7))
    if hour is not None:
        # A date without a time of day (exactly midnight, see
        # stats.has_time_of_day) has no hour: the heatmaps leave it out.
        query = query.filter(
            func.strftime("%H", Item.timestamp) == f"{hour:02d}",
            func.strftime("%H:%M:%S", Item.timestamp) != "00:00:00",
        )
    if q:
        like = f"%{q}%"
        query = query.filter(
            (Item.content.ilike(like))
            | (Item.title.ilike(like))
            | (Item.author.ilike(like))
        )
    return query


def search_items(
    db: Session,
    page: int = 1,
    page_size: int = 30,
    **filters,
) -> Tuple[List[Item], int]:
    query = _filter_items(db.query(Item), **filters)

    total = query.with_entities(func.count(Item.id)).scalar() or 0

    items = (
        query.order_by(Item.timestamp.desc().nullslast(), Item.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return items, total


def get_items_for_column_table(db: Session, limit: int = 5000, **filters) -> Tuple[List[Item], int]:
    """Like search_items, but unpaginated (up to `limit` rows) so the caller
    can sort/paginate in Python by an arbitrary user-picked field column --
    something SQLite can't do generically since fields live in a JSON blob.
    Also returns the true total so the caller can detect an over-`limit`
    result set and fall back to the plain paginated view instead."""
    query = _filter_items(db.query(Item), **filters)
    total = query.with_entities(func.count(Item.id)).scalar() or 0
    items = query.order_by(Item.timestamp.desc().nullslast(), Item.id.desc()).limit(limit).all()
    return items, total


def get_items_by_ids(db: Session, ids: Sequence[int]) -> List[Item]:
    """These items, in the order given -- for drill-down panels whose
    selection is computed in Python (a reply-delay band, a post's media
    kind) rather than expressible as a filter. The caller sorts and caps
    the selection first: one bound parameter per id, and SQLite limits
    how many a statement can take."""
    by_id = {item.id: item for item in db.query(Item).filter(Item.id.in_(list(ids))).all()} if ids else {}
    return [by_id[i] for i in ids if i in by_id]


def get_titles_by_asin(db: Session, source: str) -> Dict[str, str]:
    """ASIN (or personal document id) -> title, from every purchase line and
    Kindle library entry of a platform. A Kindle item whose own export names
    no title is grouped under the book's id; the orders of that same book,
    or another import's library, carry its title."""
    titles: Dict[str, str] = {}
    for content, fields_json in (
        _scope(db.query(Item.content, Item.fields_json), source=source)
        .filter(
            Item.item_type.in_(("order_item", "digital_order", "audible_order", "kindle_book", "kindle_document")),
            Item.content.isnot(None),
        )
        .all()
    ):
        if not fields_json:
            continue
        fields = json.loads(fields_json)
        content_id = fields.get("ASIN") or fields.get("Document")
        if content_id and content != content_id:
            titles.setdefault(content_id, content)
    return titles


def iter_export_rows(
    db: Session, item_types: Optional[Sequence[str]] = None, sources: Optional[Sequence[str]] = None,
    details: bool = False,
):
    """Every dated, deduplicated item (of these types, these platforms),
    oldest first, streamed: (id, source, category, item_type, timestamp)
    and, with `details`, (context, content, fields_json) -- what the InfluxDB
    export (app/influx_export.py) turns into points. Never raw_json. Ties
    broken by id: the same items always come in the same order, whether
    one platform or all are read."""
    columns = [Item.id, Item.source, Item.category, Item.item_type, Item.timestamp]
    if details:
        columns += [Item.context, Item.content, Item.fields_json]
    query = _scope(db.query(*columns)).filter(Item.timestamp >= NO_DATE_BEFORE)
    if item_types is not None:
        query = query.filter(Item.item_type.in_(list(item_types)))
    if sources:
        query = query.filter(Item.source.in_(list(sources)))
    return query.order_by(Item.timestamp.asc(), Item.id.asc()).yield_per(5000)


def get_sync_runs(db: Session, kind: Optional[str] = None, limit: int = 20) -> List[SyncRun]:
    """The latest sink runs (app/scheduler.py), most recent first."""
    query = db.query(SyncRun)
    if kind:
        query = query.filter(SyncRun.kind == kind)
    return query.order_by(SyncRun.id.desc()).limit(limit).all()


def get_library_rows(
    db: Session, source: str, item_types: Sequence[str], include_1970: bool = False,
) -> List[Tuple[int, Optional[datetime], str, Optional[str], Optional[str], Optional[str], Optional[str]]]:
    """(id, timestamp, item_type, context, content, author, fields_json) of a
    platform's items of these types, the whole history -- the library groups
    them by book (context) and needs the author get_dashboard_rows lacks."""
    query = _scope(
        db.query(Item.id, Item.timestamp, Item.item_type, Item.context, Item.content, Item.author, Item.fields_json),
        source=source,
    ).filter(Item.item_type.in_(list(item_types)))
    return _undate(query.order_by(Item.timestamp.asc()).all(), include_1970)


# ────────────────────────────────────────────────
# Conversations (item_type == "message", grouped by thread == context)
# ────────────────────────────────────────────────

def get_threads(db: Session, source: str) -> List[dict]:
    """One row per conversation thread: name, message count, last message
    preview/time. A handful of aggregate queries total regardless of thread
    count -- the per-thread "last message" used to be fetched one query at a
    time, which was the main reason this page was slow on large exports.

    "Last" means highest `timestamp`, NOT highest `id`: Facebook/Instagram
    export their message JSON newest-first, so insertion order runs backward
    through time and id-based ordering picked the OLDEST message instead.

    Across every import of the platform: a thread present in several yearly
    exports is ONE thread here, holding each of its messages once."""
    counts = dict(
        _scope(db.query(Item.context, func.count(Item.id)), source=source)
        .filter(Item.item_type == "message")
        .group_by(Item.context)
        .all()
    )

    latest_ts_subq = (
        _scope(db.query(Item.context, func.max(Item.timestamp).label("max_ts")), source=source)
        .filter(Item.item_type == "message")
        .group_by(Item.context)
        .subquery()
    )
    # Only the couple of columns the preview line actually shows (author,
    # content) -- fetching full Item ORM rows here used to also pull
    # raw_json/fields_json (the full original JSON fragment) for every
    # thread's last message on every single page load of this tab, which on
    # an export with many/large threads dwarfed the rest of the query.
    # The source/is_duplicate filter here is not just an optimization:
    # without it the join matches purely on (context, timestamp), so the
    # preview could come from a hidden duplicate or from another platform's
    # thread of the same name.
    last_messages = (
        _scope(db.query(Item.context, Item.timestamp, Item.author, Item.content), source=source)
        .filter(Item.item_type == "message")
        .join(
            latest_ts_subq,
            (Item.context == latest_ts_subq.c.context) & (Item.timestamp == latest_ts_subq.c.max_ts),
        )
        .all()
    )
    # A tie (two messages sharing the exact same timestamp in one thread) can
    # match more than once -- keep just the first per thread.
    last_by_context = {}
    for context, timestamp, author, content in last_messages:
        last_by_context.setdefault(context, {"timestamp": timestamp, "author": author, "content": content})

    threads = [
        {
            "context": context,
            "count": count,
            "last_timestamp": last_by_context.get(context, {}).get("timestamp"),
            "last_message": last_by_context.get(context),
        }
        for context, count in counts.items()
    ]
    threads.sort(key=lambda t: t["last_timestamp"] or datetime.min, reverse=True)
    return threads


def get_me_candidate(db: Session, source: str) -> Optional[str]:
    """Best-effort "who is the account owner": the sender appearing across
    the most distinct threads, computed as one aggregate query instead of
    walking every message of every thread in Python -- unless the platform's
    export names its owner (BaseImporter.owner_item_type), which is taken
    over the guess: in an inbox of messages only received (Reddit's, from
    Reddit and bots) the guess can only land on one of their senders."""
    importer = importers.get(source)
    owner_type = importer.owner_item_type if importer else None
    if owner_type:
        declared = (
            _scope(db.query(Item.title), source=source)
            .filter(Item.item_type == owner_type, Item.title.isnot(None))
            .order_by(Item.batch_id.desc(), Item.id.desc())
            .first()
        )
        if declared:
            return declared[0]
    row = (
        _scope(db.query(Item.author, func.count(func.distinct(Item.context)).label("thread_count")), source=source)
        .filter(Item.item_type == "message", Item.author.isnot(None))
        .group_by(Item.author)
        .order_by(func.count(func.distinct(Item.context)).desc())
        .first()
    )
    return row[0] if row else None


def get_thread_message_page(
    db: Session, source: str, context: Optional[str], offset: int = 0, limit: int = 50,
) -> Tuple[List[Item], bool]:
    """One page of a thread's messages, ordered by real timestamp (not id --
    see get_threads for why that matters) descending internally so LIMIT
    grabs the most recent end, then reversed for oldest-first chat display.
    `offset` pages further back in history -- this is what makes opening a
    long conversation fast: only the most recent `limit` messages load at
    first, older ones load on demand as the user asks for them."""
    query = _scope(db.query(Item), source=source).filter(Item.item_type == "message")
    query = query.filter(Item.context == context) if context else query.filter(Item.context.is_(None))

    rows = (
        query.order_by(Item.timestamp.desc().nullslast(), Item.id.desc())
        .offset(offset)
        .limit(limit + 1)
        .all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    rows.reverse()
    return rows, has_more


# ────────────────────────────────────────────────
# Time-activity stats (app/stats.py does the bucketing/heatmap/estimate
# math on the raw pairs this returns)
# ────────────────────────────────────────────────

def get_activity_events(
    db: Session, source: str, date_from: Optional[datetime] = None, date_to: Optional[datetime] = None,
    categories: OneOrMany = None, by: str = "category", include_1970: bool = False,
) -> List[Tuple[datetime, Optional[str]]]:
    """(timestamp, category or item type) for every dated item of a
    platform, optionally restricted to a range/categories -- only the two
    columns app/stats.py actually needs, not full Item rows (same reasoning
    as get_threads: this used to matter a lot once raw_json/fields_json got
    pulled in for no reason)."""
    query = _filter_items(
        db.query(Item.timestamp, ACTIVITY_DIMENSIONS[by]), source=source, category=categories,
        date_from=date_from, date_to=date_to,
    )
    return _real_dates_only(query, include_1970).order_by(Item.timestamp.asc()).all()


def get_activity_counts(
    db: Session, source: str, granularity: str, categories: OneOrMany = None, include_1970: bool = False,
) -> List[Tuple[datetime, Optional[str], int]]:
    """(start of the year or month, category, dated items) of a platform:
    the overview's chart, counted by SQLite instead of loading one row per
    item as the activity page does -- the overview is every platform's
    landing page, and has to stay quick on a large export."""
    fmt = "%Y" if granularity == "year" else "%Y-%m"
    period = func.strftime(fmt, Item.timestamp)
    query = _real_dates_only(
        _filter_items(db.query(period, Item.category, func.count(Item.id)), source=source, category=categories),
        include_1970,
    )
    counts = []
    for key, category, count in query.group_by(period, Item.category).all():
        if not key:
            continue
        year, _, month = key.partition("-")
        counts.append((datetime(int(year), int(month or 1), 1), category, count))
    return counts


def get_dashboard_rows(
    db: Session, source: str, item_types: List[str],
    date_from: Optional[datetime] = None, date_to: Optional[datetime] = None, include_1970: bool = False,
) -> List[Tuple[int, Optional[datetime], str, Optional[str], Optional[str], Optional[str]]]:
    """(id, timestamp, item_type, context, content, fields_json) for a
    platform's items of the given types -- the raw material for the
    reading/purchase dashboards (which need to sum a specific numeric field
    out of `fields_json` and, for purchases, count DISTINCT `context` values
    rather than rows, since one order spans several item rows). Timestamp is
    NOT filtered non-null here (unlike get_activity_events): a summary total
    (e.g. "62 retours") should still count a row with no date, even though
    it can't be placed on the time-series chart -- app/stats.py's bucketing
    functions already skip timestamp=None entries on their own."""
    query = _scope(
        db.query(Item.id, Item.timestamp, Item.item_type, Item.context, Item.content, Item.fields_json),
        source=source,
    ).filter(Item.item_type.in_(item_types))
    if date_from:
        query = query.filter(Item.timestamp >= date_from)
    if date_to:
        query = query.filter(Item.timestamp <= date_to)
    return _undate(query.order_by(Item.timestamp.asc()).all(), include_1970)


def get_titled_rows(
    db: Session, source: str, item_types: List[str],
    date_from: Optional[datetime] = None, date_to: Optional[datetime] = None, include_1970: bool = False,
) -> List[Tuple[int, Optional[datetime], Optional[str], Optional[str]]]:
    """(id, timestamp, title, fields_json) -- for a dashboard that groups items
    by their own name AND reads a number out of their fields at the same time.
    get_dashboard_rows can't serve those: it carries the content, not the
    title. Seazon's dishes are the case (one `meal` item per dish, whose
    portions and box state live in the fields). Same timestamp/date handling
    as get_dashboard_rows."""
    query = _scope(
        db.query(Item.id, Item.timestamp, Item.title, Item.fields_json), source=source,
    ).filter(Item.item_type.in_(item_types))
    if date_from:
        query = query.filter(Item.timestamp >= date_from)
    if date_to:
        query = query.filter(Item.timestamp <= date_to)
    return _undate(query.order_by(Item.timestamp.asc()).all(), include_1970)


def get_message_rows(
    db: Session, source: str, include_1970: bool = False,
) -> List[Tuple[int, Optional[datetime], Optional[str], Optional[str]]]:
    """(id, timestamp, author, context) of every message of a platform, the
    whole history -- what the messaging dashboard needs to split sent from
    received, find when each conversation started and pair replies with the
    message they answer. Four narrow columns, so even a few hundred thousand
    messages stay cheap to load."""
    return _undate(
        _scope(db.query(Item.id, Item.timestamp, Item.author, Item.context), source=source)
        .filter(Item.item_type == "message")
        .order_by(Item.timestamp.asc())
        .all(),
        include_1970,
    )


def _group_chat_contexts(source: Optional[str]):
    """The conversations more than two people wrote in, as a subquery --
    the same rule as stats.group_threads, for a drill-down panel that
    filters in SQL."""
    query = (
        select(Item.context)
        .where(Item.item_type == "message", Item.is_duplicate.is_(False), Item.author.isnot(None), Item.author != "")
        .group_by(Item.context)
        .having(func.count(func.distinct(Item.author)) > 2)
    )
    return query.where(Item.source == source) if source else query


def get_connection_rows(
    db: Session, source: str, item_types: OneOrMany = None, include_1970: bool = False,
) -> List[Tuple[int, Optional[datetime], int, str, Optional[str], Optional[str]]]:
    """(id, timestamp, batch_id, item_type, title, fields_json) of a
    platform's relationship records (followers, following, friends,
    connections...): every item of its network category, or only the given
    types."""
    query = _scope(
        db.query(Item.id, Item.timestamp, Item.batch_id, Item.item_type, Item.title, Item.fields_json),
        source=source,
    )
    types = _as_list(item_types)
    query = query.filter(Item.item_type.in_(types)) if types else query.filter(Item.category.in_(NETWORK_CATEGORIES))
    return _undate(query.order_by(Item.timestamp.asc()).all(), include_1970)


def get_career_rows(
    db: Session, source: str, item_types: Sequence[str], include_1970: bool = False,
) -> List[Tuple[int, Optional[datetime], str, Optional[str], Optional[str], Optional[str], Optional[str]]]:
    """(id, timestamp, item_type, title, author, context, fields_json) of a
    platform's career records (positions, studies, endorsements...), the
    whole history -- the Parcours tab cuts its period out in Python."""
    return _undate(
        _scope(
            db.query(Item.id, Item.timestamp, Item.item_type, Item.title, Item.author, Item.context, Item.fields_json),
            source=source,
        )
        .filter(Item.item_type.in_(list(item_types)))
        .order_by(Item.timestamp.asc())
        .all(),
        include_1970,
    )


# ────────────────────────────────────────────────
# Usages (Google): rankings and rhythms over item titles/authors
# ────────────────────────────────────────────────

_VALUE_COLUMNS = {"title": Item.title, "author": Item.author}


def get_value_counts(
    db: Session, source: str, item_types: Sequence[str], column: str,
    date_from: Optional[datetime] = None, date_to: Optional[datetime] = None,
) -> List[Tuple[str, Optional[str], int]]:
    """(item_type, title or author, items) over a period, NULL included --
    what a usage ranking counts. Grouped by SQLite: a ranking reads tens of
    thousands of items, but only a few thousand distinct values."""
    value = _VALUE_COLUMNS[column]
    query = _filter_items(
        db.query(Item.item_type, value, func.count(Item.id)), source=source, item_type=list(item_types),
        date_from=date_from, date_to=date_to,
    )
    return [(row[0], row[1], row[2]) for row in query.group_by(Item.item_type, value).all()]


def get_value_events(
    db: Session, source: str, item_types: Sequence[str], column: str,
    date_from: Optional[datetime] = None, date_to: Optional[datetime] = None, include_1970: bool = False,
    dated_only: bool = True,
) -> List[Tuple[int, Optional[datetime], Optional[str]]]:
    """(id, timestamp, title or author) of the items of the given types --
    the dated ones for a ranking's leaders over time, all of them for the
    items behind a ranking entry (picked with stats.select_usage, the rule
    get_value_counts' rows are ranked by)."""
    query = _filter_items(
        db.query(Item.id, Item.timestamp, _VALUE_COLUMNS[column]), source=source, item_type=list(item_types),
        date_from=date_from, date_to=date_to,
    )
    if dated_only:
        query = _real_dates_only(query, include_1970)
    return query.order_by(Item.timestamp.asc()).all()


def get_hour_counts(
    db: Session, source: str, date_from: Optional[datetime] = None, date_to: Optional[datetime] = None,
    include_1970: bool = False,
) -> List[Tuple[Optional[str], int, int]]:
    """(category, hour of the day, items) of a platform's items dated with
    a time of day (not exactly midnight, see stats.has_time_of_day)."""
    hour = func.strftime("%H", Item.timestamp)
    query = _real_dates_only(
        _filter_items(db.query(Item.category, hour, func.count(Item.id)), source=source,
                      date_from=date_from, date_to=date_to),
        include_1970,
    ).filter(func.strftime("%H:%M:%S", Item.timestamp) != "00:00:00")
    return [(category, int(h), n) for category, h, n in query.group_by(Item.category, hour).all() if h is not None]


def get_context_coverage(
    db: Session, source: str, include_1970: bool = False,
) -> List[Tuple[Optional[str], Optional[str], int, Optional[datetime], Optional[datetime]]]:
    """(context, category, items, first, last dated item) of a platform,
    whatever the period -- how far back each service's history goes."""
    first = func.min(Item.timestamp if include_1970 else _real_timestamp())
    last = func.max(Item.timestamp if include_1970 else _real_timestamp())
    rows = (
        _scope(db.query(Item.context, Item.category, func.count(Item.id), first, last), source=source)
        .group_by(Item.context, Item.category)
        .all()
    )
    return [tuple(row) for row in rows]


def get_active_days(
    db: Session, source: str, date_from: Optional[datetime] = None, date_to: Optional[datetime] = None,
    include_1970: bool = False,
) -> Tuple[int, int]:
    """(dated items, distinct days they fall on) of a platform over a period."""
    count, days = _real_dates_only(
        _filter_items(db.query(func.count(Item.id), func.count(func.distinct(func.date(Item.timestamp)))),
                      source=source, date_from=date_from, date_to=date_to),
        include_1970,
    ).one()
    return count or 0, days or 0


def get_batches_by_item_type(db: Session, source: str, item_types: Sequence[str]) -> Dict[str, List[int]]:
    """item_type -> ids of the imports holding at least one item of it,
    oldest first, duplicates included: a relationship list is a snapshot,
    and telling who left between two exports needs every export that had
    the list, even one whose entries all reappear in a later export."""
    result: Dict[str, List[int]] = {}
    for item_type, batch_id in (
        db.query(Item.item_type, Item.batch_id)
        .filter(Item.source == source, Item.item_type.in_(list(item_types)), Item.is_duplicate.isnot(None))
        .distinct()
        .order_by(Item.batch_id)
        .all()
    ):
        result.setdefault(item_type, []).append(batch_id)
    return result


def get_post_rows(db: Session, source: str, include_1970: bool = False) -> List[Tuple[int, Optional[datetime], Optional[str]]]:
    """(id, timestamp, content) of every post of a platform."""
    return _undate(
        _scope(db.query(Item.id, Item.timestamp, Item.content), source=source)
        .filter(Item.item_type == "post")
        .order_by(Item.timestamp.asc())
        .all(),
        include_1970,
    )


def get_post_media(db: Session, source: str) -> Dict[int, List[Tuple[str, str]]]:
    """post id -> [(media_type, URL path under /media)] in export order."""
    media: Dict[int, List[Tuple[str, str]]] = {}
    for item_id, batch_id, media_type, relative_path in (
        _scope(db.query(MediaFile.item_id, Item.batch_id, MediaFile.media_type, MediaFile.relative_path)
               .join(Item, MediaFile.item_id == Item.id), source=source)
        .filter(Item.item_type == "post")
        .order_by(MediaFile.id)
        .all()
    ):
        media.setdefault(item_id, []).append((media_type, f"/media/{batch_id}/{relative_path}"))
    return media
