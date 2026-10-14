"""What Takeback sends to InfluxDB 1.x for Grafana, declared in one table
(SERIES below) -- the single place to change when a new graph or
correlation needs data Grafana doesn't have yet. app/sinks/influxdb.py does
the writing; this module only turns items into line-protocol points.

Layout: one database per domain, like the homelab's other personal data
(BodyStats, SleepStats...): MediaStats (reading and listening), ShoppingStats
(purchases), DigitalStats (everything, as counts). Every point carries the
`source` tag (the tracker: kindle, audible, or the platform)
and, when configured, `user`, as the homelab's shared-database contract asks;
plus `platform` (the Takeback platform it comes from) and `via=takeback`,
which let a re-push drop exactly Takeback's own series in a database other
trackers may share (MediaStats is also where a listening-history sync from
another tool belongs, as source=<that tool>).

Privacy: InfluxDB has no authentication, Takeback sits behind a login.
Only numbers and low-cardinality tags leave -- never a message, a search, a
URL or a name. A text field exists only where a series declares it
(`str` in its field types): book titles, so Grafana can say
what was read -- a Kindle book named only by its ASIN gets the title its
orders or library give it (crud.get_titles_by_asin), and a personal
document (Send to Kindle: a CV, a payslip...) never leaves under its own
name, only as PERSONAL_DOCUMENT_TITLE. tests/test_influx_export.py fails
if any other text appears.

Field types are part of the contract: InfluxDB 1.x rejects a write whose
field type differs from what the shard already holds, so every field is
declared once (int / float / str) and always written the same way; an
undeclared field is an error, not a guess.

Points are aggregated per (measurement, tags, second), numbers summed: the
three lines of one order, two messages in the same second, would otherwise
overwrite each other (a point is keyed by measurement + tags + time). Rows
are read oldest first and a second's group is written as soon as the next
second starts, so memory stays flat whatever the history's size.

Times: naive timestamps are UTC (the importers write UTC, or the server's
local time, which is UTC in the deployed container). Items without a real
date (before crud.NO_DATE_BEFORE) are left out. A day-precision item (a
date without time, stats.has_time_of_day) is tagged `time_precision=day`
and gets no local hour.
"""
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app import crud, stats
from app.config import settings

VIA = "takeback"

_KIND_BY_PURCHASE = {
    "order_item": "retail", "digital_order": "digital", "audible_order": "audible",
    # Seazon's meal orders (app/importers/seazon.py): their own kind, so a
    # Grafana query on ShoppingStats can tell them from Amazon's lines --
    # `source` alone would do it, this keeps `kind` meaningful too.
    stats.SEAZON_ORDER_TYPE: "meal",
    # Zalando's clothing orders (app/importers/zalando.py), same reasoning.
    stats.ZALANDO_ORDER_TYPE: "fashion",
}
# Reading/listening item type -> (tracker, kind of media).
_READING_TRACKER = {
    "reading_session": ("kindle", "ebook"),
    "listening_session": ("audible", "audiobook"),
    "kindle_finished": ("kindle", "ebook"),
}
_TEXT_MAX = 250
# The `title` of a Kindle personal document: its real name stays in Takeback.
PERSONAL_DOCUMENT_TITLE = "Document personnel"

# Every point: (name, type).
COMMON_FIELDS = {"local_hour": int, "local_weekday": int}


class ExportRow:
    """One item as crud.iter_export_rows reads it, its fields parsed once."""

    __slots__ = ("id", "source", "category", "item_type", "timestamp", "context", "content", "_fields_json",
                 "_fields", "title")

    def __init__(self, row: Sequence[Any]):
        self.id, self.source, self.category, self.item_type, self.timestamp = row[:5]
        self.context, self.content, self._fields_json = (tuple(row[5:8]) + (None, None, None))[:3]
        self._fields = None
        # What a series may write as `title` (see series_lines).
        self.title = self.context

    @property
    def fields(self) -> Dict[str, Any]:
        if self._fields is None:
            self._fields = json.loads(self._fields_json) if self._fields_json else {}
        return self._fields


