"""Configuration loading and validation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when a config file is missing required fields or malformed."""


@dataclass(frozen=True)
class PositionAlerts:
    """Per-position thresholds. Any field left as None disables that check."""

    below: float | None = None
    above: float | None = None
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    move_24h_pct: float | None = None


@dataclass(frozen=True)
class Position:
    symbol: str
    coingecko_id: str
    amount: float
    avg_cost: float
    alerts: PositionAlerts = field(default_factory=PositionAlerts)
    note: str = ""

    @property
    def cost_basis(self) -> float:
        return self.amount * self.avg_cost

    def value(self, price: float) -> float:
        return self.amount * price

    def pnl_pct(self, price: float) -> float | None:
        if self.avg_cost <= 0:
            return None
        return (price / self.avg_cost - 1.0) * 100.0


@dataclass(frozen=True)
class MarketGuard:
    """A market-wide regime check, e.g. 'BTC below 73000 means risk-off'."""

    symbol: str
    coingecko_id: str
    below: float | None = None
    above: float | None = None
    note: str = ""


@dataclass(frozen=True)
class PortfolioAlerts:
    value_below: float | None = None
    value_above: float | None = None
    drawdown_pct: float | None = None
    total_pnl_below_pct: float | None = None


@dataclass(frozen=True)
class Config:
    positions: tuple[Position, ...]
    market_guards: tuple[MarketGuard, ...] = ()
    portfolio: PortfolioAlerts = field(default_factory=PortfolioAlerts)
    notifiers: tuple[dict[str, Any], ...] = ({"type": "console"},)
    vs_currency: str = "usd"
    cooldown_minutes: int = 720
    coingecko_api_key: str | None = None
    coingecko_plan: str = "demo"
    redact_amounts: bool = False

    @property
    def coingecko_ids(self) -> list[str]:
        ids = [p.coingecko_id for p in self.positions]
        ids += [g.coingecko_id for g in self.market_guards]
        # Preserve order, drop duplicates.
        return list(dict.fromkeys(ids))

    def total_cost_basis(self) -> float:
        return sum(p.cost_basis for p in self.positions)


def _require(raw: dict[str, Any], key: str, where: str) -> Any:
    if key not in raw:
        raise ConfigError(f"{where}: missing required field {key!r}")
    return raw[key]


def _as_float(value: Any, where: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where}: expected a number, got {value!r}") from exc


def _optional_float(raw: dict[str, Any], key: str, where: str) -> float | None:
    if raw.get(key) is None:
        return None
    return _as_float(raw[key], f"{where}.{key}")


def _parse_position_alerts(raw: dict[str, Any], where: str) -> PositionAlerts:
    return PositionAlerts(
        below=_optional_float(raw, "below", where),
        above=_optional_float(raw, "above", where),
        stop_loss_pct=_optional_float(raw, "stop_loss_pct", where),
        take_profit_pct=_optional_float(raw, "take_profit_pct", where),
        move_24h_pct=_optional_float(raw, "move_24h_pct", where),
    )


def _parse_position(raw: dict[str, Any], index: int) -> Position:
    where = f"positions[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected an object, got {type(raw).__name__}")

    symbol = str(_require(raw, "symbol", where)).upper()
    amount = _as_float(_require(raw, "amount", where), f"{where}.amount")
    if amount < 0:
        raise ConfigError(f"{where}.amount: must not be negative")

    avg_cost = _as_float(raw.get("avg_cost", 0.0), f"{where}.avg_cost")
    if avg_cost < 0:
        raise ConfigError(f"{where}.avg_cost: must not be negative")

    alerts_raw = raw.get("alerts") or {}
    if not isinstance(alerts_raw, dict):
        raise ConfigError(f"{where}.alerts: expected an object")

    return Position(
        symbol=symbol,
        coingecko_id=str(_require(raw, "coingecko_id", where)),
        amount=amount,
        avg_cost=avg_cost,
        alerts=_parse_position_alerts(alerts_raw, f"{where}.alerts"),
        note=str(raw.get("note", "")),
    )


def _parse_guard(raw: dict[str, Any], index: int) -> MarketGuard:
    where = f"market_guards[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected an object, got {type(raw).__name__}")

    guard = MarketGuard(
        symbol=str(_require(raw, "symbol", where)).upper(),
        coingecko_id=str(_require(raw, "coingecko_id", where)),
        below=_optional_float(raw, "below", where),
        above=_optional_float(raw, "above", where),
        note=str(raw.get("note", "")),
    )
    if guard.below is None and guard.above is None:
        raise ConfigError(f"{where}: needs at least one of 'below' or 'above'")
    return guard


def parse_config(raw: dict[str, Any]) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError(f"config root: expected an object, got {type(raw).__name__}")

    positions_raw = _require(raw, "positions", "config")
    if not isinstance(positions_raw, list) or not positions_raw:
        raise ConfigError("config.positions: expected a non-empty list")
    positions = tuple(_parse_position(p, i) for i, p in enumerate(positions_raw))

    seen: set[str] = set()
    for position in positions:
        if position.symbol in seen:
            raise ConfigError(f"config.positions: duplicate symbol {position.symbol!r}")
        seen.add(position.symbol)

    guards_raw = raw.get("market_guards") or []
    if not isinstance(guards_raw, list):
        raise ConfigError("config.market_guards: expected a list")
    guards = tuple(_parse_guard(g, i) for i, g in enumerate(guards_raw))

    portfolio_raw = raw.get("portfolio_alerts") or {}
    if not isinstance(portfolio_raw, dict):
        raise ConfigError("config.portfolio_alerts: expected an object")
    portfolio = PortfolioAlerts(
        value_below=_optional_float(portfolio_raw, "value_below", "config.portfolio_alerts"),
        value_above=_optional_float(portfolio_raw, "value_above", "config.portfolio_alerts"),
        drawdown_pct=_optional_float(portfolio_raw, "drawdown_pct", "config.portfolio_alerts"),
        total_pnl_below_pct=_optional_float(
            portfolio_raw, "total_pnl_below_pct", "config.portfolio_alerts"
        ),
    )

    notifiers_raw = raw.get("notifiers") or [{"type": "console"}]
    if not isinstance(notifiers_raw, list) or not notifiers_raw:
        raise ConfigError("config.notifiers: expected a non-empty list")
    for i, notifier in enumerate(notifiers_raw):
        if not isinstance(notifier, dict) or "type" not in notifier:
            raise ConfigError(f"config.notifiers[{i}]: expected an object with a 'type' field")

    cooldown = int(raw.get("cooldown_minutes", 720))
    if cooldown < 0:
        raise ConfigError("config.cooldown_minutes: must not be negative")

    plan = str(raw.get("coingecko_plan", "demo")).lower()
    if plan not in {"demo", "pro"}:
        raise ConfigError("config.coingecko_plan: expected 'demo' or 'pro'")

    return Config(
        positions=positions,
        market_guards=guards,
        portfolio=portfolio,
        notifiers=tuple(notifiers_raw),
        vs_currency=str(raw.get("vs_currency", "usd")).lower(),
        cooldown_minutes=cooldown,
        coingecko_api_key=raw.get("coingecko_api_key"),
        coingecko_plan=plan,
        redact_amounts=bool(raw.get("redact_amounts", False)),
    )


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"config file not found: {path}. Copy portfolio.example.json and edit it."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: invalid JSON ({exc})") from exc
    return parse_config(raw)
