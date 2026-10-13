import enum
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, Integer, String, Text, DateTime, ForeignKey, Index,
    Enum as SAEnum,
)
from sqlalchemy.orm import relationship

from app.database import Base


# ────────────────────────────────────────────────
# Enums
# ────────────────────────────────────────────────

class ImportStatus(str, enum.Enum):
    pending = "pending"
    extracting = "extracting"
    parsing = "parsing"
    done = "done"
    error = "error"
    unrecognized = "unrecognized"


# ────────────────────────────────────────────────
# ImportBatch
# ────────────────────────────────────────────────

class ImportBatch(Base):
    __tablename__ = "import_batches"

    id = Column(Integer, primary_key=True, index=True)
    source = Column(String(50), nullable=True, index=True)  # filled once detected, e.g. "facebook"
    original_filename = Column(String(500), nullable=False)
    status = Column(SAEnum(ImportStatus), nullable=False, default=ImportStatus.pending)
    error_message = Column(Text, nullable=True)
    item_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    items = relationship("Item", back_populates="batch", cascade="all, delete-orphan")


# ────────────────────────────────────────────────
# Item  (a normalized post / message / activity / photo / ...)
# ────────────────────────────────────────────────

class Item(Base):
    __tablename__ = "items"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("import_batches.id", ondelete="CASCADE"), nullable=False, index=True)
    source = Column(String(50), nullable=False, index=True)       # facebook / instagram / google / ...
    category = Column(String(100), nullable=True, index=True)     # broad grouping: messages / connections / ads_information / ...
    item_type = Column(String(100), nullable=False, index=True)   # fine-grained: message / follower / ad_viewed / ...
    timestamp = Column(DateTime, nullable=True, index=True)
    title = Column(String(500), nullable=True)
    content = Column(Text, nullable=True)
    author = Column(String(300), nullable=True)
    context = Column(String(500), nullable=True)  # e.g. conversation name, album name, Google service
    fields_json = Column(Text, nullable=True)      # structured {label: value} pairs for generic category records
    raw_json = Column(Text, nullable=True)         # original parsed fragment, kept so nothing is lost
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    # Cross-import deduplication (see app/dedup.py): every export of a
    # platform carries its whole history, so importing it again a year later
    # repeats every older item. `fingerprint` identifies the same real-world
    # item across imports of one source; `is_duplicate` is False for the one
    # copy the platform views count, True for the others, and NULL while the
    # batch is still being parsed (not yet visible in platform views).
    fingerprint = Column(String(80), nullable=True)
    is_duplicate = Column(Boolean, nullable=True)

    batch = relationship("ImportBatch", back_populates="items")
    media_files = relationship("MediaFile", back_populates="item", cascade="all, delete-orphan")

    __table_args__ = (
        # The conversations page filters batch_id + item_type == "message" then
        # groups/orders by context/timestamp on every load -- with only the
        # single-column indexes above, SQLite can use just one of them (usually
        # batch_id) and has to scan+sort the rest in memory, which is what made
        # opening a thread-heavy export slow. These composite indexes cover
        # get_threads (group by context) and get_thread_message_page/
        # get_me_candidate (filter+order by timestamp) directly.
        Index("ix_items_batch_type_context", "batch_id", "item_type", "context"),
        Index("ix_items_batch_type_timestamp", "batch_id", "item_type", "timestamp"),
        # Same reasoning for the platform views, which scope by
        # source + is_duplicate instead of batch_id.
        Index("ix_items_platform_timestamp", "source", "is_duplicate", "timestamp"),
        Index("ix_items_platform_type_timestamp", "source", "is_duplicate", "item_type", "timestamp"),
        Index("ix_items_platform_type_context", "source", "is_duplicate", "item_type", "context"),
        # dedup.refresh_duplicates partitions by fingerprint within a source.
        Index("ix_items_source_fingerprint", "source", "fingerprint", "batch_id"),
    )


# ────────────────────────────────────────────────
# SyncRun  (one run of a sink pushing data out -- app/scheduler.py)
# ────────────────────────────────────────────────

class SyncRun(Base):
    __tablename__ = "sync_runs"

    id = Column(Integer, primary_key=True, index=True)
    kind = Column(String(50), nullable=False, index=True)  # influxdb
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False, default="running")  # running / ok / error
    message = Column(Text, nullable=True)  # what changed, or the error
    stats_json = Column(Text, nullable=True)  # {label: count} of what the run read or wrote


# ────────────────────────────────────────────────
# MediaFile  (photo/video/audio referenced by an item, on disk under data/imports/<batch_id>/)
# ────────────────────────────────────────────────

class MediaFile(Base):
    __tablename__ = "media_files"

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey("items.id", ondelete="CASCADE"), nullable=False, index=True)
    relative_path = Column(String(1000), nullable=False)  # relative to data/imports/<batch_id>/
    media_type = Column(String(20), nullable=False, default="other")  # photo / video / audio / other
    original_name = Column(String(500), nullable=True)

    item = relationship("Item", back_populates="media_files")
