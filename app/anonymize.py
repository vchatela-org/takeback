"""Clones an extracted export into a same-format archive with identifying
content replaced by generated fake data, so it can be handed to a third
party (a developer, support, an AI assistant) without exposing real data.

Runs on the raw extracted export (the exact files an importer's parse()
would read), not on the normalized DB rows, so folder layout, JSON schema
quirks and encoding bugs are preserved for realistic parser development.

Safety model for JSON CONTENT is default-deny: every string value is treated
as sensitive and replaced UNLESS its key is explicitly recognized as purely
technical (a timestamp, or the "uri" media-path field, rewritten rather than
dropped so the archive stays internally consistent).

Safety model for FILE/DIRECTORY NAMES is the opposite: Meta/Google's export
category names (there are dozens across Facebook/Instagram/Google Takeout,
an exhaustive allow-list is unmaintainable and guessing wrong there only
destroys useful, non-personal structure) are kept as-is by default. Only the
few directory patterns known to embed a real identity are pseudonymized: a
per-conversation folder under inbox/message_requests/archived_threads, or a
per-album folder under "Google Photos". Filenames are never renamed.
"""
import base64
import csv
import io
import json
import random
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from faker import Faker

from app.database import anonymized_artifact
from app.importers import linkedin

# Field names (lowercased) whose string value is a person's name-like identity.
NAME_KEYS = {
    "name", "sender_name", "actor", "author", "first_name", "last_name",
    "given_name", "family_name", "display_name", "nickname",
}
# Field names whose string value is a direct contact detail.
CONTACT_KEYS = {"email", "email_address", "phone_number", "phone", "address", "ip_address"}
# Substring hints on the key name for values that represent a point in time.
TIMESTAMP_HINTS = ("timestamp", "time")
# Exact GPS coordinate fields (e.g. story/photo EXIF data) -> never passed
# through: a real lat/lon pinpoints a home, workplace or trip precisely.
GPS_KEYS = {"latitude", "longitude", "lat", "lng", "lon"}
# EXIF stores dates as "YYYY:MM:DD HH:MM:SS", not ISO -- unlike every other
# timestamp shape in these exports, digits and colons make it look numeric
# enough to slip past naive parsing, so it gets its own explicit format.
EXIF_DATETIME_FORMAT = "%Y:%m:%d %H:%M:%S"
# Plain-text date shapes of LinkedIn's CSVs (each file has its own): shifted
# and written back in the same shape, so the anonymized copy still parses.
TEXT_DATE_FORMATS = (
    "%d %b %Y", "%b %Y", "%m/%d/%y, %I:%M %p", "%Y/%m/%d %H:%M:%S UTC", "%Y-%m-%d %H:%M UTC",
    "%Y-%m-%d %H:%M:%S UTC", "%a %b %d %H:%M:%S UTC %Y", "%b %d, %Y %I:%M %p", "%m/%d/%Y",
)
# Generous upper bound (year ~2255) used only to detect the UNIT of a numeric
# timestamp: a real epoch value in seconds never exceeds this, so anything
# bigger is milliseconds/microseconds/nanoseconds and gets scaled down.
_MAX_REASONABLE_EPOCH_SECONDS = 9_000_000_000
# Meta's generic export wrapper represents "field name" two ways: as a real
# JSON key (string_map_data: {"Comment": {...}}, already safe since object
# keys are never rewritten) or as the VALUE of a "label" field
# (label_values: [{"label": "Comment", "value": ...}]). The label itself is
# drawn from Meta's fixed internal vocabulary, not user content, so unlike
# "value"/"href" it must be preserved for the data to stay legible.
PRESERVE_VALUE_KEYS = {"label"}

# 1x1 transparent PNG used as a stand-in for every media file, so the archive
# keeps its exact file layout without carrying any real photo/video/audio.
PLACEHOLDER_MEDIA_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic",
    ".mp4", ".mov", ".avi", ".webm", ".mp3", ".m4a", ".wav", ".ogg",
}

# A directory whose PARENT (by exact name, case-insensitive) is one of these
# is a real identity, not a product category: a Facebook/Instagram message
# thread is named after its participant(s), a Google Photos album folder is
# named after the (often personal) album title the user gave it.
PERSONAL_DIR_PARENTS = {"inbox", "message_requests", "archived_threads", "filtered_threads", "google photos"}


def _corrupt_like_meta(text: str) -> str:
    """Reproduce the Facebook/Instagram export mojibake bug (UTF-8 bytes
    decoded as Latin-1) so anonymized fixtures still exercise fix_mojibake()."""
    try:
        return text.encode("utf-8").decode("latin1")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return text


