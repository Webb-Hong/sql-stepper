"""DuckDB session: owns the connection, sample data, imports and value encoding."""

from __future__ import annotations

import datetime as dt
import decimal
import math
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from .samples import SAMPLE_SQL

FETCH_LIMIT = 100_000  # rows pulled into Python per step query


@dataclass
class Rows:
    """A query result held in Python."""

    columns: list[str]
    rows: list[tuple]
    overflow: bool = False  # more than FETCH_LIMIT rows existed

    def split_meta(self) -> tuple["Rows", dict[str, list]]:
        """Separate visible columns from internal `__name` bookkeeping columns."""
        keep = [i for i, c in enumerate(self.columns) if not c.startswith("__")]
        meta_idx = {c: i for i, c in enumerate(self.columns) if c.startswith("__")}
        visible = Rows([self.columns[i] for i in keep], [tuple(r[i] for i in keep) for r in self.rows], self.overflow)
        meta = {c: [r[i] for r in self.rows] for c, i in meta_idx.items()}
        return visible, meta

    def subset(self, flags: list[bool]) -> "Rows":
        return Rows(self.columns, [r for r, f in zip(self.rows, flags) if f], self.overflow)


def to_json_value(v):
    """Convert a DuckDB Python value into something JSON can carry faithfully."""
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, int):
        return v if abs(v) < 2**53 else str(v)
    if isinstance(v, float):
        return v if math.isfinite(v) else str(v)
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat(sep=" ") if isinstance(v, dt.datetime) else v.isoformat()
    if isinstance(v, dt.timedelta):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(v).hex()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (list, tuple)):
        return [to_json_value(x) for x in v]
    if isinstance(v, dict):
        return {str(k): to_json_value(x) for k, x in v.items()}
    return str(v)


