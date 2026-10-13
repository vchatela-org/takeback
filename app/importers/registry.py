from pathlib import Path
from typing import Dict, List, Optional, Type

from app.importers.base import BaseImporter

_registry: Dict[str, BaseImporter] = {}


def register(cls: Type[BaseImporter]) -> Type[BaseImporter]:
    instance = cls()
    _registry[instance.key] = instance
    return cls


def get(key: str) -> Optional[BaseImporter]:
    return _registry.get(key)


def all_importers() -> List[BaseImporter]:
    return list(_registry.values())


def type_label(item_type: str) -> Optional[str]:
    """The display name an importer gives this item type, if any."""
    for importer in _registry.values():
        label = importer.type_labels.get(item_type)
        if label:
            return label
    return None


def detect(root: Path) -> Optional[BaseImporter]:
    for importer in _registry.values():
        try:
            if importer.detect(root):
                return importer
        except Exception:
            continue
    return None