def _is_id_key(key_lower: str) -> bool:
    """Arbitrary platform identifiers (a numeric Facebook id, a uid) are
    pseudonymized with a consistent mapping rather than left as a real,
    potentially-reversible identifier -- but never faked as free text."""
    return key_lower in ("id", "fbid") or key_lower.endswith(("_id", "_uid"))


class PathAnonymizer:
    """Pseudonymizes ONLY directory names matching a known-personal pattern,
    memoized so the same real name maps to the same fake name everywhere
    (the folder entry itself AND any "uri" field elsewhere in the JSON that
    references that same path). Filenames are always left untouched."""

    def __init__(self, faker: Faker):
        self._faker = faker
        self._map: dict[str, str] = {}

    def _fake_dir_name(self, name: str) -> str:
        if name not in self._map:
            self._map[name] = self._faker.slug()
        return self._map[name]

    def rename_relpath(self, rel_posix: str) -> str:
        parts = [p for p in rel_posix.replace("\\", "/").split("/") if p]
        if not parts:
            return rel_posix
        new_parts = list(parts)
        for i in range(len(parts) - 1):  # never touch the last segment (the filename)
            parent = parts[i - 1].lower() if i > 0 else ""
            if parent in PERSONAL_DIR_PARENTS:
                new_parts[i] = self._fake_dir_name(parts[i])
        return "/".join(new_parts)


