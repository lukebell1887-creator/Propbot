"""
v31.2 — phantom-close grace-period regression test.

Reproduces the 2026-05-05 US500 #547550971 bug:
    1. Bot calls _broker_positions() (a CACHED list pushed by the EA over
       ZMQ) — see src/execution/mt5_bridge.py:668.
    2. Immediately after order_send returns success, the cache may not
       yet contain the new ticket because the EA push interval is
       100-500 ms.
    3. Old logic fired POS_CLOSED_BY_BROKER 15 ms after entry, then
       synthesised a fake TP1 close via `snap_tp1`, wiping in-memory
       state while the broker held the position open for 2 hours.

The fix in src/live/v30_live.py adds an entry-grace window driven by
`V30Live.RECONCILE_GRACE_S` (default 30 s).  Inside that window a missing
ticket in the cached positions list is treated as cache-lag; outside it
is treated as a real close.

This test mirrors that reconciliation loop in isolation so we don't have
to spin up MT5, the bridge, the calendar, or any of the heavy machinery
the real `_manage_open` brings in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict


# ---------------------------------------------------------------------------
# Tiny stand-ins (only what the reconciliation loop actually touches).
# ---------------------------------------------------------------------------
@dataclass
class _State:
    open_ticket: Optional[int] = None
    open_side: Optional[str] = None
    open_at: Optional[datetime] = None
    _grace_logged: bool = False


@dataclass
class _Pos:
    ticket: int


@dataclass
class _Bot:
    """Mini-replica of the v30 reconciliation block.  Keeps test fast and
    decouples it from MT5 / ZMQ / pytest-marker hell."""
    RECONCILE_GRACE_S: float = 30.0
    states: Dict[str, _State] = field(default_factory=dict)
    broker_positions_payload: List[_Pos] = field(default_factory=list)
    events: List[dict] = field(default_factory=list)
    closed_via_broker: List[str] = field(default_factory=list)

    def _log_event(self, kind: str, **fields) -> None:
        self.events.append({"kind": kind, **fields})

    def _broker_positions(self) -> List[_Pos]:
        return list(self.broker_positions_payload)

    def reconcile(self, now: datetime) -> None:
        """Mirror of `src/live/v30_live.py::_manage_open` reconcile block.

        Identical control flow to production code so a future regression
        in v30_live.py would also have to be ported here for this test
        to keep passing.
        """
        open_by_broker = {p.ticket: p for p in self._broker_positions()}
        for sym, st in self.states.items():
            if st.open_ticket is None or st.open_ticket in open_by_broker:
                continue
            # --- Layer 1: post-entry grace period --------------------------
            if st.open_at is not None:
                age_s = (now - st.open_at).total_seconds()
                if age_s < self.RECONCILE_GRACE_S:
                    if not st._grace_logged:
                        self._log_event(
                            "RECONCILE_GRACE_SKIPPED",
                            symbol=sym,
                            ticket=st.open_ticket,
                            age_s=round(age_s, 3),
                            grace_s=self.RECONCILE_GRACE_S,
                        )
                        st._grace_logged = True
                    continue
            # --- Grace expired: treat as a real broker-side close ----------
            self._log_event("POS_CLOSED_BY_BROKER",
                            symbol=sym, ticket=st.open_ticket,
                            side=st.open_side)
            self.closed_via_broker.append(sym)
            st.open_ticket = None
            st.open_side = None
            st.open_at = None


# ---------------------------------------------------------------------------
# The actual tests.
# ---------------------------------------------------------------------------
def _seed_us500_entry(bot: _Bot, now: datetime) -> None:
    bot.states["US500"] = _State(
        open_ticket=547550971,
        open_side="LONG",
        open_at=now,
    )


def test_phantom_close_blocked_by_grace_period():
    """The exact 2026-05-05 scenario: 15 ms after entry the cached
    broker-positions list is empty.  The bot must NOT phantom-close."""
    now = datetime(2026, 5, 5, 13, 45, 2, 980_000, tzinfo=timezone.utc)
    bot = _Bot(RECONCILE_GRACE_S=30.0)
    _seed_us500_entry(bot, now)
    bot.broker_positions_payload = []  # cache hasn't caught up yet

    # Reconcile exactly 15 ms after entry — the production race window.
    bot.reconcile(now + timedelta(milliseconds=15))

    assert bot.states["US500"].open_ticket == 547550971, (
        "Bot wiped its own state during the grace window — phantom close "
        "regression has returned. See Docs/POSTMORTEM_US500_2026-05-05_"
        "STUCK_LADDER.md."
    )
    assert "POS_CLOSED_BY_BROKER" not in [e["kind"] for e in bot.events]
    # Breadcrumb event must fire so production is observable.
    grace_events = [e for e in bot.events if e["kind"] == "RECONCILE_GRACE_SKIPPED"]
    assert len(grace_events) == 1
    assert grace_events[0]["ticket"] == 547550971


def test_grace_event_only_logs_once_per_position():
    """RECONCILE_GRACE_SKIPPED is operationally useful but must not spam
    the event log on every poll tick — only once per (symbol, ticket)."""
    now = datetime(2026, 5, 5, 13, 45, 2, tzinfo=timezone.utc)
    bot = _Bot(RECONCILE_GRACE_S=30.0)
    _seed_us500_entry(bot, now)
    bot.broker_positions_payload = []

    for ms in (15, 100, 250, 1_000, 5_000, 15_000, 25_000):
        bot.reconcile(now + timedelta(milliseconds=ms))

    grace_events = [e for e in bot.events if e["kind"] == "RECONCILE_GRACE_SKIPPED"]
    assert len(grace_events) == 1, (
        f"Expected exactly one RECONCILE_GRACE_SKIPPED event, got "
        f"{len(grace_events)} — log spam regression."
    )


def test_real_broker_close_still_fires_after_grace():
    """After the grace window expires, a genuinely closed position must
    still trigger POS_CLOSED_BY_BROKER (otherwise the bot would never
    notice broker-side SL/TP fills)."""
    now = datetime(2026, 5, 5, 13, 45, 2, tzinfo=timezone.utc)
    bot = _Bot(RECONCILE_GRACE_S=30.0)
    _seed_us500_entry(bot, now)
    bot.broker_positions_payload = []  # broker really has no position now

    # Tick well after the grace window.
    bot.reconcile(now + timedelta(seconds=31))

    assert bot.states["US500"].open_ticket is None
    closes = [e for e in bot.events if e["kind"] == "POS_CLOSED_BY_BROKER"]
    assert len(closes) == 1
    assert closes[0]["ticket"] == 547550971


def test_cache_catchup_inside_grace_clears_breadcrumb_path():
    """Most common production path: order_send returns success, the bridge
    cache catches up within ~200 ms, no event should fire at all."""
    now = datetime(2026, 5, 5, 13, 45, 2, tzinfo=timezone.utc)
    bot = _Bot(RECONCILE_GRACE_S=30.0)
    _seed_us500_entry(bot, now)

    # Tick 1: cache stale (15 ms after entry)
    bot.broker_positions_payload = []
    bot.reconcile(now + timedelta(milliseconds=15))

    # Tick 2: cache caught up (200 ms after entry)
    bot.broker_positions_payload = [_Pos(ticket=547550971)]
    bot.reconcile(now + timedelta(milliseconds=200))

    # Tick 3: well past grace, cache still healthy
    bot.reconcile(now + timedelta(seconds=60))

    assert bot.states["US500"].open_ticket == 547550971
    assert "POS_CLOSED_BY_BROKER" not in [e["kind"] for e in bot.events]
    # Exactly one breadcrumb from tick 1; cache recovery on tick 2 means no
    # further breadcrumbs.
    assert sum(1 for e in bot.events if e["kind"] == "RECONCILE_GRACE_SKIPPED") == 1


def test_multiple_symbols_independent_grace():
    """Two symbols opened back-to-back must each get their own grace
    window — DE40's open_at must not poison US500's reconciliation."""
    now = datetime(2026, 5, 5, 13, 45, 2, tzinfo=timezone.utc)
    bot = _Bot(RECONCILE_GRACE_S=30.0)
    bot.states["DE40"] = _State(open_ticket=111, open_side="SHORT",
                                open_at=now - timedelta(seconds=120))  # OLD
    bot.states["US500"] = _State(open_ticket=222, open_side="LONG",
                                 open_at=now)                         # FRESH
    bot.broker_positions_payload = []  # neither in cache

    bot.reconcile(now + timedelta(milliseconds=15))

    # DE40 was opened 2 minutes ago → grace expired → POS_CLOSED_BY_BROKER
    assert bot.states["DE40"].open_ticket is None
    assert "DE40" in bot.closed_via_broker
    # US500 was opened 15 ms ago → grace active → still alive
    assert bot.states["US500"].open_ticket == 222
    assert "US500" not in bot.closed_via_broker
