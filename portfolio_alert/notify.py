"""Notification backends."""

from __future__ import annotations

import sys
from typing import Any, Iterable, Sequence

import requests

from .rules import CRITICAL, WARN, Alert

_PREFIX = {CRITICAL: "[CRITICAL]", WARN: "[WARN]", "info": "[INFO]"}
_NTFY_PRIORITY = {CRITICAL: "urgent", WARN: "high", "info": "default"}


class NotifierError(RuntimeError):
    """Raised when a notifier is misconfigured."""


class Notifier:
    def send(self, alerts: Sequence[Alert], report: str) -> None:  # pragma: no cover
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    def __init__(self, stream: Any = None, show_report: bool = False):
        self.stream = stream or sys.stdout
        self.show_report = show_report

    def send(self, alerts: Sequence[Alert], report: str) -> None:
        if self.show_report and report:
            print(report, file=self.stream)
            print("", file=self.stream)
        for alert in alerts:
            prefix = _PREFIX.get(alert.severity, "[INFO]")
            print(f"{prefix} {alert.title}", file=self.stream)
            print(f"    {alert.body}", file=self.stream)


class NtfyNotifier(Notifier):
    """Pushes to a ntfy.sh topic — installs as a phone app, no account needed."""

    def __init__(
        self,
        topic: str,
        server: str = "https://ntfy.sh",
        token: str | None = None,
        timeout: float = 15.0,
    ):
        if not topic:
            raise NotifierError("ntfy notifier: 'topic' is required")
        self.topic = topic
        self.server = server.rstrip("/")
        self.token = token
        self.timeout = timeout

    def send(self, alerts: Sequence[Alert], report: str) -> None:
        headers: dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        for alert in alerts:
            requests.post(
                f"{self.server}/{self.topic}",
                data=alert.body.encode("utf-8"),
                headers={
                    **headers,
                    "Title": alert.title,
                    "Priority": _NTFY_PRIORITY.get(alert.severity, "default"),
                },
                timeout=self.timeout,
            ).raise_for_status()


class WebhookNotifier(Notifier):
    """Posts a single JSON payload — works with Slack and Discord webhooks."""

    def __init__(self, url: str, timeout: float = 15.0):
        if not url:
            raise NotifierError("webhook notifier: 'url' is required")
        self.url = url
        self.timeout = timeout

    def send(self, alerts: Sequence[Alert], report: str) -> None:
        if not alerts:
            return
        lines = [f"{_PREFIX.get(a.severity, '[INFO]')} {a.title}\n{a.body}" for a in alerts]
        text = "\n\n".join(lines)
        if report:
            text = f"{text}\n\n```\n{report}\n```"
        requests.post(
            self.url,
            json={"text": text, "content": text},
            timeout=self.timeout,
        ).raise_for_status()


def build_notifier(spec: dict[str, Any]) -> Notifier:
    kind = str(spec.get("type", "")).lower()
    if kind == "console":
        return ConsoleNotifier(show_report=bool(spec.get("show_report", False)))
    if kind == "ntfy":
        return NtfyNotifier(
            topic=str(spec.get("topic", "")),
            server=str(spec.get("server", "https://ntfy.sh")),
            token=spec.get("token"),
        )
    if kind == "webhook":
        return WebhookNotifier(url=str(spec.get("url", "")))
    raise NotifierError(f"unknown notifier type {kind!r} (expected console, ntfy or webhook)")


def build_notifiers(specs: Iterable[dict[str, Any]]) -> list[Notifier]:
    return [build_notifier(spec) for spec in specs]


def dispatch(
    notifiers: Sequence[Notifier], alerts: Sequence[Alert], report: str
) -> list[Exception]:
    """Send to every notifier; a failing one must not silence the others."""
    errors: list[Exception] = []
    for notifier in notifiers:
        try:
            notifier.send(alerts, report)
        except Exception as exc:  # noqa: BLE001 - one bad sink must not stop the rest
            errors.append(exc)
    return errors
