"""Budgets for the two inputs an archive gives the app no control over: how
much can be uploaded, and how much an archive is allowed to expand into.

This matters because Takeback has no authentication and an uploaded archive is
untrusted input: without a budget, a single request can fill the disk, and a
small zip can expand into an arbitrarily large one (the "zip bomb"). Both are
denial-of-service rather than disclosure, but neither is something a self-hosted
app should let a visitor do by accident or on purpose.

`ZipFile.extractall` already sanitises member names -- it drops absolute paths,
`..` and drive letters -- so a Zip Slip is not the risk here. What it has no
budget for is size, so extraction goes through `extract_archive` below, which
counts the bytes as they land rather than trusting the archive's declared
sizes: a crafted central directory can understate them.
"""
import logging
import zipfile
from pathlib import Path
from typing import Iterable, List

from fastapi import UploadFile

from app.config import settings

logger = logging.getLogger(__name__)

_CHUNK = 1024 * 1024


class UploadTooLarge(Exception):
    """An upload, or what an archive expands to, went past its budget.

    A plain exception rather than an HTTP one, so the limits stay usable from
    the import pipeline (a background task) as well as from a request handler;
    each caller turns it into the response its own layer speaks.
    """


def _mb(value: int) -> str:
    return f"{value / 1024 ** 2:.0f} Mo"


def check_upload_size(filename: str, written: int) -> None:
    """A single uploaded file's budget."""
    limit = settings.max_upload_mb * 1024 ** 2
    if written > limit:
        raise UploadTooLarge(
            f"« {filename} » dépasse la taille maximale d'un fichier "
            f"({settings.max_upload_mb} Mo)."
        )


def check_batch_upload_size(paths: Iterable[Path]) -> None:
    """A batch's budget, over every file that belongs to it -- an import is
    several files, and the per-file cap alone would let a caller send an
    unbounded number of them."""
    total = 0
    for path in paths:
        try:
            total += path.stat().st_size
        except OSError:
            continue
    limit = settings.max_batch_upload_mb * 1024 ** 2
    if total > limit:
        raise UploadTooLarge(
            f"Cet import dépasse la taille maximale d'un envoi "
            f"({settings.max_batch_upload_mb} Mo au total)."
        )


async def stream_upload(upload: UploadFile, dest: Path, filename: str) -> int:
    """Write an upload to disk, refusing it the moment it passes its budget.

    Checking as the bytes arrive rather than afterwards matters: a request body
    with no length (chunked encoding) is exactly the shape that would otherwise
    be written in full before anyone looked at its size. A refused upload deletes
    what it wrote, so a too-large file leaves nothing behind.
    """
    written = 0
    try:
        with open(dest, "wb") as out:
            while chunk := await upload.read(_CHUNK):
                written += len(chunk)
                check_upload_size(filename, written)
                out.write(chunk)
    except BaseException:
        # BaseException, not Exception: a cancelled request must not leave a
        # truncated archive behind to be picked up as a complete one later.
        dest.unlink(missing_ok=True)
        raise
    return written


def extract_archive(zip_paths: List[Path], dest: Path) -> None:
    """Extract every archive into `dest`, refusing to blow past the byte
    budget.

    Replaces `ZipFile.extractall`, which has no budget of its own. Each member
    is written explicitly: the target path is resolved and checked to be inside
    `dest` (belt and braces on top of `zipfile`'s own sanitising, since this
    loop is what actually decides where bytes go now), and the running total of
    *actual* decompressed bytes is what the limit is tested against.
    """
    limit = int(settings.max_extract_gb * 1024 ** 3)
    root = dest.resolve()
    total = 0

    for zip_path in zip_paths:
        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                # An absolute name, or one climbing out with "..", is not an
                # archive we try to repair: skipping it silently would hide a
                # crafted archive, so it is refused.
                target = (root / member.filename).resolve()
                if target != root and root not in target.parents:
                    raise UploadTooLarge(
                        f"L'archive contient un chemin hors du dossier de "
                        f"destination ({member.filename!r}) : elle est refusée."
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, open(target, "wb") as out:
                    while chunk := source.read(_CHUNK):
                        total += len(chunk)
                        if total > limit:
                            raise UploadTooLarge(
                                f"Le contenu extrait dépasse la taille maximale "
                                f"({settings.max_extract_gb:g} Go)."
                            )
                        out.write(chunk)

    logger.info("Extracted %s into %s (%s)", [p.name for p in zip_paths], dest, _mb(total))
