"""Locate v30.3 integration points in src/live/v30_live.py."""
import re
PATH = "src/live/v30_live.py"
PATTERNS = [
    "def __init__", "LiveSymbolState", "OrderRequest(", "_manage_open",
    "tp=", "open_tp1", "open_tp2", "def _process_", "def _save_state",
    "def _load_state", "open_ticket", "def _handle_entry", "async def",
    "def _try_entry", "def _on_bar", "def _on_new_bar", "last_m1_close",
    "def _persist", "def _restore", "close_position", "POS_CLOSED",
    "send_order", "def _enter_", "def _exit_", "open_size_lots",
    "self.partial_", "ATRTracker", "def _read_bars", "bar.high", "bar.low",
    "for bar in", "_on_close", "broker_close", "self.bridge.modify",
    "broker_sync", "_close_position",
]
with open(PATH, encoding="utf-8") as f:
    for i, line in enumerate(f, 1):
        for p in PATTERNS:
            if p in line:
                print(f"{i:5d}: {line.rstrip()[:160]}")
                break
