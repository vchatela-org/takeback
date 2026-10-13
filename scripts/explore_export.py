#!/usr/bin/env python
"""Dev helper: inspect a real export zip without going through the app
(no server, no DB). Used to ground the importer modules in the actual
structure of a real export instead of guesses.

Usage:
    python scripts/explore_export.py sample_data/facebook_export.zip
    python scripts/explore_export.py sample_data/facebook_export.zip --tree
    python scripts/explore_export.py sample_data/facebook_export.zip --limit 20
"""
import argparse
import json
import sys
import tempfile
import zipfile
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # avoid mangling accents / the real FB-mojibake on Windows consoles

from app import importers  # noqa: E402


def print_tree(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_file():
            print(f"{path.stat().st_size:>10}  {rel}")
        else:
            print(f"{'':>10}  {rel}/")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("zip_path", type=Path)
    parser.add_argument("--tree", action="store_true", help="N'affiche que l'arborescence, sans parser")
    parser.add_argument("--limit", type=int, default=10, help="Nombre d'éléments à prévisualiser")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        extract_dir = Path(tmp)
        with zipfile.ZipFile(args.zip_path) as zf:
            zf.extractall(extract_dir)

        print(f"--- Fichiers dans {args.zip_path.name} ---")
        print_tree(extract_dir)

        if args.tree:
            return

        importer = importers.detect(extract_dir)
        print("\n--- Détection ---")
        print(f"Module détecté : {importer.key if importer else 'AUCUN'}")
        if not importer:
            return

        counts: dict[str, int] = {}
        preview = []
        for i, item in enumerate(importer.parse(extract_dir)):
            counts[item.item_type] = counts.get(item.item_type, 0) + 1
            if i < args.limit:
                preview.append(asdict(item))

        print(f"\n--- Total par type ---\n{json.dumps(counts, indent=2, ensure_ascii=False)}")
        print(f"\n--- Aperçu (max {args.limit}) ---")
        print(json.dumps(preview, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