@dataclass(frozen=True)
class Series:
    database: str
    measurement: str
    # None: every item type.
    item_types: Optional[Tuple[str, ...]]
    tags: Callable[[ExportRow], Dict[str, Optional[str]]]
    fields: Callable[[ExportRow], Dict[str, Any]]
    # Every field the series may write, with its type; `str` opts a text
    # field in (see the module docstring).
    field_types: Dict[str, type]
    # int fields counting distinct values, each once over the whole series,
    # in the first point holding it (orders: an order whose lines fall in
    # two departments counts in one of them), so that sums stay exact.
    distinct: Dict[str, Callable[[ExportRow], Optional[str]]] = field(default_factory=dict)
    # Whether the rows need context/content/fields_json (not for counts).
    details: bool = True


def _reading_tags(row: ExportRow) -> Dict[str, Optional[str]]:
    tracker, kind = _READING_TRACKER[row.item_type]
    return {"source": tracker, "kind": kind, "device": row.fields.get("Appareil")}


def _reading_fields(row: ExportRow) -> Dict[str, Any]:
    pages = stats.extract_leading_number(row.fields.get("Pages tournées"))
    return {
        "minutes": stats.reading_minutes(row.item_type, row.fields),
        "sessions": 1,
        "pages": int(pages) if pages is not None else None,
        "title": row.title,
    }


def _purchase_tags(row: ExportRow) -> Dict[str, Optional[str]]:
    return {
        "source": row.source,
        "kind": _KIND_BY_PURCHASE.get(row.item_type),
        "department": stats.purchase_category(row.item_type, row.fields),
        "currency": stats.extract_currency(row.fields.get("Montant")),
    }


SERIES: List[Series] = [
    # Everything, as counts: any platform, category or item type can be
    # graphed (searches per day, messages by hour...) with no change here.
    Series(
        database="DigitalStats", measurement="item", item_types=None, details=False,
        tags=lambda row: {"source": row.source, "category": row.category, "item_type": row.item_type},
        fields=lambda row: {"count": 1},
        field_types={"count": int},
    ),
    # Reading and listening sessions, real durations (Kindle, Audible): one
    # tracker per `source`, `kind` ebook/audiobook.
    Series(
        database="MediaStats", measurement="reading_session", item_types=tuple(stats.READING_ITEM_TYPES),
        tags=_reading_tags, fields=_reading_fields,
        field_types={"minutes": float, "sessions": int, "pages": int, "title": str},
    ),
    Series(
        database="MediaStats", measurement="book_finished", item_types=(stats.KINDLE_FINISHED_TYPE,),
        tags=lambda row: {"source": _READING_TRACKER[row.item_type][0], "kind": _READING_TRACKER[row.item_type][1]},
        fields=lambda row: {"count": 1, "title": row.title},
        field_types={"count": int, "title": str},
    ),
    # Purchase lines (Amazon), amounts in their own currency (never converted).
    Series(
        database="ShoppingStats", measurement="purchase", item_types=tuple(_KIND_BY_PURCHASE),
        tags=_purchase_tags,
        fields=lambda row: {"amount": stats.purchase_amount(row.fields), "lines": 1},
        field_types={"amount": float, "lines": int, "orders": int},
        # A line without an order id is an order of its own, as on the
        # purchases page.
        distinct={"orders": lambda row: row.context or f"#{row.id}"},
    ),
]


def databases() -> List[str]:
    return sorted({series.database for series in SERIES})


# ────────────────────────────────────────────────
# Line protocol
# ────────────────────────────────────────────────

def _escape_key(value: str) -> str:
    """Measurement names, tag keys/values and field keys. A line break would
    end the line: it becomes a space first, escaped like any other."""
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text.replace("\\", "\\\\").replace(",", r"\,").replace("=", r"\=").replace(" ", r"\ ")


def _format_field(name: str, value: Any, kind: type) -> str:
    if kind is int:
        return f"{_escape_key(name)}={int(value)}i"
    if kind is float:
        return f"{_escape_key(name)}={float(value)!r}"
    if kind is str:
        text = str(value)[:_TEXT_MAX].replace("\r", " ").replace("\n", " ").replace("\\", "\\\\").replace('"', '\\"')
        return f'{_escape_key(name)}="{text}"'
    raise ValueError(f"unsupported field type {kind!r} for {name}")


