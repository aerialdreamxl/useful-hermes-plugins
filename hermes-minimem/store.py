"""SQLite storage: one table carrying dense and sparse embeddings independently.

Why SQLite and not a JSON file (measured, PROMPT.md 3.1 / 3.2):
  * append cost: JSON rewrites the whole file (245 ms at 20k entries, 935 ms at 50k);
    SQLite stays at 4-6 ms regardless of size;
  * two processes appending concurrently: JSON lost 50/600 entries and raised WinError 32;
    SQLite WAL lost none.

v0.8 schema v2 adds backend-aware dense vector columns and sparse JSON columns. Dense vectors
remain int8 BLOBs; sqlite-vec was measured 225x slower on KNN and 43x slower on writes, so
there is still no virtual table here. Schema v1 is backed up before an additive migration.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .results import pack_sparse, unpack_sparse

SCHEMA_VERSION = "2"
LEGACY_BACKEND = "openai"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mem(
  id             TEXT PRIMARY KEY,
  text           TEXT NOT NULL,
  kind           TEXT NOT NULL,
  imp            REAL NOT NULL DEFAULT 0.5,
  created        TEXT NOT NULL,
  updated        TEXT,
  source         TEXT,
  vec            BLOB NOT NULL,
  vec_model      TEXT NOT NULL,
  vec_dim        INTEGER NOT NULL,
  vec_backend    TEXT NOT NULL DEFAULT 'openai',
  sparse_json    TEXT,
  sparse_model   TEXT,
  sparse_backend TEXT,
  output_type    TEXT NOT NULL DEFAULT 'dense'
);
CREATE INDEX IF NOT EXISTS idx_mem_created ON mem(created);
CREATE INDEX IF NOT EXISTS idx_mem_backend_vector ON mem(vec_backend, vec_model, vec_dim);
CREATE INDEX IF NOT EXISTS idx_mem_sparse_vector ON mem(sparse_backend, sparse_model);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""

MAX_TEXT = 8000  # hard cap so one pathological entry cannot blow up the prompt


def new_id() -> str:
    """``mm_<epoch_ms>_<8 hex>`` — monotonic-ish and collision-free enough for one machine."""
    return f"mm_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


def pack_vector(vec: Iterable[float]) -> bytes:
    """float vector (assumed unit-ish) -> int8 bytes. Values are clipped, not wrapped.

    Deliberately numpy-free: a provider running on the keyword-only tier (numpy missing)
    must still be able to WRITE memories, otherwise a missing dependency silently becomes a
    read-only store. Pure Python over 1024 dimensions costs well under a millisecond.
    """
    values = list(vec)
    out = bytearray(len(values))
    for i, raw in enumerate(values):
        scaled = int(round(float(raw) * 127.0))
        if scaled > 127:
            scaled = 127
        elif scaled < -127:
            scaled = -127
        out[i] = scaled & 0xFF
    return bytes(out)


def unpack_vector(blob: bytes) -> "Any":
    """int8 bytes -> float32 array scaled back to [-1, 1]."""
    import numpy as np

    return np.frombuffer(blob, dtype=np.int8).astype(np.float32) / 127.0


class Store:
    """Thread-safe wrapper around one SQLite file. One connection, guarded by a lock.

    WAL + busy_timeout make concurrent processes work; the in-process lock keeps a
    single connection (and its transaction state) sane across writer/reader threads.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("PRAGMA busy_timeout=10000")
            # Switching the journal mode needs an exclusive moment; with two processes
            # opening the file at once it can lose the race. WAL is persistent once set,
            # so a failure here just means somebody else already did it.
            try:
                cur.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass
            cur.execute("PRAGMA synchronous=NORMAL")
            stored = self._stored_schema_version(cur)
            if stored in (None, ""):
                self._retry(lambda: cur.executescript(_SCHEMA))
                self._conn.commit()
                self.set_meta("schema_version", SCHEMA_VERSION)
            elif stored == "1":
                self._backup_v1(path)
                self._migrate_v1(cur)
            elif stored != SCHEMA_VERSION:
                # PROMPT.md §4.3: a version we do not know must fail loudly. Silently reading
                # a future layout is how a column rename becomes data corruption.
                raise RuntimeError(
                    f"minimem.db schema_version is {stored!r}, this build expects "
                    f"{SCHEMA_VERSION!r}. Back up the file, then run `hermes hermes-minimem reindex` "
                    "with the matching plugin version (see ACCEPTANCE.md)."
                )
            self._ensure_v2_columns(cur)
            self._writes_since_checkpoint = 0

    @staticmethod
    def _stored_schema_version(cur: sqlite3.Cursor) -> Optional[str]:
        tables = {row[0] for row in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "meta" not in tables:
            return None
        row = cur.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
        return row[0] if row else None

    @staticmethod
    def _backup_v1(path: str) -> None:
        stamp = time.strftime("%Y%m%dT%H%M%S")
        backup = os.path.join(os.path.dirname(os.path.abspath(path)) or ".",
                              f"{os.path.basename(path)}.v1-backup-{stamp}.sqlite")
        if os.path.exists(backup):  # a crash/retry in the same second must not overwrite a copy
            backup = f"{backup}.1"
        # Best effort: fold the live WAL into the file before copying, so the backup is not
        # missing the most recent writes of a sibling process.
        try:
            conn = sqlite3.connect(path, timeout=5)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
        except sqlite3.Error:
            pass
        shutil.copy2(path, backup)

    def _migrate_v1(self, cur: sqlite3.Cursor) -> None:
        try:
            self._retry(lambda: cur.executescript("""
                ALTER TABLE mem ADD COLUMN vec_backend TEXT;
                ALTER TABLE mem ADD COLUMN sparse_json TEXT;
                ALTER TABLE mem ADD COLUMN sparse_model TEXT;
                ALTER TABLE mem ADD COLUMN sparse_backend TEXT;
                ALTER TABLE mem ADD COLUMN output_type TEXT;
            """))
        except sqlite3.OperationalError as exc:
            # A sibling process (e.g. the CLI while the gateway boots) may have added the
            # columns between our meta read and this ALTER. "duplicate column" means the
            # schema is already (partly) v2: fall through — _ensure_v2_columns completes
            # whatever is missing, guarded by PRAGMA table_info.
            if "duplicate column" not in str(exc).lower():
                raise
        self._retry(lambda: cur.executescript(f"""
            UPDATE mem SET vec_backend=COALESCE(vec_backend, '{LEGACY_BACKEND}'),
                           output_type=COALESCE(output_type, 'dense');
            CREATE INDEX IF NOT EXISTS idx_mem_backend_vector ON mem(vec_backend, vec_model, vec_dim);
            CREATE INDEX IF NOT EXISTS idx_mem_sparse_vector ON mem(sparse_backend, sparse_model);
        """))
        self._conn.commit()
        self.set_meta("schema_version", SCHEMA_VERSION)

    def _ensure_v2_columns(self, cur: sqlite3.Cursor) -> None:
        existing = {row[1] for row in cur.execute("PRAGMA table_info(mem)").fetchall()}
        column_sql = {
            "vec_backend": "ALTER TABLE mem ADD COLUMN vec_backend TEXT NOT NULL DEFAULT 'openai'",
            "sparse_json": "ALTER TABLE mem ADD COLUMN sparse_json TEXT",
            "sparse_model": "ALTER TABLE mem ADD COLUMN sparse_model TEXT",
            "sparse_backend": "ALTER TABLE mem ADD COLUMN sparse_backend TEXT",
            "output_type": "ALTER TABLE mem ADD COLUMN output_type TEXT NOT NULL DEFAULT 'dense'",
        }
        for name, sql in column_sql.items():
            if name not in existing:
                self._retry(lambda sql=sql: cur.execute(sql))
        self._retry(lambda: cur.executescript("""
            CREATE INDEX IF NOT EXISTS idx_mem_backend_vector ON mem(vec_backend, vec_model, vec_dim);
            CREATE INDEX IF NOT EXISTS idx_mem_sparse_vector ON mem(sparse_backend, sparse_model);
        """))
        self._conn.commit()

    @staticmethod
    def _retry(fn, attempts: int = 4, delays=(0.05, 0.2, 0.8)):
        """Run ``fn``, retrying on "database is locked" with backoff (PROMPT.md 6).

        SQLite's busy_timeout covers most contention, but a concurrent schema write or a
        checkpoint runs in a context where it does not apply; retrying is the only way to
        keep two live processes from losing a write.
        """
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                return fn()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                last = exc
                if attempt < attempts - 1:
                    time.sleep(delays[min(attempt, len(delays) - 1)])
        raise last  # type: ignore[misc]

    # -- meta --------------------------------------------------------------
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, str(value)),
            )
            self._conn.commit()

    def _write(self, fn):
        """Run one write closure under the connection lock, retrying on lock contention."""
        with self._lock:
            result = self._retry(fn)
            self._after_write()
            return result

    def _do_execute(self, sql: str, params) -> sqlite3.Cursor:
        """execute + commit as one retryable unit (a partial write must not be left open)."""
        def run():
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur
        return self._write(run)

    # -- writes ------------------------------------------------------------
    def add(
        self,
        text: str,
        kind: str,
        vec: bytes,
        vec_model: str,
        vec_dim: int,
        *,
        imp: float = 0.5,
        source: Optional[Dict[str, Any]] = None,
        mem_id: Optional[str] = None,
        sparse: Optional[Sequence[Tuple[int, float]]] = None,
        sparse_model: Optional[str] = None,
        backend: str = LEGACY_BACKEND,
        output_type: str = "dense",
    ) -> str:
        mem_id = mem_id or new_id()
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self._do_execute(
            "INSERT INTO mem(id,text,kind,imp,created,updated,source,vec,vec_model,vec_dim,"
            "vec_backend,sparse_json,sparse_model,sparse_backend,output_type)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                mem_id,
                text[:MAX_TEXT],
                kind,
                float(imp),
                now,
                None,
                json.dumps(source, ensure_ascii=False) if source else None,
                vec,
                vec_model,
                int(vec_dim),
                backend,
                pack_sparse(sparse),
                sparse_model if sparse is not None else None,
                backend if sparse is not None else None,
                output_type,
            ),
        )
        return mem_id

    def add_many(self, rows: List[Tuple[Any, ...]]) -> int:
        """Bulk insert within one transaction.

        Legacy rows are ``(id, text, kind, imp, source, vec, model, dim)`` and default to
        OpenAI-compatible dense output. New rows may append sparse, sparse_model, backend and
        output_type for explicit control.
        """
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        payload = []
        for row in rows:
            mem_id, text, kind, imp, source, vec, model, dim = row[:8]
            sparse, sparse_model, backend, output_type = (
                row[8] if len(row) > 8 else None,
                row[9] if len(row) > 9 else None,
                row[10] if len(row) > 10 else LEGACY_BACKEND,
                row[11] if len(row) > 11 else "dense",
            )
            payload.append((
                mem_id, str(text)[:MAX_TEXT], kind, float(imp), now, source,
                vec, model, int(dim), backend, pack_sparse(sparse),
                sparse_model if sparse is not None else None,
                backend if sparse is not None else None,
                output_type,
            ))

        def run():
            self._conn.executemany(
                "INSERT OR REPLACE INTO mem(id,text,kind,imp,created,updated,source,vec,vec_model,"
                "vec_dim,vec_backend,sparse_json,sparse_model,sparse_backend,output_type)"
                " VALUES(?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,?)",
                payload,
            )
            self._conn.commit()

        self._write(run)
        return len(rows)

    def update(
        self,
        mem_id: str,
        *,
        text: Optional[str] = None,
        vec: Optional[bytes] = None,
        vec_model: Optional[str] = None,
        vec_dim: Optional[int] = None,
        imp: Optional[float] = None,
        kind: Optional[str] = None,
        sparse: Optional[Sequence[Tuple[int, float]]] = None,
        sparse_model: Optional[str] = None,
        backend: Optional[str] = None,
        output_type: Optional[str] = None,
    ) -> bool:
        if vec is not None and vec_dim is not None and len(vec) != vec_dim:
            # A length disagreement means a caller bug; refuse to store a corrupt blob
            # rather than poisoning the int8 matrix for every dense read.
            raise ValueError(f"vec length {len(vec)} != vec_dim {vec_dim}")
        if sparse is not None and backend is None:
            with self._lock:
                row = self._conn.execute("SELECT vec_backend FROM mem WHERE id=?", (mem_id,)).fetchone()
            if row is not None:
                backend = row["vec_backend"] or LEGACY_BACKEND
        sets, args = [], []
        for column, value in (
            ("text", text[:MAX_TEXT] if text is not None else None),
            ("vec", vec),
            ("vec_model", vec_model),
            ("vec_dim", vec_dim),
            ("imp", imp),
            ("kind", kind),
            ("sparse_json", pack_sparse(sparse) if sparse is not None else None),
            ("sparse_model", sparse_model if sparse is not None else None),
            ("vec_backend", backend),
            ("sparse_backend", backend if sparse is not None else None),
            ("output_type", output_type),
        ):
            if value is not None:
                sets.append(f"{column}=?")
                args.append(value)
        if not sets:
            return False
        sets.append("updated=?")
        args.append(time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        args.append(mem_id)
        return self._do_execute(f"UPDATE mem SET {', '.join(sets)} WHERE id=?", args).rowcount > 0

    def delete(self, mem_id: str) -> bool:
        return self._do_execute("DELETE FROM mem WHERE id=?", (mem_id,)).rowcount > 0

    def delete_by_text(self, text: str, *, exclude_origin: Optional[str] = None) -> int:
        """Delete by exact text, optionally skipping one origin (mirror vs tool ownership)."""
        sql = "DELETE FROM mem WHERE text=?"
        args: List[Any] = [text]
        if exclude_origin:
            sql += " AND COALESCE(json_extract(source,'$.origin'),'') <> ?"
            args.append(exclude_origin)
        return self._do_execute(sql, args).rowcount

    def _after_write(self) -> None:
        """Checkpoint the WAL every 100 writes so it cannot grow without bound."""
        self._writes_since_checkpoint = getattr(self, "_writes_since_checkpoint", 0) + 1
        if self._writes_since_checkpoint >= 100:
            self._writes_since_checkpoint = 0
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass

    # -- reads -------------------------------------------------------------
    def get(self, mem_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM mem WHERE id=?", (mem_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def find_by_text(
        self, text: str, *, exclude_origin: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Newest row with this exact text.

        ``exclude_origin`` implements the ownership split from PROMPT.md §6.5: a mirror
        event must never match a row a tool wrote, even when the texts are identical.
        """
        sql = "SELECT * FROM mem WHERE text=?"
        args: List[Any] = [text]
        if exclude_origin:
            sql += " AND COALESCE(json_extract(source,'$.origin'),'') <> ?"
            args.append(exclude_origin)
        sql += " ORDER BY rowid DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        return _row_to_dict(row) if row else None

    def all_ids_texts(self) -> List[Tuple[str, str, float]]:
        """``(id, text, importance)`` for every row in rowid order (BM25 row order)."""
        with self._lock:
            rows = self._conn.execute("SELECT id, text, imp FROM mem ORDER BY rowid").fetchall()
        return [(r["id"], r["text"], float(r["imp"])) for r in rows]

    def all_vectors(self, model: str, dim: int, backend: Optional[str] = None
                    ) -> Tuple[List[str], List[str], bytes]:
        """``(ids, texts, concatenated int8 blob)`` for rows matching the dense basis.

        Rows with another model/dim/backend are excluded here rather than scored against the
        wrong basis — mixing embedding spaces silently is worse than losing recall on them.
        """
        sql = ("SELECT id, text, vec FROM mem WHERE vec_model=? AND vec_dim=? AND vec_dim>0")
        args: List[Any] = [model, dim]
        if backend is not None:
            sql += " AND vec_backend=?"
            args.append(backend)
        sql += " ORDER BY rowid"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        ids = [r["id"] for r in rows]
        texts = [r["text"] for r in rows]
        return ids, texts, b"".join(r["vec"] for r in rows)

    def all_sparse(self, sparse_model: str, backend: Optional[str] = None
                   ) -> List[Tuple[str, List[Tuple[int, float]]]]:
        """``(id, sparse pairs)`` in BM25 row order for one sparse embedding basis."""
        sql = "SELECT id, sparse_json FROM mem WHERE sparse_model=? AND sparse_json IS NOT NULL"
        args: List[Any] = [sparse_model]
        if backend is not None:
            sql += " AND sparse_backend=?"
            args.append(backend)
        sql += " ORDER BY rowid"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [(r["id"], unpack_sparse(r["sparse_json"])) for r in rows]

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) c FROM mem").fetchone()["c"])

    def _stale_where(
        self,
        model: str,
        dim: int,
        *,
        backend: Optional[str] = None,
        wants_dense: bool = True,
        wants_model_sparse: bool = False,
        sparse_model: Optional[str] = None,
    ) -> Tuple[str, List[Any]]:
        """Shared predicate behind count_stale/stale_ids (keep both in lockstep)."""
        sql = "1=1"
        args: List[Any] = []
        if wants_dense:
            sql += " AND (vec_model<>? OR vec_dim<>? OR vec_dim=0 OR vec IS NULL"
            args.extend([model, dim])
            if backend is not None:
                sql += " OR vec_backend<>?"
                args.append(backend)
            sql += ")"
        if wants_model_sparse:
            sql += " AND (sparse_json IS NULL OR sparse_model<>?"
            args.append(sparse_model or "")
            if backend is not None:
                sql += " OR sparse_backend<>?"
                args.append(backend)
            sql += ")"
        return sql, args

    def count_stale(
        self,
        model: str,
        dim: int,
        *,
        backend: Optional[str] = None,
        wants_dense: bool = True,
        wants_model_sparse: bool = False,
        sparse_model: Optional[str] = None,
    ) -> int:
        where, args = self._stale_where(
            model, dim, backend=backend, wants_dense=wants_dense,
            wants_model_sparse=wants_model_sparse, sparse_model=sparse_model,
        )
        with self._lock:
            return int(self._conn.execute(f"SELECT COUNT(*) c FROM mem WHERE {where}", args).fetchone()["c"])

    def stale_ids(
        self,
        model: str,
        dim: int,
        *,
        backend: Optional[str] = None,
        wants_dense: bool = True,
        wants_model_sparse: bool = False,
        sparse_model: Optional[str] = None,
    ) -> List[str]:
        where, args = self._stale_where(
            model, dim, backend=backend, wants_dense=wants_dense,
            wants_model_sparse=wants_model_sparse, sparse_model=sparse_model,
        )
        with self._lock:
            rows = self._conn.execute(f"SELECT id FROM mem WHERE {where} ORDER BY rowid", args).fetchall()
        return [r["id"] for r in rows]

    def recent(self, limit: int = 5) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM mem ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [_row_to_dict(r) for r in rows]

    def db_size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    @contextmanager
    def transaction(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._conn.close()


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    data = dict(row)
    if data.get("source"):
        try:
            data["source"] = json.loads(data["source"])
        except (ValueError, TypeError):
            pass
    data["sparse"] = unpack_sparse(data.get("sparse_json"))
    data["vec_backend"] = data.get("vec_backend") or LEGACY_BACKEND
    data["output_type"] = data.get("output_type") or "dense"
    return data
