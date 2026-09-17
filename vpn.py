#!/usr/bin/env python3
"""
VPN-based IP rotation — replaces the old direct/proxy split as the way this
app avoids getting stuck behind a single blocked IP.

Wraps `nordvpn_switcher`, which drives the NordVPN Linux CLI (`nordvpn`)
already installed and logged in inside the container (see Dockerfile —
login happens via `nordvpn login --token` in the entrypoint, before this
module is ever used, so `initialize_VPN()`'s own interactive login prompt
is never reached).

`rotate()` is the only thing scraper.py calls: on a 403, it asks this
module to move to a new NordVPN server, then retries the request. Actual
rotation is serialized behind a process-wide lock and a cooldown — VPN
rotation changes the whole container's egress IP, so concurrent
detail-fetch threads all hitting a block at once must not each trigger
their own rotation.
"""

import subprocess
import threading
import time

from nordvpn_switcher import initialize_VPN, rotate_VPN

from config import NORDVPN_COUNTRIES, VPN_ROTATE_COOLDOWN_SECONDS

_lock = threading.Lock()
_instructions = None
_last_rotation = 0.0


def _ensure_initialized_locked():
    global _instructions
    if _instructions is not None:
        return
    countries = [c.strip() for c in NORDVPN_COUNTRIES.split(",") if c.strip()]
    area_input = countries or ["complete rotation"]
    print(f"Initializing NordVPN rotation (area: {area_input})...")
    _instructions = initialize_VPN(area_input=area_input, save=0, skip_settings=1)
    print("NordVPN rotation initialized.")


def _status_line() -> str:
    """One-line summary of `nordvpn status` for the log — server/country/IP,
    whatever the CLI reports right now."""
    try:
        out = subprocess.run(
            ["nordvpn", "status"], capture_output=True, text=True, timeout=10
        ).stdout
        fields = [line.strip() for line in out.splitlines() if line.strip()]
        return ", ".join(fields) if fields else "status unavailable"
    except Exception as e:
        return f"status unavailable ({e})"


def rotate(reason: str = "") -> bool:
    """Move to a new NordVPN server. Returns True if this identity's IP has
    (recently) changed and the caller should retry its request, False if
    rotation itself failed (caller should fall back to the timed cooldown).

    A rotation that completed within VPN_ROTATE_COOLDOWN_SECONDS is treated
    as a success without rotating again — several threads hitting a block
    around the same time share one rotation instead of piling up retries.
    """
    global _last_rotation
    with _lock:
        try:
            _ensure_initialized_locked()
        except Exception as e:
            print(f"VPN initialization failed: {e}")
            return False

        if time.time() - _last_rotation < VPN_ROTATE_COOLDOWN_SECONDS:
            print(f"[VPN] reusing rotation from {time.time() - _last_rotation:.1f}s ago ({reason})")
            return True

        print(f"[VPN] rotating ({reason})...")
        try:
            rotate_VPN(instructions=_instructions)
        except Exception as e:
            print(f"[VPN] rotation failed: {e}")
            return False
        _last_rotation = time.time()
        print(f"[VPN] rotated ({reason}) -> {_status_line()}")
        return True