def sanitize_name(name: str) -> str:
    name = re.sub(r"\W+", "_", Path(name).stem).strip("_").lower()
    if not name:
        name = "imported"
    return "t_" + name if name[0].isdigit() else name


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@dataclass
class Session:
    """One DuckDB connection shared by the web UI (guarded by a lock)."""

    path: str = ":memory:"
    load_samples: bool = True
    con: duckdb.DuckDBPyConnection = field(init=False)
    lock: threading.RLock = field(default_factory=threading.RLock, init=False)

    def __post_init__(self):
        self.con = duckdb.connect(self.path)
        self.home = self.con.execute("SELECT current_database()").fetchone()[0]
        if self.load_samples:
            self.reset_samples(drop_all=False)

    # ---- querying -------------------------------------------------------
    def query(self, sql: str, limit: int = FETCH_LIMIT) -> Rows:
        cur = self.con.execute(sql)
        if cur.description is None:
            return Rows([], [])
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(limit + 1)
        overflow = len(rows) > limit
        return Rows(cols, rows[:limit], overflow)

    @contextmanager
    def sandbox(self):
        """Run inside a throwaway copy of the database so DML/DDL leaves no trace."""
        self.con.execute("ATTACH ':memory:' AS __sandbox")
        try:
            self.con.execute(f"COPY FROM DATABASE {quote_ident(self.home)} TO __sandbox")
            self.con.execute("USE __sandbox")
            yield
        finally:
            self.con.execute(f"USE {quote_ident(self.home)}")
            self.con.execute("DETACH __sandbox")

    @contextmanager
    def deterministic(self):
        """Single-threaded execution keeps row order stable between step queries."""
        self.con.execute("SET threads = 1")
        try:
            yield
        finally:
            self.con.execute("RESET threads")

    # ---- catalog ----------------------------------------------------------
    def tables(self) -> list[dict]:
        rows = self.con.execute(
            """
            SELECT table_name, table_type FROM information_schema.tables
            WHERE table_catalog = current_database() AND table_schema = 'main'
            ORDER BY table_name
            """
        ).fetchall()
        out = []
        for name, kind in rows:
            cols = self.con.execute(
                """
                SELECT column_name, data_type FROM information_schema.columns
                WHERE table_catalog = current_database() AND table_schema = 'main' AND table_name = ?
                ORDER BY ordinal_position
                """,
                [name],
            ).fetchall()
            count = self.con.execute(f"SELECT COUNT(*) FROM {quote_ident(name)}").fetchone()[0]
            out.append(
                {
                    "name": name,
                    "kind": "view" if kind == "VIEW" else "table",
                    "rows": count,
                    "columns": [{"name": c, "type": t} for c, t in cols],
                }
            )
        return out

    def reset_samples(self, drop_all: bool = True) -> None:
        if drop_all:
            for t in self.tables():
                kind = "VIEW" if t["kind"] == "view" else "TABLE"
                self.con.execute(f"DROP {kind} IF EXISTS {quote_ident(t['name'])} CASCADE")
        self.con.execute(SAMPLE_SQL)

    def drop(self, name: str) -> None:
        kind = next((t["kind"] for t in self.tables() if t["name"] == name), None)
        if kind is None:
            raise ValueError(f"找不到資料表 {name}")
        self.con.execute(f"DROP {'VIEW' if kind == 'view' else 'TABLE'} {quote_ident(name)}")

    # ---- imports --------------------------------------------------------------
    def import_file(self, path: Path, table_name: str | None = None) -> list[str]:
        """Load a data file as table(s). Returns the created table names."""
        suffix = path.suffix.lower()
        head = path.read_bytes()[:16]
        if head.startswith(b"SQLite format 3"):
            return self._import_sqlite(path)
        if suffix in (".duckdb", ".ddb", ".db"):
            return self._import_duckdb(path)
        readers = {
            ".csv": "read_csv_auto", ".tsv": "read_csv_auto", ".txt": "read_csv_auto",
            ".parquet": "read_parquet", ".pq": "read_parquet",
            ".json": "read_json_auto", ".jsonl": "read_json_auto", ".ndjson": "read_json_auto",
            ".xlsx": "read_xlsx",
        }
        if suffix not in readers:
            raise ValueError(f"不支援的檔案類型 {suffix}（支援 CSV/TSV、Parquet、JSON、XLSX、DuckDB、SQLite）")
        name = sanitize_name(table_name or path.name)
        literal = "'" + str(path).replace("'", "''") + "'"
        if suffix == ".xlsx":
            try:
                self.con.execute("INSTALL excel; LOAD excel")
            except duckdb.Error as e:
                raise ValueError(f"讀取 Excel 需要 DuckDB 的 excel 擴充套件，第一次使用時要連網下載（{e}）") from e
        self.con.execute(f"CREATE OR REPLACE TABLE {quote_ident(name)} AS SELECT * FROM {readers[suffix]}({literal})")
        return [name]

    def _import_duckdb(self, path: Path) -> list[str]:
        literal = "'" + str(path).replace("'", "''") + "'"
        self.con.execute(f"ATTACH {literal} AS __import (READ_ONLY)")
        try:
            names = [
                r[0]
                for r in self.con.execute(
                    "SELECT table_name FROM duckdb_tables() WHERE database_name = '__import' AND schema_name = 'main'"
                ).fetchall()
            ]
            for n in names:
                self.con.execute(f"CREATE OR REPLACE TABLE {quote_ident(n)} AS SELECT * FROM __import.main.{quote_ident(n)}")
        finally:
            self.con.execute("DETACH __import")
        return names

    def _import_sqlite(self, path: Path) -> list[str]:
        # Python's sqlite3 avoids needing DuckDB's sqlite extension (a network download).
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            names = [r[0] for r in src.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            for n in names:
                info = src.execute(f"PRAGMA table_info({quote_ident(n)})").fetchall()
                cols = [(c[1], _sqlite_type(c[2])) for c in info]
                rows = src.execute(f"SELECT * FROM {quote_ident(n)}").fetchall()
                self._create_from_rows(n, cols, rows)
        finally:
            src.close()
        return names

    def _create_from_rows(self, name: str, cols: list[tuple[str, str]], rows: list[tuple]) -> None:
        qn = quote_ident(name)
        for types in ([t for _, t in cols], ["VARCHAR"] * len(cols)):  # SQLite typing is loose
            self.con.execute(f"CREATE OR REPLACE TABLE {qn} ({', '.join(f'{quote_ident(c)} {t}' for (c, _), t in zip(cols, types))})")
            try:
                if rows:
                    marks = ", ".join("?" * len(cols))
                    if types[0] == "VARCHAR" and len(set(types)) == 1:
                        rows = [tuple(None if v is None else str(v) for v in r) for r in rows]
                    self.con.executemany(f"INSERT INTO {qn} VALUES ({marks})", rows)
                return
            except duckdb.Error:
                continue


def _sqlite_type(decl: str) -> str:
    d = (decl or "").upper()
    if "INT" in d:
        return "BIGINT"
    if any(k in d for k in ("REAL", "FLOA", "DOUB")):
        return "DOUBLE"
    if any(k in d for k in ("NUMERIC", "DECIMAL")):
        return "DOUBLE"
    if "BLOB" in d:
        return "BLOB"
    if "BOOL" in d:
        return "BOOLEAN"
    if "DATETIME" in d or "TIMESTAMP" in d:
        return "TIMESTAMP"
    if d == "DATE":
        return "DATE"
    return "VARCHAR"
