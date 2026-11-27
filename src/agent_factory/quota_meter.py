"""Quota meter state management: rolling window tracking, 429 cooldown parsing, and recovery resets."""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_factory.ports.clock import Clock
from agent_factory.telemetry import _resolve_db_path

DEFAULT_ESTIMATED_WINDOW_CAPACITY: int = 180000
DEFAULT_QUOTA_WINDOW_SECONDS: int = 5 * 3600
DEFAULT_RESERVE_THRESHOLD_PCT: int = 30
DEFAULT_AVG_TURN_TOKENS: int = 12000
DEFAULT_QUOTA_RESET_DELAY_SECONDS: int = 1800
MIN_QUOTA_RESET_DELAY_SECONDS: int = 1

_RESET_IN_EXTENDED_RE = re.compile(
    r"Resets?\s+in\s+"
    r"(?:(?P<hours>\d+)\s*(?:hours?|hrs?|h(?![a-z]))[\s,]*)?"
    r"(?:(?P<minutes>\d+)\s*(?:minutes?|mins?|m(?![a-z]))[\s,]*)?"
    r"(?:(?P<seconds>\d+)\s*(?:seconds?|secs?|s(?![a-z])))?",
    re.IGNORECASE,
)


class _WallClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def _normalize_utc(value: datetime | None, clock: Clock | None = None) -> datetime:
    dt = value if value is not None else (clock.now() if clock is not None else datetime.now(UTC))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_iso_datetime(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None


def _should_skip_default_db_write(db_path: Path | str | None) -> bool:
    return (
        db_path is None
        and "PYTEST_CURRENT_TEST" in os.environ
        and not os.environ.get("AGENT_FACTORY_TELEMETRY_DB")
    )


def parse_reset_delay_seconds(
    raw_output: str,
    *,
    default_seconds: int = DEFAULT_QUOTA_RESET_DELAY_SECONDS,
) -> int:
    """Extract wait duration in seconds from 429 'Resets in...' messages, or return default_seconds."""
    text = str(raw_output or "")
    match = _RESET_IN_EXTENDED_RE.search(text)
    if not match:
        return max(60, int(default_seconds))
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    total = (hours * 3600) + (minutes * 60) + seconds
    if total <= 0:
        return max(60, int(default_seconds))
    return max(MIN_QUOTA_RESET_DELAY_SECONDS, total)


def _ensure_quota_schema(conn: sqlite3.Connection, *, now_iso: str) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS quota_state (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            window_started_at TEXT NOT NULL,
            window_tokens_used INTEGER NOT NULL DEFAULT 0 CHECK(window_tokens_used >= 0),
            estimated_window_capacity INTEGER NOT NULL DEFAULT 180000
                CHECK(estimated_window_capacity >= 1000),
            quota_paused_until TEXT,
            reserve_threshold_pct INTEGER NOT NULL DEFAULT 30
                CHECK(reserve_threshold_pct >= 0 AND reserve_threshold_pct <= 95),
            turns_used INTEGER NOT NULL DEFAULT 0 CHECK(turns_used >= 0),
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO quota_state (
            id,
            window_started_at,
            window_tokens_used,
            estimated_window_capacity,
            quota_paused_until,
            reserve_threshold_pct,
            turns_used,
            updated_at
        ) VALUES (1, ?, 0, ?, NULL, ?, 0, ?);
        """,
        (now_iso, DEFAULT_ESTIMATED_WINDOW_CAPACITY, DEFAULT_RESERVE_THRESHOLD_PCT, now_iso),
    )


def _row_to_quota_snapshot(
    row: sqlite3.Row,
    *,
    now: datetime,
    window_seconds: int = DEFAULT_QUOTA_WINDOW_SECONDS,
) -> dict[str, Any]:
    ref_now = _normalize_utc(now)
    capacity = max(1000, int(row["estimated_window_capacity"]))
    used = max(0, int(row["window_tokens_used"]))
    turns_used = max(0, int(row["turns_used"]))
    reserve_pct = max(0, min(95, int(row["reserve_threshold_pct"])))
    paused_until_raw = row["quota_paused_until"]
    paused_dt = _parse_iso_datetime(paused_until_raw)
    window_dt = _parse_iso_datetime(row["window_started_at"])

    cooldown_active = bool(paused_dt is not None and ref_now < paused_dt)
    cooldown_expired = bool(paused_dt is not None and ref_now >= paused_dt)
    rolling_window_expired = bool(
        window_dt is not None and (ref_now - window_dt).total_seconds() >= max(1, int(window_seconds))
    )
    window_expired = bool(cooldown_expired or (paused_dt is None and rolling_window_expired))

    raw_remaining_capacity = max(0, capacity - used)
    raw_remaining_pct = max(0, min(100, round((raw_remaining_capacity / capacity) * 100)))
    was_paused = bool(paused_dt is not None)
    was_exhausted = bool(raw_remaining_pct <= 0)
    was_reserve_reached = bool(raw_remaining_pct <= reserve_pct)

    if cooldown_active and paused_dt is not None:
        remaining_pct = 0
        estimated_turns_left = 0
        resume_hhmm: str | None = paused_dt.astimezone().strftime("%H:%M")
    else:
        remaining_pct = raw_remaining_pct
        avg_turn = max(4000, used // turns_used) if turns_used > 0 else DEFAULT_AVG_TURN_TOKENS
        estimated_turns_left = max(0, raw_remaining_capacity // avg_turn)
        resume_hhmm = None

    is_exhausted = bool((not cooldown_active) and (not window_expired) and remaining_pct <= 0)
    reserve_reached = bool((not cooldown_active) and (not window_expired) and (remaining_pct <= reserve_pct))
    is_paused = bool(cooldown_active or is_exhausted)
    is_blocked = bool(cooldown_active or is_exhausted or reserve_reached)
    requires_recovery = bool(
        is_blocked or (window_expired and (was_paused or was_exhausted or was_reserve_reached))
    )

    return {
        "window_started_at": str(row["window_started_at"]),
        "window_tokens_used": used,
        "estimated_window_capacity": capacity,
        "quota_paused_until": paused_dt.isoformat() if cooldown_active and paused_dt is not None else None,
        "raw_quota_paused_until": paused_dt.isoformat() if paused_dt is not None else None,
        "reserve_threshold_pct": reserve_pct,
        "turns_used": turns_used,
        "quota_remaining_pct": remaining_pct,
        "estimated_turns_left": estimated_turns_left,
        "is_paused": is_paused,
        "cooldown_active": cooldown_active,
        "cooldown_expired": cooldown_expired,
        "rolling_window_expired": rolling_window_expired,
        "window_expired": window_expired,
        "was_paused": was_paused,
        "was_exhausted": was_exhausted,
        "was_reserve_reached": was_reserve_reached,
        "is_exhausted": is_exhausted,
        "reserve_reached": reserve_reached,
        "is_blocked": is_blocked,
        "requires_recovery": requires_recovery,
        "resume_hhmm": resume_hhmm,
        "updated_at": str(row["updated_at"]),
    }


class QuotaMeter:
    """Thread-safe SQLite-backed quota meter compatible with quota_state in telemetry.db."""

    def __init__(
        self,
        *,
        db_path: Path | str | None = None,
        clock: Clock | None = None,
        window_seconds: int = DEFAULT_QUOTA_WINDOW_SECONDS,
    ) -> None:
        self.db_path = db_path
        self.clock: Clock = clock if clock is not None else _WallClock()
        self.window_seconds = max(1, int(window_seconds))
        self._lock = threading.RLock()
        self._memory_conn: sqlite3.Connection | None = None

    def _is_memory_db(self) -> bool:
        return self.db_path is not None and str(self.db_path) == ":memory:"

    def _open_conn(self, *, now: datetime) -> tuple[sqlite3.Connection, bool]:
        now_iso = _normalize_utc(now).isoformat()
        if self._is_memory_db():
            if self._memory_conn is None:
                conn = sqlite3.connect(":memory:", check_same_thread=False)
                conn.row_factory = sqlite3.Row
                _ensure_quota_schema(conn, now_iso=now_iso)
                conn.commit()
                self._memory_conn = conn
            return self._memory_conn, False

        resolved = _resolve_db_path(self.db_path)
        if str(resolved) != ":memory:":
            resolved.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(resolved), timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            _ensure_quota_schema(conn, now_iso=now_iso)
            conn.commit()
        except Exception:
            conn.close()
            raise
        return conn, True

    def close(self) -> None:
        """Close any owned in-memory SQLite connection cleanly."""
        with self._lock:
            if self._memory_conn is not None:
                self._memory_conn.close()
                self._memory_conn = None

    def peek_status(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Read persisted quota_state without mutating or auto-resetting expired windows."""
        ref_now = _normalize_utc(now, self.clock)
        if _should_skip_default_db_write(self.db_path):
            now_iso = ref_now.isoformat()
            return {
                "window_started_at": now_iso,
                "window_tokens_used": 0,
                "estimated_window_capacity": DEFAULT_ESTIMATED_WINDOW_CAPACITY,
                "quota_paused_until": None,
                "raw_quota_paused_until": None,
                "reserve_threshold_pct": DEFAULT_RESERVE_THRESHOLD_PCT,
                "turns_used": 0,
                "quota_remaining_pct": 100,
                "estimated_turns_left": DEFAULT_ESTIMATED_WINDOW_CAPACITY // DEFAULT_AVG_TURN_TOKENS,
                "is_paused": False,
                "cooldown_active": False,
                "cooldown_expired": False,
                "rolling_window_expired": False,
                "window_expired": False,
                "was_paused": False,
                "was_exhausted": False,
                "was_reserve_reached": False,
                "is_exhausted": False,
                "reserve_reached": False,
                "is_blocked": False,
                "requires_recovery": False,
                "resume_hhmm": None,
                "updated_at": now_iso,
            }
        with self._lock:
            conn, should_close = self._open_conn(now=ref_now)
            try:
                row = conn.execute(
                    "SELECT window_started_at, window_tokens_used, estimated_window_capacity, "
                    "quota_paused_until, reserve_threshold_pct, turns_used, updated_at "
                    "FROM quota_state WHERE id = 1;"
                ).fetchone()
                assert row is not None
                return _row_to_quota_snapshot(row, now=ref_now, window_seconds=self.window_seconds)
            finally:
                if should_close:
                    conn.close()

    def get_status(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Return quota status, resetting cleanly when an expired cooldown or rolling window is detected."""
        ref_now = _normalize_utc(now, self.clock)
        snap = self.peek_status(now=ref_now)
        if snap["window_expired"]:
            return self.reset_pause(now=ref_now)
        return snap

    def is_cooldown_active(self, *, now: datetime | None = None) -> bool:
        """Return True when an explicit 429 cooldown timer is currently active."""
        return bool(self.peek_status(now=now)["cooldown_active"])

    def is_paused(self, *, now: datetime | None = None) -> bool:
        """Return True when quota is paused due to active 429 cooldown or ~0% cycle exhaustion."""
        return bool(self.peek_status(now=now)["is_paused"])

    def is_blocked(self, *, now: datetime | None = None, include_reserve: bool = True) -> bool:
        """Return True when autonomous execution is blocked by cooldown, exhaustion, or reserve threshold."""
        snap = self.peek_status(now=now)
        if include_reserve:
            return bool(snap["is_blocked"])
        return bool(snap["is_paused"])

    def is_window_expired(self, *, now: datetime | None = None) -> bool:
        """Return True when the active cooldown or rolling quota window has expired."""
        return bool(self.peek_status(now=now)["window_expired"])

    def reset_pause(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Immediately clear quota pause/cooldown and restore 100% available balance for the new cycle."""
        ref_now = _normalize_utc(now, self.clock)
        if _should_skip_default_db_write(self.db_path):
            snap = self.peek_status(now=ref_now)
            snap.update(
                {
                    "window_started_at": ref_now.isoformat(),
                    "window_tokens_used": 0,
                    "quota_paused_until": None,
                    "raw_quota_paused_until": None,
                    "turns_used": 0,
                    "quota_remaining_pct": 100,
                    "estimated_turns_left": max(
                        0, int(snap["estimated_window_capacity"]) // DEFAULT_AVG_TURN_TOKENS
                    ),
                    "is_paused": False,
                    "cooldown_active": False,
                    "cooldown_expired": False,
                    "rolling_window_expired": False,
                    "window_expired": False,
                    "was_paused": False,
                    "was_exhausted": False,
                    "was_reserve_reached": False,
                    "is_exhausted": False,
                    "reserve_reached": False,
                    "is_blocked": False,
                    "requires_recovery": False,
                    "resume_hhmm": None,
                    "updated_at": ref_now.isoformat(),
                }
            )
            return snap

        now_iso = ref_now.isoformat()
        with self._lock:
            conn, should_close = self._open_conn(now=ref_now)
            try:
                conn.execute(
                    """
                    UPDATE quota_state
                    SET window_started_at = ?,
                        window_tokens_used = 0,
                        quota_paused_until = NULL,
                        turns_used = 0,
                        updated_at = ?
                    WHERE id = 1;
                    """,
                    (now_iso, now_iso),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT window_started_at, window_tokens_used, estimated_window_capacity, "
                    "quota_paused_until, reserve_threshold_pct, turns_used, updated_at "
                    "FROM quota_state WHERE id = 1;"
                ).fetchone()
                assert row is not None
                return _row_to_quota_snapshot(row, now=ref_now, window_seconds=self.window_seconds)
            finally:
                if should_close:
                    conn.close()

    def record_429_pause(
        self,
        raw_output: str = "",
        *,
        reset_seconds: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Parse 'Resets in...' from a 429 error and update quota_paused_until atomically."""
        ref_now = _normalize_utc(now, self.clock)
        delay = (
            max(MIN_QUOTA_RESET_DELAY_SECONDS, int(reset_seconds))
            if reset_seconds is not None
            else parse_reset_delay_seconds(raw_output, default_seconds=DEFAULT_QUOTA_RESET_DELAY_SECONDS)
        )
        paused_until = ref_now + timedelta(seconds=delay)
        if _should_skip_default_db_write(self.db_path):
            snap = self.peek_status(now=ref_now)
            snap.update(
                {
                    "quota_paused_until": paused_until.isoformat(),
                    "raw_quota_paused_until": paused_until.isoformat(),
                    "quota_remaining_pct": 0,
                    "estimated_turns_left": 0,
                    "is_paused": True,
                    "cooldown_active": True,
                    "cooldown_expired": False,
                    "window_expired": False,
                    "is_blocked": True,
                    "requires_recovery": True,
                    "reset_seconds": delay,
                    "resume_hhmm": paused_until.astimezone().strftime("%H:%M"),
                    "updated_at": ref_now.isoformat(),
                }
            )
            return snap

        with self._lock:
            conn, should_close = self._open_conn(now=ref_now)
            try:
                row = conn.execute(
                    "SELECT window_tokens_used, estimated_window_capacity FROM quota_state WHERE id = 1;"
                ).fetchone()
                used = int(row["window_tokens_used"]) if row is not None else 0
                current_cap = (
                    int(row["estimated_window_capacity"])
                    if row is not None
                    else DEFAULT_ESTIMATED_WINDOW_CAPACITY
                )
                calibrated_cap = used if used >= 5000 else current_cap
                conn.execute(
                    """
                    UPDATE quota_state
                    SET estimated_window_capacity = ?,
                        window_tokens_used = ?,
                        quota_paused_until = ?,
                        updated_at = ?
                    WHERE id = 1;
                    """,
                    (calibrated_cap, calibrated_cap, paused_until.isoformat(), ref_now.isoformat()),
                )
                conn.commit()
                updated_row = conn.execute(
                    "SELECT window_started_at, window_tokens_used, estimated_window_capacity, "
                    "quota_paused_until, reserve_threshold_pct, turns_used, updated_at "
                    "FROM quota_state WHERE id = 1;"
                ).fetchone()
                assert updated_row is not None
                payload = _row_to_quota_snapshot(
                    updated_row,
                    now=ref_now,
                    window_seconds=self.window_seconds,
                )
                payload["reset_seconds"] = delay
                return payload
            finally:
                if should_close:
                    conn.close()

    def record_usage(
        self,
        input_chars: int = 0,
        output_chars: int = 0,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Accumulate estimated token consumption for the current rolling window."""
        ref_now = _normalize_utc(now, self.clock)
        if _should_skip_default_db_write(self.db_path):
            return self.peek_status(now=ref_now)

        delta = max(0, int(input_chars)) + max(0, int(output_chars))
        if delta <= 0:
            delta = 1

        if self.is_window_expired(now=ref_now):
            self.reset_pause(now=ref_now)

        with self._lock:
            conn, should_close = self._open_conn(now=ref_now)
            try:
                conn.execute(
                    """
                    UPDATE quota_state
                    SET window_tokens_used = window_tokens_used + ?,
                        turns_used = turns_used + 1,
                        updated_at = ?
                    WHERE id = 1;
                    """,
                    (delta, ref_now.isoformat()),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT window_started_at, window_tokens_used, estimated_window_capacity, "
                    "quota_paused_until, reserve_threshold_pct, turns_used, updated_at "
                    "FROM quota_state WHERE id = 1;"
                ).fetchone()
                assert row is not None
                return _row_to_quota_snapshot(row, now=ref_now, window_seconds=self.window_seconds)
            finally:
                if should_close:
                    conn.close()


def inspect_quota_state(
    *,
    db_path: Path | str | None = None,
    now: datetime | None = None,
    window_seconds: int = DEFAULT_QUOTA_WINDOW_SECONDS,
) -> dict[str, Any]:
    """Inspect current quota_state without mutating expired windows."""
    return QuotaMeter(db_path=db_path, window_seconds=window_seconds).peek_status(now=now)


def is_quota_paused(
    *,
    db_path: Path | str | None = None,
    now: datetime | None = None,
    window_seconds: int = DEFAULT_QUOTA_WINDOW_SECONDS,
) -> bool:
    """Return True when a quota pause (429 cooldown or 0% exhaustion) is currently active."""
    return QuotaMeter(db_path=db_path, window_seconds=window_seconds).is_paused(now=now)


def is_quota_window_expired(
    *,
    db_path: Path | str | None = None,
    now: datetime | None = None,
    window_seconds: int = DEFAULT_QUOTA_WINDOW_SECONDS,
) -> bool:
    """Return True when the active cooldown or rolling quota window has expired."""
    return QuotaMeter(db_path=db_path, window_seconds=window_seconds).is_window_expired(now=now)


def reset_quota_pause(
    *,
    db_path: Path | str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reset any active quota pause/cooldown immediately and restore cycle availability."""
    return QuotaMeter(db_path=db_path).reset_pause(now=now)


def update_quota_pause_from_429(
    raw_output: str = "",
    *,
    reset_seconds: int | None = None,
    db_path: Path | str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Parse a 429 'Resets in...' error and update the persisted quota pause timer."""
    return QuotaMeter(db_path=db_path).record_429_pause(
        raw_output,
        reset_seconds=reset_seconds,
        now=now,
    )
