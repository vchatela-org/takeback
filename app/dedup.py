"""Cross-import deduplication, so a platform can be imported again every year
and still be viewed as one continuous history.

Every export of a platform (Amazon "Request My Data", Facebook/Instagram
"Download your information", Google Takeout) carries the account's WHOLE
history up to the export date, not just what changed since the previous
one. Merging two yearly Amazon exports naively would count every older order
twice -- so each item gets a `fingerprint`, and among the items of one source
sharing a fingerprint exactly one is kept by the platform views
(`is_duplicate` False), the copy from the most recent import.

The fingerprint is computed from the stored, normalized columns (not
`raw_json`): a later export can add CSV columns or JSON keys the importer
doesn't map, which would make the raw fragment differ for the very same
order. Field values that legitimately change between two exports of the same
record (an order's status, a Keep note's "pinned" flag) are left out, so the
record isn't counted once per status it went through -- those are listed per
item type in VOLATILE_FIELDS, and per source where the same item type is
exported differently by two platforms (see VOLATILE_FIELDS_BY_SOURCE).

Two rows of ONE import can be genuinely identical (the same product twice in
one order, on two lines): they get an occurrence suffix (":2", ":3"...) so
they stay distinct from each other, while still matching the same two rows of
another import. The number of copies kept is therefore the most any single
import holds, never the sum across imports.
"""
import hashlib
import json
import logging
from typing import Iterable, Optional

from sqlalchemy import func, text, update
from sqlalchemy.orm import Session

from app.models import ImportBatch, ImportStatus, Item

logger = logging.getLogger(__name__)

# item_type -> field labels left out of the fingerprint (see module docstring).
VOLATILE_FIELDS = {
    "order_item": ("Statut", "Expédié le"),
    "digital_order": ("Statut",),
    "audible_order": ("Statut",),
    "return": ("Date de remboursement",),
    "return_status": ("Résolution", "Date de remboursement"),
    # Kindle: a book's right revoked (refund, loan ended), a document deleted.
    "kindle_book": ("Statut",),
    "kindle_document": ("Supprimé",),
    "keep_note": ("Épinglé", "Archivé", "Corbeille"),
    # LinkedIn: a connection changes jobs, an endorsement gets accepted, a
    # position gets an end date or a reworded description.
    "connection": ("Entreprise", "Poste", "E-mail"),
    "member_follow": ("Statut",),
    "hashtag_follow": ("Statut", "Modifié le"),
    "position": ("Fin", "Lieu", "Description"),
    "volunteering": ("Fin", "Description"),
    "education": ("Notes", "Activités"),
    "certification": ("Expire", "Lien"),
    "endorsement_received": ("Statut",),
    "endorsement_given": ("Statut",),
    "recommendation_received": ("Statut",),
    "recommendation_given": ("Statut",),
    "event": ("Statut",),
    "learning_course": ("Terminé le", "Enregistré", "Notes"),
    "message": ("Dossier",),
    "post": ("Visibilité",),
    # LinkedIn recomputes its inferred scores every week.
    "credibility_score": ("Score", "Calculé le", "Historique"),
    "inference": ("Valeur", "Description"),
}
# Item types dated by their latest occurrence rather than their creation (a
# LinkedIn Learning course is dated by its last viewing): that date moves
# from one export to the next, so it stays out of the fingerprint too.
VOLATILE_TIMESTAMP_TYPES = {"learning_course"}

# "*" for a source whose records are filled in progressively: every field
# can be rewritten between two exports, so only the record's identity -- the
# item_type, the timestamp and the `context` the importer puts its own id
# in -- is left in the signature.
ALL_FIELDS = "*"

# Volatility is a property of how a SOURCE writes a record as much as of the
# item type, and the same item type can be exported differently by two
# platforms: an Amazon order is complete the moment Order History lists it,
# while a Seazon order is charged when its selection window closes and the
# export keeps filling it in afterwards (tracking number at shipping, rating
# once delivered, refund once cancelled). Checked before VOLATILE_FIELDS.
VOLATILE_FIELDS_BY_SOURCE = {
    ("seazon", item_type): ALL_FIELDS for item_type in
    ("seazon_order", "seazon_refund", "order_cancelled", "meal", "subscription_plan", "notification",
     "account_log", "coupon", "profile")
}

_CHUNK = 5000


