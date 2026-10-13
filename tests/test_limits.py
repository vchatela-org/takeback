"""The budgets on untrusted archives: per-file, per-batch, and what an archive
may expand into (app/limits.py)."""
import asyncio
import os
import tempfile
import zipfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="tb-test-")
os.environ.setdefault("TAKEBACK_DATABASE_URL", f"sqlite:///{_TMP}/test.db")
os.environ.setdefault("TAKEBACK_DATA_DIR", _TMP)

import pytest  # noqa: E402

from app import limits  # noqa: E402
from app.config import settings  # noqa: E402


@pytest.fixture
def budget(monkeypatch, tmp_path):
    """A tiny budget, so the tests stay about the check rather than about
    writing gigabytes."""
    monkeypatch.setattr(settings, "max_upload_mb", 1)          # 1 MiB per file
    monkeypatch.setattr(settings, "max_batch_upload_mb", 2)   # 2 MiB per batch
    monkeypatch.setattr(settings, "max_extract_gb", 1 / 1024)  # 1 MiB extracted
    return tmp_path


class FakeUpload:
    """Just enough of `UploadFile` for `stream_upload`: an async `read`."""

    def __init__(self, content: bytes):
        self._content = content
        self._pos = 0

    async def read(self, size: int = -1) -> bytes:
        chunk = self._content[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk


def write_zip(path: Path, entries) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in entries:
            zf.writestr(name, payload)
    return path


# --- Upload size -------------------------------------------------------------

def test_a_file_under_its_budget_passes(budget):
    limits.check_upload_size("export.zip", 512 * 1024)


def test_a_file_over_its_budget_is_refused(budget):
    with pytest.raises(limits.UploadTooLarge):
        limits.check_upload_size("export.zip", 2 * 1024 * 1024)


def test_the_batch_budget_counts_every_file_of_the_import(budget, tmp_path):
    first = tmp_path / "a.zip"
    first.write_bytes(b"x" * (1024 * 1024))
    # One file is fine, and so is a batch exactly at the limit.
    limits.check_batch_upload_size([first])
    second = tmp_path / "b.zip"
    second.write_bytes(b"x" * (1024 * 1024))
    limits.check_batch_upload_size([first, second])
    # One byte more is not, though each file is well under the per-file cap --
    # an import is several zips, which is the whole reason for this check.
    third = tmp_path / "c.zip"
    third.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(limits.UploadTooLarge):
        limits.check_batch_upload_size([first, second, third])


# --- Upload streaming --------------------------------------------------------

def test_stream_upload_writes_the_file(budget, tmp_path):
    dest = tmp_path / "up.zip"
    written = asyncio.run(limits.stream_upload(FakeUpload(b"hello"), dest, "export.zip"))
    assert written == 5
    assert dest.read_bytes() == b"hello"


def test_stream_upload_refuses_an_oversized_body_and_leaves_nothing_behind(budget, tmp_path):
    dest = tmp_path / "up.zip"
    with pytest.raises(limits.UploadTooLarge):
        asyncio.run(limits.stream_upload(FakeUpload(b"x" * (2 * 1024 * 1024)), dest, "export.zip"))
    # A truncated archive must not stay on disk to be picked up as a whole one.
    assert not dest.exists()


def test_stream_upload_cleans_up_when_the_request_is_cancelled(budget, tmp_path):
    class Cancelled(FakeUpload):
        async def read(self, size: int = -1) -> bytes:
            raise asyncio.CancelledError()

    dest = tmp_path / "up.zip"
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(limits.stream_upload(Cancelled(b""), dest, "export.zip"))
    assert not dest.exists()


# --- Extraction --------------------------------------------------------------

def test_extract_archive_writes_the_tree(budget, tmp_path):
    archive = write_zip(tmp_path / "a.zip", [("export/posts.json", b"[]")])
    dest = tmp_path / "out"
    dest.mkdir()
    limits.extract_archive([archive], dest)
    assert (dest / "export" / "posts.json").read_bytes() == b"[]"


def test_extract_archive_refuses_a_path_climbing_out_of_the_destination(budget, tmp_path):
    archive = write_zip(tmp_path / "a.zip", [("../escaped.json", b"{}")])
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(limits.UploadTooLarge):
        limits.extract_archive([archive], dest)
    assert not (tmp_path / "escaped.json").exists()


def test_extract_archive_refuses_what_expands_past_the_budget(budget, tmp_path):
    # Highly compressible, so the archive stays small while the expansion does
    # not: the shape of a zip bomb, and what the per-file cap cannot catch.
    archive = write_zip(tmp_path / "bomb.zip", [("big.txt", b"\0" * (2 * 1024 * 1024))])
    assert archive.stat().st_size < 1024 * 1024
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(limits.UploadTooLarge):
        limits.extract_archive([archive], dest)


def test_extract_archive_does_not_trust_a_declared_size(budget, tmp_path):
    """The central directory can understate a member's real length; the running
    count of bytes actually decompressed is what the limit is tested against."""
    archive = write_zip(tmp_path / "liar.zip", [("big.txt", b"\0" * (2 * 1024 * 1024))])
    with zipfile.ZipFile(archive, "a") as zf:
        info = zf.infolist()[0]
    # Rewrite the declared uncompressed size to a lie, in place.
    raw = bytearray(archive.read_bytes())
    raw[info.header_offset + 22:info.header_offset + 26] = (10).to_bytes(4, "little")
    archive.write_bytes(bytes(raw))
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(limits.UploadTooLarge):
        limits.extract_archive([archive], dest)


# --- Wiring: the routes use it -----------------------------------------------

def test_the_api_refuses_an_oversized_upload_and_leaves_no_import_behind(budget, tmp_path):
    """The endpoint, not just the helper: reading `batch.id` after deleting the
    batch raised ObjectDeletedError and turned this into a 500."""
    from fastapi.testclient import TestClient

    from app import crud
    from app.database import Base, SessionLocal, engine
    from app.main import app

    Base.metadata.create_all(engine)
    client = TestClient(app, follow_redirects=False)
    db = SessionLocal()
    before = len(crud.get_batches(db))
    db.close()

    response = client.post(
        "/api/imports/",
        files={"files": ("big.zip", b"x" * (2 * 1024 * 1024), "application/zip")},
    )
    assert response.status_code == 413

    db = SessionLocal()
    after = len(crud.get_batches(db))
    db.close()
    # The half-written import must not be left listed as an empty one.
    assert after == before


def test_the_web_form_refuses_an_oversized_upload_too(budget, tmp_path):
    from fastapi.testclient import TestClient

    from app import crud
    from app.database import Base, SessionLocal, engine
    from app.main import app

    Base.metadata.create_all(engine)
    client = TestClient(app, follow_redirects=False)
    db = SessionLocal()
    before = len(crud.get_batches(db))
    db.close()

    response = client.post(
        "/imports",
        files={"files": ("big.zip", b"x" * (2 * 1024 * 1024), "application/zip")},
    )
    assert response.status_code == 413

    db = SessionLocal()
    after = len(crud.get_batches(db))
    db.close()
    assert after == before


def test_a_normal_upload_still_goes_through(budget, tmp_path):
    """The guard must not refuse what it exists to let in."""
    from fastapi.testclient import TestClient

    from app.database import Base, engine
    from app.main import app

    Base.metadata.create_all(engine)
    client = TestClient(app)

    archive = write_zip(tmp_path / "ok.zip", [("linkedin/Profile.csv", b"headline\n")])
    response = client.post(
        "/api/imports/",
        files={"files": ("ok.zip", archive.read_bytes(), "application/zip")},
    )
    assert response.status_code == 201
