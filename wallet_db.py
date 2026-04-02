import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "travel_wallet.sqlite3"


@dataclass
class Trip:
    id: int
    user_id: int
    title: str
    home_ccy: str
    dest_ccy: str
    rate_home_per_dest: float
    balance_home: float
    balance_dest: float


@dataclass
class ExpenseRow:
    id: int
    amount_dest: float
    amount_home: float
    created_at: str
    rate_home_per_dest: float | None
    rate_date: str | None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_conn():
    c = _connect()
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                home_ccy TEXT NOT NULL,
                dest_ccy TEXT NOT NULL,
                rate_home_per_dest REAL NOT NULL,
                balance_home REAL NOT NULL,
                balance_dest REAL NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trip_id INTEGER NOT NULL,
                amount_dest REAL NOT NULL,
                amount_home REAL NOT NULL,
                created_at TEXT NOT NULL,
                rate_home_per_dest REAL,
                rate_date TEXT,
                FOREIGN KEY (trip_id) REFERENCES trips(id)
            );
            CREATE TABLE IF NOT EXISTS user_state (
                user_id INTEGER PRIMARY KEY,
                active_trip_id INTEGER,
                FOREIGN KEY (active_trip_id) REFERENCES trips(id)
            );
            CREATE INDEX IF NOT EXISTS idx_trips_user ON trips(user_id);
            CREATE INDEX IF NOT EXISTS idx_expenses_trip ON expenses(trip_id);
            """
        )
        expense_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(expenses)").fetchall()
        }
        if "rate_home_per_dest" not in expense_columns:
            conn.execute("ALTER TABLE expenses ADD COLUMN rate_home_per_dest REAL")
        if "rate_date" not in expense_columns:
            conn.execute("ALTER TABLE expenses ADD COLUMN rate_date TEXT")


def get_active_trip_id(user_id: int) -> int | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT active_trip_id FROM user_state WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None or row["active_trip_id"] is None:
            return None
        return int(row["active_trip_id"])


def set_active_trip(user_id: int, trip_id: int | None) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO user_state (user_id, active_trip_id)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET active_trip_id = excluded.active_trip_id
            """,
            (user_id, trip_id),
        )


def list_trips(user_id: int) -> list[Trip]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, user_id, title, home_ccy, dest_ccy, rate_home_per_dest,
                   balance_home, balance_dest
            FROM trips WHERE user_id = ? ORDER BY id
            """,
            (user_id,),
        ).fetchall()
    return [_row_to_trip(r) for r in rows]


def get_trip(trip_id: int, user_id: int) -> Trip | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT id, user_id, title, home_ccy, dest_ccy, rate_home_per_dest,
                   balance_home, balance_dest
            FROM trips WHERE id = ? AND user_id = ?
            """,
            (trip_id, user_id),
        ).fetchone()
    return _row_to_trip(row) if row else None


def get_active_trip(user_id: int) -> Trip | None:
    tid = get_active_trip_id(user_id)
    if tid is None:
        return None
    return get_trip(tid, user_id)


def _row_to_trip(row: sqlite3.Row) -> Trip:
    return Trip(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        title=str(row["title"]),
        home_ccy=str(row["home_ccy"]),
        dest_ccy=str(row["dest_ccy"]),
        rate_home_per_dest=float(row["rate_home_per_dest"]),
        balance_home=float(row["balance_home"]),
        balance_dest=float(row["balance_dest"]),
    )


def create_trip(
    user_id: int,
    title: str,
    home_ccy: str,
    dest_ccy: str,
    rate_home_per_dest: float,
    balance_home: float,
    balance_dest: float,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO trips (
                user_id, title, home_ccy, dest_ccy, rate_home_per_dest,
                balance_home, balance_dest, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                title,
                home_ccy.upper(),
                dest_ccy.upper(),
                rate_home_per_dest,
                balance_home,
                balance_dest,
                now,
            ),
        )
        trip_id = int(cur.lastrowid)
    set_active_trip(user_id, trip_id)
    return trip_id


