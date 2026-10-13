"""Shared parsing for Meta's (Facebook/Instagram) generic export wrapper
shapes, used across the long tail of category files that aren't posts or
messages -- connections, ads, search history, security logs, and so on.

A Facebook or Instagram export has ~5-10 files with a dedicated, documented
schema (posts, messages) and dozens more that all reuse the same handful of
generic "label -> value" wrapper shapes:

- a list of records, each with "label_values": [{"label":, "value":}, ...],
  optionally grouped into named sub-sections via {"dict": [...], "title": }
- a list of records, each with "title" + "string_list_data": [{"href":,
  "value":, "timestamp":}, ...] (connections/followers_and_following/*)
- a list of records, each with "title" + "string_map_data": {"Field Name":
  {"value":, "href":, "timestamp":}, ...} (security/search history/profile)
- any of the above nested one level inside a single-key dict, e.g.
  {"relationships_following": [...]}

Rather than hand-writing a parser per category file (there are dozens, and
Meta adds more over time), this recognizes the shape and flattens it
generically, deriving `category`/`item_type` from the file's own path so a
new category file needs no code at all to show up as structured, browsable
data instead of disappearing entirely.
"""
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Union

from app.importers.base import NormalizedItem
from app.importers.utils import guess_media_type, load_json, resolve_media_uri

_TRAILING_INDEX_RE = re.compile(r"_\d+$")
_INSTAGRAM_POST_URL_RE = re.compile(r"https?://(?:www\.)?instagram\.com/(?:[\w.]+/)?(p|reel|tv)/[\w-]+/?")


def find_instagram_post_urls(values: Any) -> List[str]:
    """Post/reel links found among a set of field values -- what the "load
    the Instagram preview" button in the item view offers to embed. Only
    matches actual post/reel/tv permalinks, not arbitrary instagram.com
    links (profile URLs, ad click-throughs, ...)."""
    found = []
    for value in values:
        if not isinstance(value, str):
            continue
        match = _INSTAGRAM_POST_URL_RE.search(value)
        if match and match.group(0) not in found:
            found.append(match.group(0))
    return found


def category_from_path(rel_posix: str, activity_root: Union[str, tuple, list]) -> str:
    """Top-level category a file belongs to. Files under the platform's
    "your activity" umbrella are bucketed by their own sub-folder (comments,
    likes, saved, story_interactions, ...) since that's the meaningful
    grouping; everything else uses its own top-level folder name.

    `activity_root` accepts more than one name because Meta has renamed this
    folder before (Facebook: "your_facebook_activity" -> current
    "your_activity_across_facebook") -- an export requested a while ago can
    still use an older name."""
    roots = (activity_root,) if isinstance(activity_root, str) else tuple(activity_root)
    parts = rel_posix.split("/")
    if parts[0] in roots and len(parts) > 2:
        return parts[1]
    return parts[0]


def item_type_from_filename(filename: str) -> str:
    """e.g. 'followers_1.json' -> 'followers', 'ads_viewed.json' -> 'ads_viewed'."""
    stem = Path(filename).stem
    return _TRAILING_INDEX_RE.sub("", stem) or stem


def humanize(identifier: str) -> str:
    """'ads_viewed' -> 'Ads viewed' -- a readable fallback label for any
    category/item_type this code has no dedicated translation for."""
    return identifier.replace("_", " ").replace("-", " ").strip().capitalize()


def _flatten_label_values(entries: List[Any], prefix: str = "") -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if "dict" in entry and isinstance(entry["dict"], list):
            section = entry.get("title") or ""
            nested = _flatten_label_values(entry["dict"], prefix=f"{prefix}{section} / " if section else prefix)
            fields.update(nested)
            continue
        label = prefix + str(entry.get("label") or entry.get("title") or "?")
        value = entry.get("value")
        if value is None:
            value = entry.get("timestamp_value")
        if value is not None:
            fields[label] = value
        # "href" is a real, separate piece of data (a profile/post/ad link)
        # alongside "value", not just a fallback for when value is missing --
        # dropping it silently loses the one field a "load the Instagram
        # preview" feature would need.
        href = entry.get("href")
        if href is not None and href != value:
            fields[f"{label} (lien)" if value is not None else label] = href
    return fields


