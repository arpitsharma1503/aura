"""Shared grocery list stored in SQLite (``data/aura.db``).

* Every change bumps a list-wide ``revision`` so clients (dashboard, phones)
  can tell when to refresh; see ``app/events.py``.
* Every item has a ``version``. Edits to name/quantity/category/notes must send
  the version they were based on; if someone else changed the item meanwhile
  the edit is rejected with the current item (optimistic locking), so nobody
  silently overwrites the other person's change. Marking purchased is a plain
  "set to true/false", which is safe to repeat and needs no version.
* Writes are serialised by a lock and run in SQLite transactions (WAL mode),
  so the list survives restarts and power cuts.
* A legacy ``data/groceries.json`` is imported once, then renamed to
  ``groceries.json.migrated`` (never deleted).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from .storage import make_private

log = logging.getLogger(__name__)

CATEGORIES: dict[str, str] = {
    "produce": "Fruit & veg",
    "dairy": "Dairy & eggs",
    "bakery": "Bakery",
    "meat": "Meat & fish",
    "pantry": "Pantry",
    "frozen": "Frozen",
    "drinks": "Drinks",
    "household": "Household",
    "personal": "Personal care",
    "other": "Other",
}
CategoryName = Literal[
    "produce", "dairy", "bakery", "meat", "pantry", "frozen", "drinks", "household", "personal", "other"
]

COMMON_ITEMS: list[tuple[str, str]] = [
    ("Milk", "dairy"), ("Eggs", "dairy"), ("Bread", "bakery"), ("Curd", "dairy"),
    ("Bananas", "produce"), ("Tomatoes", "produce"), ("Onions", "produce"), ("Potatoes", "produce"),
    ("Rice", "pantry"), ("Atta", "pantry"), ("Dal", "pantry"), ("Cooking oil", "pantry"),
    ("Butter", "dairy"), ("Paneer", "dairy"), ("Coffee", "drinks"), ("Tea", "drinks"),
    ("Sugar", "pantry"), ("Apples", "produce"), ("Coriander", "produce"), ("Chicken", "meat"),
    ("Dish soap", "household"), ("Detergent", "household"), ("Toilet paper", "household"),
    ("Toothpaste", "personal"),
]

_COMMON_BY_KEY = {n.casefold(): c for n, c in COMMON_ITEMS}

NAME_MAX = 60
QTY_MAX = 20
NOTES_MAX = 200
MAX_ITEMS = 500
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SPACES = re.compile(r"\s+")


def _clean(value: str | None, *, max_len: int, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ValueError("is required")
        return None
    value = value.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    if _CONTROL.search(value):
        raise ValueError("contains invalid characters")
    value = _SPACES.sub(" ", value).strip()
    if not value:
        if required:
            raise ValueError("can't be empty")
        return None
    if len(value) > max_len:
        raise ValueError(f"must be at most {max_len} characters")
    return value


def _clean_name(v) -> str:
    if not isinstance(v, str):
        raise ValueError("must be text")
    v = _clean(v, max_len=NAME_MAX, required=True)
    return v[0].upper() + v[1:] if v.islower() else v  # "milk" -> "Milk", keep "iPhone cable"


def name_key(name: str) -> str:
    return _SPACES.sub(" ", name).strip().casefold()


class ItemIn(BaseModel):
    name: str
    quantity: str | None = None
    category: CategoryName | None = None  # None = guess from history / common items
    notes: str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v):
        return _clean_name(v)

    @field_validator("quantity", mode="before")
    @classmethod
    def _qty(cls, v):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            v = str(v)
        if v is not None and not isinstance(v, str):
            raise ValueError("must be text")
        return _clean(v, max_len=QTY_MAX)

    @field_validator("notes", mode="before")
    @classmethod
    def _notes(cls, v):
        if v is not None and not isinstance(v, str):
            raise ValueError("must be text")
        return _clean(v, max_len=NOTES_MAX)


class ItemPatch(ItemIn):
    """Partial update. Only fields actually sent are changed."""

    name: str | None = None  # type: ignore[assignment]
    category: CategoryName | None = None  # type: ignore[assignment]
    purchased: bool | None = None
    version: int | None = Field(default=None, ge=1)

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v):
        return _clean_name(v)

    @property
    def edits_details(self) -> bool:
        return bool(self.model_fields_set & {"name", "quantity", "category", "notes"})


class Item(BaseModel):
    id: int
    name: str
    quantity: str | None
    category: CategoryName
    category_label: str
    notes: str | None
    purchased: bool
    created_at: datetime
    updated_at: datetime
    purchased_at: datetime | None
    version: int


class GroceryList(BaseModel):
    status: Literal["ok", "empty", "error"]
    message: str | None = None
    items: list[Item] = []
    remaining: int = 0
    revision: int = 0


class Suggestion(BaseModel):
    name: str
    category: CategoryName


class GroceryStoreError(Exception):
    pass


class ItemNotFound(GroceryStoreError):
    pass


class DuplicateItem(GroceryStoreError):
    def __init__(self, item: Item) -> None:
        super().__init__(f"{item.name} is already on the list")
        self.item = item


class VersionConflict(GroceryStoreError):
    def __init__(self, item: Item) -> None:
        super().__init__(f"{item.name} was changed on another device")
        self.item = item


class ListFull(GroceryStoreError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 60),
    name_key     TEXT NOT NULL,
    quantity     TEXT CHECK (quantity IS NULL OR length(quantity) <= 20),
    category     TEXT NOT NULL DEFAULT 'other',
    notes        TEXT CHECK (notes IS NULL OR length(notes) <= 200),
    purchased    INTEGER NOT NULL DEFAULT 0 CHECK (purchased IN (0, 1)),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    purchased_at TEXT,
    version      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS items_open ON items (purchased, name_key);
CREATE TABLE IF NOT EXISTS history (
    name_key   TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    category   TEXT NOT NULL,
    times      INTEGER NOT NULL DEFAULT 0,
    last_added TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO meta (key, value) VALUES ('revision', '0');
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class GroceryStore:
    def __init__(self, db_path: Path, legacy_json: Path | None = None) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(SCHEMA)
        make_private(db_path)
        if legacy_json is not None:
            self._import_legacy(legacy_json)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ----- helpers -----

    def _tx(self):
        db = self._db

        class _Tx:
            def __enter__(self_inner):
                db.execute("BEGIN IMMEDIATE")
                return db

            def __exit__(self_inner, exc_type, exc, tb):
                db.execute("ROLLBACK" if exc_type else "COMMIT")
                return False

        return _Tx()

    @staticmethod
    def _row(row: sqlite3.Row) -> Item:
        cat = row["category"] if row["category"] in CATEGORIES else "other"
        return Item(
            id=row["id"], name=row["name"], quantity=row["quantity"], category=cat,
            category_label=CATEGORIES[cat], notes=row["notes"], purchased=bool(row["purchased"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
            purchased_at=row["purchased_at"], version=row["version"],
        )

    def _get(self, db, item_id: int) -> Item:
        row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise ItemNotFound("Item not found")
        return self._row(row)

    @staticmethod
    def _bump(db) -> None:
        db.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'revision'")

    @staticmethod
    def _remember(db, name: str, category: str) -> None:
        db.execute(
            """INSERT INTO history (name_key, name, category, times, last_added) VALUES (?, ?, ?, 1, ?)
               ON CONFLICT(name_key) DO UPDATE SET name = excluded.name, category = excluded.category,
               times = times + 1, last_added = excluded.last_added""",
            (name_key(name), name, category, _now()),
        )

    # ----- reads -----

    @property
    def revision(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()[0])

    def load(self) -> GroceryList:
        order = " ".join(f"WHEN '{k}' THEN {i}" for i, k in enumerate(CATEGORIES))
        try:
            with self._lock:
                rows = self._db.execute(
                    f"SELECT * FROM items ORDER BY purchased, CASE category {order} ELSE 99 END, "
                    "created_at, id"
                ).fetchall()
                rev = int(self._db.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()[0])
        except sqlite3.Error as exc:
            log.exception("Could not read grocery list")
            return GroceryList(status="error", message=f"Could not read the grocery list ({exc.__class__.__name__})")
        items = [self._row(r) for r in rows]
        remaining = sum(not i.purchased for i in items)
        if not items:
            return GroceryList(status="empty", message="Your grocery list is empty", revision=rev)
        return GroceryList(status="ok", items=items, remaining=remaining, revision=rev)

    def suggestions(self, limit: int = 12) -> list[Suggestion]:
        with self._lock:
            on_list = {r[0] for r in self._db.execute("SELECT name_key FROM items WHERE purchased = 0")}
            frequent = self._db.execute(
                "SELECT name, category FROM history ORDER BY times DESC, last_added DESC LIMIT 50"
            ).fetchall()
        out: list[Suggestion] = []
        seen: set[str] = set()
        for name, cat in [(r["name"], r["category"]) for r in frequent] + COMMON_ITEMS:
            key = name_key(name)
            if key in on_list or key in seen:
                continue
            seen.add(key)
            out.append(Suggestion(name=name, category=cat if cat in CATEGORIES else "other"))
            if len(out) >= limit:
                break
        return out

    # ----- writes -----

    def _guess_category(self, db, key: str) -> str:
        row = db.execute("SELECT category FROM history WHERE name_key = ?", (key,)).fetchone()
        if row and row[0] in CATEGORIES:
            return row[0]
        return _COMMON_BY_KEY.get(key, "other")

    def add(self, data: ItemIn) -> Item:
        key = name_key(data.name)
        now = _now()
        with self._lock, self._tx() as db:
            category = data.category or self._guess_category(db, key)
            dup = db.execute(
                "SELECT * FROM items WHERE name_key = ? AND purchased = 0 LIMIT 1", (key,)
            ).fetchone()
            if dup is not None:
                raise DuplicateItem(self._row(dup))
            if db.execute("SELECT COUNT(*) FROM items").fetchone()[0] >= MAX_ITEMS:
                raise ListFull(f"The list is full ({MAX_ITEMS} items). Clear purchased items first.")
            cur = db.execute(
                """INSERT INTO items (name, name_key, quantity, category, notes, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (data.name, key, data.quantity, category, data.notes, now, now),
            )
            self._remember(db, data.name, category)
            self._bump(db)
            return self._get(db, cur.lastrowid)

    def update(self, item_id: int, patch: ItemPatch) -> Item:
        with self._lock, self._tx() as db:
            current = self._get(db, item_id)
            if patch.edits_details:
                if patch.version is None:
                    raise VersionConflict(current)
                if patch.version != current.version:
                    raise VersionConflict(current)

            fields: dict[str, object] = {}
            sent = patch.model_fields_set
            if "name" in sent and patch.name != current.name:
                key = name_key(patch.name)
                if not current.purchased:
                    dup = db.execute(
                        "SELECT * FROM items WHERE name_key = ? AND purchased = 0 AND id != ?",
                        (key, item_id),
                    ).fetchone()
                    if dup is not None:
                        raise DuplicateItem(self._row(dup))
                fields["name"], fields["name_key"] = patch.name, key
            for f in ("quantity", "notes"):
                if f in sent and getattr(patch, f) != getattr(current, f):
                    fields[f] = getattr(patch, f)
            if "category" in sent and patch.category and patch.category != current.category:
                fields["category"] = patch.category
            if "purchased" in sent and patch.purchased is not None and patch.purchased != current.purchased:
                if not patch.purchased:
                    dup = db.execute(
                        "SELECT * FROM items WHERE name_key = ? AND purchased = 0 AND id != ?",
                        (name_key(current.name), item_id),
                    ).fetchone()
                    if dup is not None:
                        raise DuplicateItem(self._row(dup))
                fields["purchased"] = int(patch.purchased)
                fields["purchased_at"] = _now() if patch.purchased else None

            if not fields:
                return current  # nothing changed (e.g. both phones tapped "purchased")
            fields["updated_at"] = _now()
            assignments = ", ".join(f"{k} = ?" for k in fields)
            db.execute(
                f"UPDATE items SET {assignments}, version = version + 1 WHERE id = ?",
                (*fields.values(), item_id),
            )
            self._bump(db)
            return self._get(db, item_id)

    def delete(self, item_id: int, version: int | None = None) -> None:
        with self._lock, self._tx() as db:
            current = self._get(db, item_id)
            if version is not None and version != current.version:
                raise VersionConflict(current)
            db.execute("DELETE FROM items WHERE id = ?", (item_id,))
            self._bump(db)

    def clear_purchased(self) -> int:
        with self._lock, self._tx() as db:
            n = db.execute("DELETE FROM items WHERE purchased = 1").rowcount
            if n:
                self._bump(db)
            return n

    # ----- migration -----

    def _import_legacy(self, path: Path) -> None:
        with self._lock:
            done = self._db.execute("SELECT value FROM meta WHERE key = 'legacy_imported'").fetchone()
        if done or not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            entries = raw.get("items", []) if isinstance(raw, dict) else []
        except (OSError, ValueError) as exc:
            log.error("Legacy %s is unreadable (%s); left in place, not imported", path.name, exc)
            return
        imported = 0
        now = _now()
        with self._lock, self._tx() as db:
            for e in entries:
                try:
                    data = ItemIn(name=e.get("name"), quantity=e.get("quantity"),
                                  category=e.get("category") or "other", notes=e.get("notes"))
                except (ValidationError, AttributeError):
                    log.warning("Skipping invalid legacy grocery entry: %r", e)
                    continue
                checked = bool(e.get("checked") or e.get("purchased"))
                db.execute(
                    """INSERT INTO items (name, name_key, quantity, category, notes, purchased,
                       created_at, updated_at, purchased_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (data.name, name_key(data.name), data.quantity, data.category or "other", data.notes,
                     int(checked), now, now, now if checked else None),
                )
                self._remember(db, data.name, data.category or "other")
                imported += 1
            db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('legacy_imported', ?)", (now,))
            self._bump(db)
        target = path.with_name(path.name + ".migrated")
        try:
            path.rename(target)
        except OSError:
            log.warning("Imported %s but could not rename it", path.name)
        log.info("Imported %d grocery item(s) from %s (kept as %s)", imported, path.name, target.name)