def format_line(measurement: str, tags: Dict[str, str], fields: Dict[str, Any], types: Dict[str, type],
                epoch_seconds: int) -> str:
    """One point. Tags sorted (what InfluxDB recommends), empty ones left
    out (InfluxDB refuses an empty tag value); every field must be declared."""
    tag_part = "".join(f",{_escape_key(k)}={_escape_key(v)}" for k, v in sorted(tags.items()) if v not in (None, ""))
    field_parts = []
    for name, value in sorted(fields.items()):
        if name not in types:
            raise ValueError(f"field {name!r} of {measurement} is not declared")
        field_parts.append(_format_field(name, value, types[name]))
    return f"{_escape_key(measurement)}{tag_part} {','.join(field_parts)} {epoch_seconds}"


# ────────────────────────────────────────────────
# Items -> points
# ────────────────────────────────────────────────

@lru_cache(maxsize=4)
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def _local_fields(second: datetime) -> Dict[str, int]:
    local = second.replace(tzinfo=timezone.utc).astimezone(_zone(settings.influxdb_tz))
    return {"local_hour": local.hour, "local_weekday": local.isoweekday()}


def _is_personal_document(row: ExportRow) -> bool:
    # app/importers/amazon.py: a store book has an "ASIN", a personal
    # document a "Document" id (and, on a session, Type "Document personnel").
    return bool(row.fields.get("Document")) or row.fields.get("Type") == "Document personnel"


def _title(row: ExportRow, titles: Dict[str, str]) -> Optional[str]:
    if _is_personal_document(row):
        return PERSONAL_DOCUMENT_TITLE
    return titles.get(row.context, row.context) if row.context else None


def _flush(series: Series, second: datetime, group: Dict) -> Iterator[str]:
    epoch = int(second.replace(tzinfo=timezone.utc).timestamp())
    types = {**COMMON_FIELDS, **series.field_types}
    for acc in group.values():
        fields: Dict[str, Any] = dict(acc["numbers"])
        for name, values in acc["texts"].items():
            fields[name] = " / ".join(sorted(values))
        for name, values in acc["distinct"].items():
            fields[name] = len(values)
        fields.update(acc["common"])
        yield format_line(series.measurement, acc["tags"], fields, types, epoch)


def series_lines(db: Session, series: Series, sources: Optional[Sequence[str]] = None) -> Iterator[str]:
    """The points of one series, aggregated per second, oldest first."""
    current: Optional[datetime] = None
    group: Dict[Tuple, Dict] = {}
    counted: Dict[str, set] = defaultdict(set)
    titled = "title" in series.field_types
    # source -> its ASIN/document id -> title, read once per platform.
    titles: Dict[str, Dict[str, str]] = {}
    local_fields: Dict[str, int] = {}
    for raw in crud.iter_export_rows(db, series.item_types, sources, details=series.details):
        row = ExportRow(raw)
        second = row.timestamp.replace(microsecond=0)
        if second != current:
            if group:
                yield from _flush(series, current, group)
            current, group = second, {}
            local_fields = _local_fields(second)
        if titled:
            if row.source not in titles:
                titles[row.source] = crud.get_titles_by_asin(db, row.source)
            row.title = _title(row, titles[row.source])
        day_only = not stats.has_time_of_day(row.timestamp)
        common_tags = {"platform": row.source, "via": VIA, "user": settings.influxdb_user,
                       "time_precision": "day" if day_only else "second"}
        tags = {k: v for k, v in {**common_tags, **series.tags(row)}.items() if v not in (None, "")}
        key = tuple(sorted(tags.items()))
        acc = group.get(key)
        if acc is None:
            acc = group[key] = {
                "tags": tags, "numbers": defaultdict(float), "texts": defaultdict(set), "distinct": defaultdict(set),
                "common": {} if day_only else local_fields,
            }
        for name, value in series.fields(row).items():
            if value is None or value == "":
                continue
            if series.field_types.get(name) is str:
                acc["texts"][name].add(str(value))
            else:
                acc["numbers"][name] += value
        for name, key in series.distinct.items():
            # The field exists on every point of the series (0 when none is
            # new here): a type and a shape Grafana can sum as is.
            acc["distinct"].setdefault(name, set())
            value = key(row)
            if value and value not in counted[name]:
                counted[name].add(value)
                acc["distinct"][name].add(value)
    if group:
        yield from _flush(series, current, group)


def export(db: Session, sources: Optional[Sequence[str]] = None,
           measurement: Optional[str] = None) -> Iterator[Tuple[str, str]]:
    """(database, line) for every series (or one measurement), every
    platform or only these."""
    for series in SERIES:
        if measurement and series.measurement != measurement:
            continue
        for line in series_lines(db, series, sources):
            yield series.database, line