def _flatten_string_list(entries: List[Any]) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        suffix = f" {i + 1}" if len(entries) > 1 else ""
        value = entry.get("value")
        if value is not None:
            fields[f"Valeur{suffix}"] = value
        href = entry.get("href")
        if href is not None and href != value:
            fields[f"Lien{suffix}" if value is not None else f"Valeur{suffix}"] = href
    return fields


def _flatten_string_map(mapping: Dict[str, Any]) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    for field_name, payload in mapping.items():
        if isinstance(payload, dict):
            value = payload.get("value")
            if value is not None:
                fields[field_name] = value
            href = payload.get("href")
            if href is not None and href != value:
                fields[f"{field_name} (lien)" if value is not None else field_name] = href
        elif payload is not None:
            fields[field_name] = payload
    return fields


def _record_timestamp(record: Dict[str, Any], entries_source: Optional[List[Any]]) -> Optional[datetime]:
    ts = record.get("timestamp")
    if ts is None and entries_source:
        for entry in entries_source:
            if isinstance(entry, dict) and entry.get("timestamp"):
                ts = entry["timestamp"]
                break
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts))
    except (ValueError, OSError, OverflowError, TypeError):
        return None


def _normalize_record(root: Path, record: Dict[str, Any], category: str, item_type: str) -> Optional[NormalizedItem]:
    fields: Dict[str, Any] = {}
    timestamp = None

    if isinstance(record.get("label_values"), list):
        fields = _flatten_label_values(record["label_values"])
        timestamp = _record_timestamp(record, None)
    elif isinstance(record.get("string_list_data"), list):
        fields = _flatten_string_list(record["string_list_data"])
        timestamp = _record_timestamp(record, record["string_list_data"])
    elif isinstance(record.get("string_map_data"), dict):
        fields = _flatten_string_map(record["string_map_data"])
        timestamp = _record_timestamp(record, None)
    else:
        return None  # not a shape we recognize -- leave it to the raw-JSON fallback

    media = []
    for media_entry in (record.get("media") or []):
        uri = media_entry.get("uri") if isinstance(media_entry, dict) else None
        if uri:
            rel = resolve_media_uri(root, uri)
            if rel:
                media.append((rel, guess_media_type(rel)))

    preview = " · ".join(f"{k} : {v}" for k, v in list(fields.items())[:3] if v not in (None, ""))

    return NormalizedItem(
        item_type=item_type,
        category=category,
        timestamp=timestamp,
        title=record.get("title") if record.get("title") else None,
        content=preview or None,
        author=None,
        context=humanize(category),
        fields=fields or None,
        media=media,
        raw=record,
    )


def parse_generic_category_file(root: Path, path: Path, activity_root: Union[str, tuple, list]) -> Iterator[NormalizedItem]:
    """Parse one non-post/non-message export file using Meta's generic
    label/value wrapper shapes. Silently yields nothing for files that don't
    match any recognized shape (they're not lost -- just not covered yet;
    add a shape to this module rather than per-file code)."""
    try:
        data = load_json(path, fix_encoding=True)
    except Exception:
        return

    records: Any = data
    if isinstance(data, dict):
        list_values = [v for v in data.values() if isinstance(v, list)]
        records = list_values[0] if len(list_values) == 1 else [data]
    if not isinstance(records, list):
        return

    rel = path.relative_to(root).as_posix()
    category = category_from_path(rel, activity_root)
    item_type = item_type_from_filename(path.name)

    for record in records:
        if not isinstance(record, dict):
            continue
        try:
            item = _normalize_record(root, record, category, item_type)
        except Exception:
            continue
        if item is not None:
            yield item