def item_signature(source, item_type, category, timestamp, title, content, author, context, fields_json) -> str:
    fields = json.loads(fields_json) if fields_json else {}
    volatile = VOLATILE_FIELDS_BY_SOURCE.get((source, item_type)) or VOLATILE_FIELDS.get(item_type, ())
    if volatile is ALL_FIELDS:
        fields = {}
    else:
        for label in volatile:
            fields.pop(label, None)
    if item_type in VOLATILE_TIMESTAMP_TYPES:
        timestamp = None
    payload = json.dumps(
        [item_type, category, timestamp.isoformat() if timestamp else None, title, content, author, context, fields],
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def assign_fingerprints(db: Session, batch_id: int) -> None:
    """Fingerprint every item of one batch. Read in id-keyed chunks (only the
    columns the signature needs -- never raw_json) so a very large export
    doesn't have to fit in memory at once, and committed per chunk so the
    SQLite write lock is never held long enough for a concurrent request to
    give up on it. Safe to re-run on a partly fingerprinted batch: every
    fingerprint is recomputed from scratch before the occurrence suffix."""
    last_id = 0
    while True:
        rows = (
            db.query(
                Item.id, Item.source, Item.item_type, Item.category, Item.timestamp, Item.title,
                Item.content, Item.author, Item.context, Item.fields_json,
            )
            .filter(Item.batch_id == batch_id, Item.id > last_id)
            .order_by(Item.id)
            .limit(_CHUNK)
            .all()
        )
        if not rows:
            break
        db.execute(update(Item), [{"id": row[0], "fingerprint": item_signature(*row[1:])} for row in rows])
        db.commit()
        last_id = rows[-1][0]

    # Occurrence suffix for rows identical within this batch -- see the
    # module docstring. Ordered by id, i.e. parse order.
    db.execute(
        text(
            "UPDATE items SET fingerprint = items.fingerprint || ':' || ranked.rn "
            "FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY fingerprint ORDER BY id) AS rn "
            "      FROM items WHERE batch_id = :batch_id) AS ranked "
            "WHERE items.id = ranked.id AND ranked.rn > 1"
        ),
        {"batch_id": batch_id},
    )
    db.commit()


def refresh_duplicates(db: Session, source: Optional[str]) -> None:
    """Re-elect the kept copy of every fingerprint of one source: the one from
    the most recent batch. Run after any batch of that source is added,
    reanalyzed or deleted -- deleting the newest import must hand the
    visibility of its items back to the previous import's copies."""
    if not source:
        return
    db.execute(
        text("UPDATE items SET is_duplicate = NULL WHERE source = :source AND fingerprint IS NULL"),
        {"source": source},
    )
    db.execute(
        text(
            "UPDATE items SET is_duplicate = (ranked.rn > 1) "
            "FROM (SELECT id, ROW_NUMBER() OVER ("
            "        PARTITION BY fingerprint ORDER BY batch_id DESC, id DESC) AS rn "
            "      FROM items WHERE source = :source AND fingerprint IS NOT NULL) AS ranked "
            "WHERE items.id = ranked.id "
            "  AND (items.is_duplicate IS NULL OR items.is_duplicate != (ranked.rn > 1))"
        ),
        {"source": source},
    )
    db.commit()


def finalize_batch(db: Session, batch_id: int, source: str) -> None:
    assign_fingerprints(db, batch_id)
    refresh_duplicates(db, source)


def refresh_sources(db: Session, sources: Iterable[Optional[str]]) -> None:
    for source in {s for s in sources if s}:
        refresh_duplicates(db, source)


def backfill(db: Session) -> None:
    """One-off at startup for items imported before deduplication existed:
    fingerprint every finished batch that still has unfingerprinted items.
    Items of a batch still pending/parsing are left alone -- they get theirs
    when that batch finishes."""
    batches = (
        db.query(ImportBatch.id, ImportBatch.source)
        .filter(ImportBatch.status == ImportStatus.done)
        .filter(
            db.query(Item.id)
            .filter(Item.batch_id == ImportBatch.id, Item.fingerprint.is_(None))
            .exists()
        )
        .all()
    )
    stale_sources = {
        row[0] for row in db.query(Item.source)
        .filter(Item.fingerprint.isnot(None), Item.is_duplicate.is_(None))
        .distinct()
        .all()
    }
    if not batches and not stale_sources:
        return

    for batch_id, source in batches:
        logger.info("Dedup backfill: fingerprinting batch %s (%s)", batch_id, source)
        assign_fingerprints(db, batch_id)
        stale_sources.add(source)
    refresh_sources(db, stale_sources)
    logger.info(
        "Dedup backfill done: %d batch(es), %d duplicate item(s) hidden",
        len(batches), db.query(func.count(Item.id)).filter(Item.is_duplicate.is_(True)).scalar() or 0,
    )
