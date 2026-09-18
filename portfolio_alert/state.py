"""Persistent alert state: de-duplication and peak tracking."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Iterable

from .rules import Alert


class AlertState:
    """Tracks which alerts are currently firing, so each one notifies once.

    An alert stays silent while its condition holds. It notifies again only
    after the condition clears and re-arms, or after `cooldown_seconds` have
    passed while it is still active.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._active: dict[str, float] = {}
        self._peak_value: float | None = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt state file must not stop alerting; start clean instead.
            return
        if not isinstance(raw, dict):
            return
        active = raw.get("active")
        if isinstance(active, dict):
            self._active = {
                str(key): float(value)
                for key, value in active.items()
                if isinstance(value, (int, float))
            }
        peak = raw.get("peak_value")
        if isinstance(peak, (int, float)):
            self._peak_value = float(peak)

    def save(self) -> None:
        payload = {"active": self._active, "peak_value": self._peak_value}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically so an interrupted run cannot truncate the state file.
        fd, tmp_path = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
            os.replace(tmp_path, self.path)
        except BaseException:
            Path(tmp_path).unlink(missing_ok=True)
            raise

    @property
    def peak_value(self) -> float | None:
        return self._peak_value

    def observe_value(self, total_value: float) -> None:
        if total_value <= 0:
            return
        if self._peak_value is None or total_value > self._peak_value:
            self._peak_value = total_value

    def reset_peak(self, total_value: float | None = None) -> None:
        self._peak_value = total_value

    def select_new(
        self,
        alerts: Iterable[Alert],
        *,
        cooldown_seconds: float,
        now: float | None = None,
    ) -> list[Alert]:
        """Return the alerts that should notify, and update the active set."""
        now = time.time() if now is None else now
        alerts = list(alerts)
        current_keys = {alert.key for alert in alerts}

        # Conditions that no longer hold re-arm for next time.
        for key in list(self._active):
            if key not in current_keys:
                del self._active[key]

        to_send: list[Alert] = []
        for alert in alerts:
            fired_at = self._active.get(alert.key)
            if fired_at is None or (now - fired_at) >= cooldown_seconds:
                to_send.append(alert)
                self._active[alert.key] = now
        return to_send