def update_trip_balances(trip_id: int, user_id: int, home: float, dest: float) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE trips SET balance_home = ?, balance_dest = ?
            WHERE id = ? AND user_id = ?
            """,
            (home, dest, trip_id, user_id),
        )
        return cur.rowcount > 0


def update_trip_rate(
    trip_id: int, user_id: int, rate_home_per_dest: float
) -> Trip | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT balance_home FROM trips WHERE id = ? AND user_id = ?
            """,
            (trip_id, user_id),
        ).fetchone()
        if not row:
            return None
        bh = float(row["balance_home"])
        bd = bh / rate_home_per_dest if rate_home_per_dest else 0.0
        conn.execute(
            """
            UPDATE trips SET rate_home_per_dest = ?, balance_dest = ?
            WHERE id = ? AND user_id = ?
            """,
            (rate_home_per_dest, bd, trip_id, user_id),
        )
    return get_trip(trip_id, user_id)


def add_expense(
    trip_id: int,
    user_id: int,
    amount_dest: float,
    amount_home: float,
    rate_home_per_dest: float | None = None,
    rate_date: str | None = None,
) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT balance_home, balance_dest FROM trips
            WHERE id = ? AND user_id = ?
            """,
            (trip_id, user_id),
        ).fetchone()
        if not row:
            return False
        bh = float(row["balance_home"]) - amount_home
        bd = float(row["balance_dest"]) - amount_dest
        conn.execute(
            """
            UPDATE trips SET balance_home = ?, balance_dest = ?
            WHERE id = ? AND user_id = ?
            """,
            (bh, bd, trip_id, user_id),
        )
        conn.execute(
            """
            INSERT INTO expenses (
                trip_id, amount_dest, amount_home, created_at,
                rate_home_per_dest, rate_date
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (trip_id, amount_dest, amount_home, now, rate_home_per_dest, rate_date),
        )
    return True


def list_expenses(trip_id: int, user_id: int, limit: int = 50) -> list[ExpenseRow]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM trips WHERE id = ? AND user_id = ?",
            (trip_id, user_id),
        ).fetchone()
        if not row:
            return []
        rows = conn.execute(
            """
            SELECT id, amount_dest, amount_home, created_at,
                   rate_home_per_dest, rate_date
            FROM expenses
            WHERE trip_id = ? ORDER BY id DESC LIMIT ?
            """,
            (trip_id, limit),
        ).fetchall()
    return [
        ExpenseRow(
            id=int(r["id"]),
            amount_dest=float(r["amount_dest"]),
            amount_home=float(r["amount_home"]),
            created_at=str(r["created_at"]),
            rate_home_per_dest=(
                float(r["rate_home_per_dest"])
                if r["rate_home_per_dest"] is not None
                else None
            ),
            rate_date=str(r["rate_date"]) if r["rate_date"] is not None else None,
        )
        for r in rows
    ]


def delete_trip(trip_id: int, user_id: int) -> tuple[bool, int | None]:
    current_active_id = get_active_trip_id(user_id)
    next_active_id = current_active_id

    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM trips WHERE id = ? AND user_id = ?",
            (trip_id, user_id),
        ).fetchone()
        if not row:
            return False, current_active_id

        conn.execute("DELETE FROM expenses WHERE trip_id = ?", (trip_id,))
        conn.execute(
            "DELETE FROM trips WHERE id = ? AND user_id = ?",
            (trip_id, user_id),
        )

        if current_active_id == trip_id:
            next_row = conn.execute(
                "SELECT id FROM trips WHERE user_id = ? ORDER BY id LIMIT 1",
                (user_id,),
            ).fetchone()
            next_active_id = int(next_row["id"]) if next_row else None

    if current_active_id == trip_id:
        set_active_trip(user_id, next_active_id)

    return True, next_active_id
