"""Receipt image storage.

Behind a Protocol so object storage can replace the local filesystem in
production without touching the pipeline. Receipt images are personal
financial records: paths are derived from the content hash, never from
user-supplied filenames, so nothing a client sends can influence where a file
lands on disk.
"""

from pathlib import Path
from typing import Protocol

from ns.config import get_settings
from ns.logging import get_logger

log = get_logger(__name__)


class ReceiptStorage(Protocol):
    """Content-addressed blob store for receipt images."""

    def write(self, sha256: str, extension: str, data: bytes) -> str:
        """Persist bytes and return an opaque storage key."""
        ...

    def read(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...


class LocalReceiptStorage:
    """Filesystem-backed storage rooted at `settings.receipt_storage_path`."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or get_settings().receipt_storage_path
        self._root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, key: str) -> Path:
        resolved = (self._root / key).resolve()
        root = self._root.resolve()
        # Defence in depth. Keys are generated from content hashes, so this
        # should be unreachable — but a storage layer that can be talked into
        # writing outside its root is the kind of bug worth making impossible.
        if not resolved.is_relative_to(root):
            raise ValueError(f"Storage key escapes the storage root: {key!r}")
        return resolved

    def write(self, sha256: str, extension: str, data: bytes) -> str:
        # Shard on the first two hex characters. With years of receipts plus
        # bulk backfill, a single flat directory gets unpleasant to work with.
        key = f"{sha256[:2]}/{sha256}.{extension}"
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)

        if path.exists():
            # Same content hash means identical bytes; rewriting is pointless.
            log.debug("storage.write_skipped_existing", key=key)
            return key

        # Write to a temporary file and rename, so a crash mid-write cannot
        # leave a truncated image at a path the database already references.
        tmp = path.with_suffix(path.suffix + ".partial")
        tmp.write_bytes(data)
        tmp.replace(path)
        log.info("storage.write", key=key, bytes=len(data))
        return key

    def read(self, key: str) -> bytes:
        return self._path_for(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path_for(key).is_file()

    def delete(self, key: str) -> None:
        self._path_for(key).unlink(missing_ok=True)


def get_storage() -> ReceiptStorage:
    return LocalReceiptStorage()