class Anonymizer:
    def __init__(self, paths: PathAnonymizer, corrupt_encoding: bool = False):
        self._faker = Faker("fr_FR")
        self._paths = paths
        self._map: dict[str, str] = {}
        self._corrupt_encoding = corrupt_encoding
        # One offset for the whole export: real dates are shifted, not
        # scrambled, so relative ordering/duration between events survives.
        self._day_offset = timedelta(days=random.randint(30, 900) * random.choice([-1, 1]))

    def _maybe_corrupt(self, text: str) -> str:
        return _corrupt_like_meta(text) if self._corrupt_encoding else text

    def _remember(self, original: str, generate) -> str:
        original = str(original)
        if original not in self._map:
            self._map[original] = generate()
        return self._map[original]

    def _pseudo_name(self, value: str) -> str:
        return self._maybe_corrupt(self._remember(value, self._faker.name))

    def _pseudo_contact(self, key: str, value: str) -> str:
        if "email" in key:
            return self._remember(value, self._faker.email)
        if "phone" in key:
            return self._remember(value, self._faker.phone_number)
        if "address" in key:
            return self._maybe_corrupt(self._remember(value, self._faker.address))
        if "ip" in key:
            return self._remember(value, self._faker.ipv4)
        return self._remember(value, self._faker.word)

    def _pseudo_text(self, value: str) -> str:
        word_count = max(1, min(len(value.split()), 40))
        return self._maybe_corrupt(self._faker.sentence(nb_words=word_count))

    def _pseudo_id(self, value: Any) -> Any:
        replacement = self._remember(value, lambda: str(random.randint(10**10, 10**18)))
        return int(replacement) if isinstance(value, int) else replacement

    def _pseudo_amount(self, value: str) -> str:
        """A numeric-looking CSV cell (price, tax, quantity...) is replaced
        with a random plausible number of similar shape (decimal vs. whole)
        rather than free text -- otherwise a column like Amazon's "Total
        Owed" would turn into a nonsense sentence, making the anonymized
        copy useless for verifying amount-parsing logic against. A
        non-numeric placeholder (Amazon uses the literal string
        "Not Available" in several numeric columns) isn't actually a
        number, so it falls back to the normal text treatment."""
        stripped = value.strip()
        try:
            num = float(stripped.replace(",", ""))
        except ValueError:
            return self._pseudo_text(value)
        is_int = "." not in stripped and "," not in stripped
        # A whole-number column here is virtually always a quantity, which
        # is never 0 on a real order line -- floor the fake value at 1 so
        # it doesn't produce an unrealistic edge case the real data never
        # had (a decimal amount can still land near 0, e.g. a small tax).
        floor = 1 if is_int else 0
        fake = random.uniform(floor, max(abs(num) * 2, floor + 1))
        return str(max(int(fake), floor)) if is_int else f"{fake:.2f}"

    def _pseudo_gps(self, value: Any) -> Any:
        if not isinstance(value, (int, float)):
            return value
        fake = random.uniform(-90, 90) if abs(value) <= 90 else random.uniform(-180, 180)
        return round(fake, 6)

    def _jitter_timestamp(self, value: Any) -> Any:
        # Safety net first: whatever shape this turns out to be, a
        # timestamp-hinted key must never leave this function holding its
        # original real value -- every return path below is either a
        # shifted date or an explicit fallback, never a bare `return value`.
        try:
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and len(value) == 4 and value.isdigit() and 1900 <= int(value) <= 2100:
                # A bare year (LinkedIn's studies), not an epoch value.
                return str(int(value) + round(self._day_offset.days / 365))
            if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
                num = float(value)
                # Google in particular mixes epoch seconds, milliseconds,
                # microseconds (Keep's createdTimestampUsec) AND nanoseconds
                # (Google Fit/Pixel diagnostics) depending on the field --
                # a single "is it bigger than 1e12 -> must be milliseconds"
                # guess mistook micro/nanosecond values for milliseconds and
                # produced a shifted date with a 5-to-6-digit year (datetime
                # then raises "year N is out of range"). Repeatedly scale
                # down by 1000 until the magnitude looks like real epoch
                # seconds instead of assuming a single fixed unit.
                divisor = 1
                scaled = num
                while abs(scaled) > _MAX_REASONABLE_EPOCH_SECONDS and divisor < 10**9:
                    scaled /= 1000
                    divisor *= 1000
                dt = datetime.fromtimestamp(scaled)
                shifted_num = (dt + self._day_offset).timestamp() * divisor
                return str(int(shifted_num)) if isinstance(value, str) else type(value)(shifted_num)
            if isinstance(value, str):
                try:
                    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    shifted = dt + self._day_offset
                    return shifted.isoformat().replace("+00:00", "Z")
                except ValueError:
                    pass
                try:
                    dt = datetime.strptime(value, EXIF_DATETIME_FORMAT)
                    shifted = dt + self._day_offset
                    return shifted.strftime(EXIF_DATETIME_FORMAT)
                except ValueError:
                    pass
                # A range ("Oct 8, 2026 10:00 AM - Oct 8, 2026 11:00 AM"):
                # both ends shifted.
                parts = [self._shift_text_date(part) for part in value.split(" - ")]
                if all(parts):
                    return " - ".join(parts)
        except (OverflowError, OSError, ValueError):
            # ValueError included because datetime raises it (not
            # OverflowError) for a shifted result outside year 1-9999 --
            # exactly the "year N is out of range" crash this whole rewrite
            # is fixing. The magnitude scaling above should already keep
            # real data in range; this stays as the last-resort net for
            # whatever it doesn't anticipate.
            pass
        # Unrecognized date format under a timestamp-hinted key: don't risk
        # leaking the real value verbatim, replace it with faked text instead.
        return self._pseudo_text(value) if isinstance(value, str) else value

    def _shift_text_date(self, value: str) -> Optional[str]:
        for fmt in TEXT_DATE_FORMATS:
            try:
                return (datetime.strptime(value.strip(), fmt) + self._day_offset).strftime(fmt)
            except ValueError:
                continue
        return None

    def walk(self, key: str, value: Any) -> Any:
        if isinstance(value, dict):
            # Meta's generic nested-group wrapper pairs a "dict" (child
            # entries) with a "title" that is the group's section header,
            # not user content -- e.g. {"dict": [...], "title": "Ad topics"}.
            # Only in that specific pairing is "title" structural; on its
            # own (a post/thread title) it stays subject to the normal rule.
            is_label_group = "dict" in value
            return {
                k: (v if (k == "title" and is_label_group) else self.walk(k, v))
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.walk(key, v) for v in value]

        key_lower = key.lower()

        if key_lower in PRESERVE_VALUE_KEYS:
            return value

        if key_lower in GPS_KEYS:
            return self._pseudo_gps(value)

        if any(hint in key_lower for hint in TIMESTAMP_HINTS):
            return self._jitter_timestamp(value)

        if key_lower == "uri" and isinstance(value, str):
            return self._paths.rename_relpath(value)

        if _is_id_key(key_lower) and isinstance(value, (int, str)):
            return self._pseudo_id(value)

        if isinstance(value, str):
            if key_lower in NAME_KEYS:
                return self._pseudo_name(value)
            if key_lower in CONTACT_KEYS:
                return self._pseudo_contact(key_lower, value)
            # Default-deny: any other text field is treated as free-form
            # content and fully replaced. We never try to guess which
            # unrecognized fields are "safe" to keep verbatim.
            return self._pseudo_text(value)

        return value  # numbers/bools/null: structural (counts, flags, schema-level values)


