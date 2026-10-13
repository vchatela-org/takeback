from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Iterator, List, Tuple, Any, Dict


@dataclass
class NormalizedItem:
    """One browsable item produced by an importer.

    `category` is the broad module grouping an item belongs to (e.g.
    "messages", "connections", "ads_information") -- what the exploration UI
    groups/navigates by. `item_type` is the fine-grained kind within that
    category (e.g. "message", "follower", "ad_viewed").

    `fields` holds structured label->value pairs extracted from a source's
    generic key/value export shapes (Meta's "label_values"/"string_map_data"
    wrappers cover dozens of categories with no dedicated schema) so the UI
    can render a real table instead of falling back to raw JSON.

    `media` holds (relative_path, media_type) pairs, where relative_path is
    relative to the export root (the directory an importer's parse() receives)
    and must point at a file that actually exists there.
    """

    item_type: str
    category: Optional[str] = None
    timestamp: Optional[datetime] = None
    title: Optional[str] = None
    content: Optional[str] = None
    author: Optional[str] = None
    context: Optional[str] = None
    fields: Optional[Dict[str, Any]] = None
    media: List[Tuple[str, str]] = field(default_factory=list)
    raw: Any = None


class BaseImporter(ABC):
    """Base class for a pluggable data-source import module.

    To add a new source: create a module under app/importers/, subclass this,
    decorate it with @registry.register, and import the module from
    app/importers/__init__.py so the decorator runs.
    """

    key: str
    label: str
    # item_type -> the name the UI shows for it (French); a type without
    # one is shown humanized ("app_install" -> "App install").
    type_labels: Dict[str, str] = {}

    @abstractmethod
    def detect(self, root: Path) -> bool:
        """Return True if the extracted export at `root` matches this source."""

    @abstractmethod
    def parse(self, root: Path) -> Iterator[NormalizedItem]:
        """Yield NormalizedItem instances found under `root`."""