def _anonymize_csv(anonymizer: Anonymizer, text: str, escapechar: Optional[str] = None) -> str:
    """Keeps the header row (column names are structural) verbatim, then
    dispatches each data cell rather than treating every cell identically:
    a date/time-like column (by name) is jittered the same way JSON
    timestamps are (real per-row dates, chronological order preserved),
    and any other cell that's a bare number gets a random plausible number
    of similar shape via _pseudo_amount() (which itself falls back to full
    text replacement for anything that doesn't actually parse as one) --
    detecting numeric CELLS rather than matching amount/quantity/duration-
    ish column NAMES against a keyword list, which kept needing a new
    keyword added every time another real export turned up one more
    numeric column it didn't cover (Amazon alone: Total Owed, Quantity,
    Duration Ms, Book Length Milliseconds, total_reading_millis...).
    Without any of this, a CSV export would have its Order Date and Total
    Owed columns turned into nonsense sentences right alongside the
    genuinely sensitive Product Name column, leaving nothing usable to
    verify date/amount parsing logic against.
    `escapechar`: how the platform's importer reads this file (LinkedIn's
    Comments.csv, linkedin.csv_escapechar); written back standard."""
    reader = csv.reader(io.StringIO(text), escapechar=escapechar)
    rows = list(reader)
    if not rows:
        return text
    out = io.StringIO()
    writer = csv.writer(out)
    # LinkedIn's Connections.csv opens with "Notes:", a note and a blank
    # line before its header row: kept in that shape (the note itself
    # replaced, like any text), so the header stays a header.
    start = 0
    if len(rows[0]) == 1 and rows[0][0].strip().endswith(":"):
        blank = next((i for i, r in enumerate(rows) if i and not any(c.strip() for c in r)), None)
        if blank is not None:
            writer.writerow(rows[0])
            for row in rows[1:blank]:
                writer.writerow([anonymizer._pseudo_text(c) if c.strip() else c for c in row])
            writer.writerow([])
            start = blank + 1
    if start >= len(rows):
        return out.getvalue()
    header = rows[start]
    header_lower = [h.strip().lower() for h in header]
    writer.writerow(header)
    for row in rows[start + 1:]:
        new_row = []
        for i, cell in enumerate(row):
            if not cell:
                new_row.append(cell)
                continue
            col = header_lower[i] if i < len(header_lower) else ""
            # "time" alone also catches "timestamp" (Kindle's
            # start_timestamp/end_timestamp, missing a "date" substring)
            # and "Start Time"/"Device Record Time"-style columns seen
            # across several real Amazon files.
            # LinkedIn names its date columns "Connected On", "Sent At"...
            if "date" in col or "time" in col or col.endswith((" on", " at")):
                new_row.append(str(anonymizer._jitter_timestamp(cell)))
            else:
                new_row.append(anonymizer._pseudo_amount(cell))
        writer.writerow(new_row)
    return out.getvalue()


def _new_relpath(anonymizer: Anonymizer, paths: PathAnonymizer, rel: str, source_key: Optional[str]) -> str:
    new_rel = paths.rename_relpath(rel)
    if source_key == "linkedin":
        # LinkedIn's Complete export names its activity files after the
        # member's id ("Shares_387121575.csv"): replaced like any identifier,
        # the same fake id on every file.
        new_rel = linkedin.MEMBER_ID_SUFFIX_RE.sub(lambda m: "_" + anonymizer._pseudo_id(m.group()[1:]), new_rel)
    return new_rel


def _anonymize_one_file(zf: zipfile.ZipFile, anonymizer: Anonymizer, paths: PathAnonymizer, root: Path, path: Path,
                        source_key: Optional[str] = None) -> None:
    rel = path.relative_to(root).as_posix()
    new_rel = _new_relpath(anonymizer, paths, rel, source_key)
    suffix = path.suffix.lower()

    if suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            zf.writestr(new_rel, b"")
            return

        # Google Photos sidecars resolve their sibling media file by
        # exact filename match on "title". Filenames are never
        # renamed, so "title" must be preserved verbatim too, or the
        # anonymized copy stops resolving its own photos.
        if path.name.endswith(".supplemental-metadata.json") and isinstance(data, dict) and "title" in data:
            data = dict(data)
            real_title = data.pop("title")
            scrubbed = anonymizer.walk("", data)
            scrubbed["title"] = real_title
        else:
            scrubbed = anonymizer.walk("", data)

        zf.writestr(new_rel, json.dumps(scrubbed, ensure_ascii=False, indent=2))
        return

    if suffix in MEDIA_EXTENSIONS:
        zf.writestr(new_rel, PLACEHOLDER_MEDIA_BYTES)
        return

    if suffix == ".csv":
        try:
            escapechar = linkedin.csv_escapechar(path) if source_key == "linkedin" else None
            zf.writestr(new_rel, _anonymize_csv(anonymizer, path.read_text(encoding="utf-8", errors="replace"), escapechar))
            return
        except Exception:
            pass

    # Unknown file type (including .html/.txt, and large binary archives
    # like a Gmail .mbox export, all of which can carry large amounts of
    # free-form personal content): keep the entry so folder layout /
    # detect() heuristics still work, but never carry its real content --
    # and never read it either, since an .mbox can be many gigabytes.
    zf.writestr(new_rel, b"")


def anonymize_export(root: Path, dest_zip: Path, source_key: Optional[str] = None) -> list[str]:
    """Returns the (relative path, error) pairs for any file that could not
    be anonymized -- one bad/huge/oddly-encoded file used to abort the whole
    job with no partial result and, worse, no visible reason (the exception
    was recorded to a `.error` marker on disk that the UI never displayed).
    Now a single problem file is skipped -- written empty, the safe default
    for anything we can't confidently process -- rather than losing the rest
    of a potentially very large, slow-to-regenerate archive."""
    corrupt_encoding = source_key in ("facebook", "instagram")
    paths = PathAnonymizer(Faker("fr_FR"))
    anonymizer = Anonymizer(paths, corrupt_encoding=corrupt_encoding)
    failures: list[str] = []

    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in root.rglob("*"):
            if path.is_dir():
                continue
            try:
                _anonymize_one_file(zf, anonymizer, paths, root, path, source_key)
            except Exception as exc:
                rel = path.relative_to(root).as_posix()
                failures.append(f"{rel}: {exc}")
                try:
                    zf.writestr(_new_relpath(anonymizer, paths, rel, source_key), b"")
                except Exception:
                    pass

    return failures


# ────────────────────────────────────────────────
# Background job + on-disk state (no DB schema needed: presence of the
# .building / .error / .zip marker files under data/anonymized/ IS the state)
# ────────────────────────────────────────────────

def run_anonymize_job(batch_id: int, extract_dir: Path, source_key: Optional[str]) -> None:
    building = anonymized_artifact(batch_id, ".building")
    error = anonymized_artifact(batch_id, ".error")
    warnings = anonymized_artifact(batch_id, ".warnings")
    zip_path = anonymized_artifact(batch_id, ".zip")
    tmp_path = anonymized_artifact(batch_id, ".zip.tmp")

    error.unlink(missing_ok=True)
    warnings.unlink(missing_ok=True)
    building.touch()
    try:
        failures = anonymize_export(extract_dir, tmp_path, source_key=source_key)
        tmp_path.replace(zip_path)
        if failures:
            # The job still succeeded (a usable archive exists) -- these are
            # just the files that got skipped/emptied instead of properly
            # anonymized, kept around so a "ready" result can still explain
            # why it might be missing something instead of looking perfect.
            warnings.write_text("\n".join(failures)[:20000], encoding="utf-8")
    except Exception:
        import traceback
        tmp_path.unlink(missing_ok=True)
        error.write_text(traceback.format_exc()[-4000:], encoding="utf-8")
    finally:
        building.unlink(missing_ok=True)


def get_state(batch_id: int) -> str:
    if anonymized_artifact(batch_id, ".building").exists():
        return "building"
    if anonymized_artifact(batch_id, ".error").exists():
        return "error"
    if anonymized_artifact(batch_id, ".zip").exists():
        return "ready"
    return "none"


def get_error_detail(batch_id: int) -> Optional[str]:
    path = anonymized_artifact(batch_id, ".error")
    return path.read_text(encoding="utf-8") if path.exists() else None


def get_warnings(batch_id: int) -> Optional[str]:
    path = anonymized_artifact(batch_id, ".warnings")
    return path.read_text(encoding="utf-8") if path.exists() else None


def delete_artifacts(batch_id: int) -> None:
    for suffix in (".zip", ".zip.tmp", ".building", ".error", ".warnings"):
        anonymized_artifact(batch_id, suffix).unlink(missing_ok=True)
