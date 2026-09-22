"""Portfolio valuation and alert rule evaluation."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config, Position
from .prices import Quote

INFO = "info"
WARN = "warn"
CRITICAL = "critical"

_SEVERITY_ORDER = {CRITICAL: 0, WARN: 1, INFO: 2}


@dataclass(frozen=True)
class Alert:
    """A fired rule.

    `key` identifies the rule instance and drives de-duplication: the same key
    stays silent until the condition clears and re-arms (or the cooldown lapses).
    """

    key: str
    severity: str
    title: str
    body: str


@dataclass(frozen=True)
class PositionSnapshot:
    position: Position
    price: float
    change_24h_pct: float | None

    @property
    def value(self) -> float:
        return self.position.value(self.price)

    @property
    def pnl_pct(self) -> float | None:
        return self.position.pnl_pct(self.price)

    @property
    def pnl_abs(self) -> float:
        return self.value - self.position.cost_basis


@dataclass(frozen=True)
class PortfolioSnapshot:
    positions: tuple[PositionSnapshot, ...]
    missing_ids: tuple[str, ...] = ()

    @property
    def total_value(self) -> float:
        return sum(p.value for p in self.positions)

    @property
    def total_cost(self) -> float:
        return sum(p.position.cost_basis for p in self.positions)

    @property
    def total_pnl_abs(self) -> float:
        return self.total_value - self.total_cost

    @property
    def total_pnl_pct(self) -> float | None:
        cost = self.total_cost
        if cost <= 0:
            return None
        return (self.total_value / cost - 1.0) * 100.0

    def change_24h(self) -> tuple[float, float] | None:
        """Portfolio-wide 24h move as (percent, share of value covered).

        Reconstructs yesterday's value from each position's own 24h change,
        which weights every holding by size - a 13% move in a 3% position is
        not the same event as a 13% move in a 30% one.

        Positions without change data are left out of both sides rather than
        silently treated as flat, and the share they leave uncovered is
        reported so a partial figure is never mistaken for a complete one.
        Returns None when nothing can be computed.
        """
        now = 0.0
        yesterday = 0.0
        for item in self.positions:
            if item.change_24h_pct is None:
                continue
            factor = 1.0 + item.change_24h_pct / 100.0
            if factor <= 0:
                # A -100% move gives no finite prior price; skip rather than
                # divide by zero.
                continue
            now += item.value
            yesterday += item.value / factor

        if yesterday <= 0:
            return None
        total = self.total_value
        covered = (now / total * 100.0) if total > 0 else 0.0
        return (now / yesterday - 1.0) * 100.0, covered

    def weight(self, snapshot: PositionSnapshot) -> float | None:
        total = self.total_value
        if total <= 0:
            return None
        return snapshot.value / total * 100.0


def build_snapshot(config: Config, quotes: dict[str, Quote]) -> PortfolioSnapshot:
    snapshots: list[PositionSnapshot] = []
    missing: list[str] = []
    for position in config.positions:
        quote = quotes.get(position.coingecko_id)
        if quote is None:
            missing.append(position.coingecko_id)
            continue
        snapshots.append(
            PositionSnapshot(
                position=position,
                price=quote.price,
                change_24h_pct=quote.change_24h_pct,
            )
        )
    return PortfolioSnapshot(positions=tuple(snapshots), missing_ids=tuple(missing))


def _fmt_price(value: float) -> str:
    if value >= 1000:
        return f"{value:,.0f}"
    if value >= 1:
        return f"{value:,.2f}"
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _fmt_money(value: float) -> str:
    return f"{value:,.2f}"


def _fmt_pct(value: float) -> str:
    return f"{value:+.1f}%"


def _pnl_body(symbol, price_text, pnl_pct, position, snapshot, unit, redact):
    """Body for a stop-loss / take-profit alert.

    Prices, thresholds and percentages are public information. The absolute
    unrealised amount is not: it reveals position size, so redaction drops it.
    """
    body = (
        f"{symbol} at {price_text} is {_fmt_pct(pnl_pct)} vs average cost "
        f"{_fmt_price(position.avg_cost)} {unit}."
    )
    if redact:
        return body
    return f"{body} Unrealised P&L {_fmt_money(snapshot.pnl_abs)} {unit}."


def evaluate_position(
    snapshot: PositionSnapshot, vs_currency: str, redact: bool = False
) -> list[Alert]:
    alerts: list[Alert] = []
    position = snapshot.position
    rules = position.alerts
    symbol = position.symbol
    unit = vs_currency.upper()
    price_text = f"{_fmt_price(snapshot.price)} {unit}"

    if rules.below is not None and snapshot.price <= rules.below:
        alerts.append(
            Alert(
                key=f"{symbol}:below:{rules.below}",
                severity=WARN,
                title=f"{symbol} below {_fmt_price(rules.below)} {unit}",
                body=f"{symbol} is at {price_text} (threshold {_fmt_price(rules.below)} {unit}).",
            )
        )

    if rules.above is not None and snapshot.price >= rules.above:
        alerts.append(
            Alert(
                key=f"{symbol}:above:{rules.above}",
                severity=INFO,
                title=f"{symbol} above {_fmt_price(rules.above)} {unit}",
                body=f"{symbol} is at {price_text} (threshold {_fmt_price(rules.above)} {unit}).",
            )
        )

    pnl_pct = snapshot.pnl_pct
    if pnl_pct is not None and rules.stop_loss_pct is not None and pnl_pct <= rules.stop_loss_pct:
        alerts.append(
            Alert(
                key=f"{symbol}:stop_loss:{rules.stop_loss_pct}",
                severity=CRITICAL,
                title=f"{symbol} hit stop-loss ({_fmt_pct(pnl_pct)})",
                body=_pnl_body(symbol, price_text, pnl_pct, position, snapshot, unit, redact),
            )
        )

    if (
        pnl_pct is not None
        and rules.take_profit_pct is not None
        and pnl_pct >= rules.take_profit_pct
    ):
        alerts.append(
            Alert(
                key=f"{symbol}:take_profit:{rules.take_profit_pct}",
                severity=INFO,
                title=f"{symbol} hit take-profit ({_fmt_pct(pnl_pct)})",
                body=_pnl_body(symbol, price_text, pnl_pct, position, snapshot, unit, redact),
            )
        )

    change = snapshot.change_24h_pct
    if change is not None and rules.move_24h_pct is not None and abs(change) >= rules.move_24h_pct:
        direction = "up" if change > 0 else "down"
        alerts.append(
            Alert(
                key=f"{symbol}:move24h:{direction}:{rules.move_24h_pct}",
                severity=WARN if change < 0 else INFO,
                title=f"{symbol} moved {_fmt_pct(change)} in 24h",
                body=f"{symbol} is {direction} {_fmt_pct(change)} over 24h, now {price_text}.",
            )
        )

    return alerts


def evaluate_market_guards(config: Config, quotes: dict[str, Quote]) -> list[Alert]:
    alerts: list[Alert] = []
    unit = config.vs_currency.upper()

    for guard in config.market_guards:
        quote = quotes.get(guard.coingecko_id)
        if quote is None:
            continue
        suffix = f" {guard.note}" if guard.note else ""

        if guard.below is not None and quote.price <= guard.below:
            alerts.append(
                Alert(
                    key=f"guard:{guard.symbol}:below:{guard.below}",
                    severity=CRITICAL,
                    title=f"Regime guard: {guard.symbol} below {_fmt_price(guard.below)} {unit}",
                    body=(
                        f"{guard.symbol} is at {_fmt_price(quote.price)} {unit}, at or below "
                        f"the {_fmt_price(guard.below)} {unit} level.{suffix}"
                    ),
                )
            )

        if guard.above is not None and quote.price >= guard.above:
            alerts.append(
                Alert(
                    key=f"guard:{guard.symbol}:above:{guard.above}",
                    severity=INFO,
                    title=f"Regime guard: {guard.symbol} above {_fmt_price(guard.above)} {unit}",
                    body=(
                        f"{guard.symbol} is at {_fmt_price(quote.price)} {unit}, at or above "
                        f"the {_fmt_price(guard.above)} {unit} level.{suffix}"
                    ),
                )
            )

    return alerts


def evaluate_portfolio(
    snapshot: PortfolioSnapshot, config: Config, peak_value: float | None
) -> list[Alert]:
    alerts: list[Alert] = []
    rules = config.portfolio
    unit = config.vs_currency.upper()
    redact = config.redact_amounts
    total = snapshot.total_value

    if rules.value_below is not None and total <= rules.value_below:
        alerts.append(
            Alert(
                key=f"portfolio:value_below:{rules.value_below}",
                severity=CRITICAL,
                title=(
                    "Portfolio below its floor"
                    if redact
                    else f"Portfolio below {_fmt_money(rules.value_below)} {unit}"
                ),
                body=(
                    "Total portfolio value is below the configured floor."
                    if redact
                    else f"Total portfolio value is {_fmt_money(total)} {unit}."
                ),
            )
        )

    if rules.value_above is not None and total >= rules.value_above:
        alerts.append(
            Alert(
                key=f"portfolio:value_above:{rules.value_above}",
                severity=INFO,
                title=(
                    "Portfolio above its target"
                    if redact
                    else f"Portfolio above {_fmt_money(rules.value_above)} {unit}"
                ),
                body=(
                    "Total portfolio value is above the configured target."
                    if redact
                    else f"Total portfolio value is {_fmt_money(total)} {unit}."
                ),
            )
        )

    pnl_pct = snapshot.total_pnl_pct
    if (
        pnl_pct is not None
        and rules.total_pnl_below_pct is not None
        and pnl_pct <= rules.total_pnl_below_pct
    ):
        alerts.append(
            Alert(
                key=f"portfolio:pnl_below:{rules.total_pnl_below_pct}",
                severity=CRITICAL,
                title=f"Portfolio P&L at {_fmt_pct(pnl_pct)}",
                body=(
                    "Total value is below total cost."
                    if redact
                    else (
                        f"Total value {_fmt_money(total)} {unit} against cost "
                        f"{_fmt_money(snapshot.total_cost)} {unit}."
                    )
                ),
            )
        )

    if rules.move_24h_pct:
        measured = snapshot.change_24h()
        if measured is not None:
            change, covered = measured
            direction = "up" if change > 0 else "down"
            widest = max(rules.move_24h_pct)
            partial = "" if covered >= 99.5 else f" Based on {covered:.0f}% of portfolio value."
            for band in rules.move_24h_pct:
                if abs(change) < band:
                    continue
                alerts.append(
                    Alert(
                        key=f"portfolio:move24h:{direction}:{band}",
                        # The widest configured band, downward, is the one
                        # worth a louder notification; the rest are context.
                        severity=WARN if (direction == "down" and band == widest) else INFO,
                        title=f"Portfolio {direction} {abs(change):.1f}% in 24h ({band:g}% band)",
                        body=(
                            f"Total portfolio value moved {change:+.1f}% over 24h, "
                            f"crossing the {band:g}% band.{partial}"
                        ),
                    )
                )

    if rules.drawdown_pct is not None and peak_value and peak_value > 0:
        drawdown = (1.0 - total / peak_value) * 100.0
        if drawdown >= rules.drawdown_pct:
            alerts.append(
                Alert(
                    key=f"portfolio:drawdown:{rules.drawdown_pct}",
                    severity=CRITICAL,
                    title=f"Portfolio drawdown {drawdown:.1f}% from peak",
                    body=(
                        f"Portfolio is {drawdown:.1f}% below its observed peak."
                        if redact
                        else (
                            f"Value {_fmt_money(total)} {unit} is {drawdown:.1f}% below the "
                            f"observed peak of {_fmt_money(peak_value)} {unit}."
                        )
                    ),
                )
            )

    return alerts


def evaluate_all(
    config: Config,
    snapshot: PortfolioSnapshot,
    quotes: dict[str, Quote],
    peak_value: float | None,
) -> list[Alert]:
    alerts: list[Alert] = []
    for position_snapshot in snapshot.positions:
        alerts.extend(
            evaluate_position(position_snapshot, config.vs_currency, config.redact_amounts)
        )
    alerts.extend(evaluate_market_guards(config, quotes))
    alerts.extend(evaluate_portfolio(snapshot, config, peak_value))
    alerts.sort(key=lambda alert: (_SEVERITY_ORDER.get(alert.severity, 9), alert.key))
    return alerts


def format_report(snapshot: PortfolioSnapshot, config: Config) -> str:
    """Human-readable portfolio table, used by --report and console alerts."""
    unit = config.vs_currency.upper()
    lines = [
        f"{'SYMBOL':<8}{'PRICE':>14}{'24H':>9}{'VALUE':>14}{'P&L':>12}{'WEIGHT':>9}",
        "-" * 66,
    ]
    for item in sorted(snapshot.positions, key=lambda s: s.value, reverse=True):
        change = "n/a" if item.change_24h_pct is None else f"{item.change_24h_pct:+.1f}%"
        pnl = "n/a" if item.pnl_pct is None else f"{item.pnl_pct:+.1f}%"
        weight = snapshot.weight(item)
        weight_text = "n/a" if weight is None else f"{weight:.1f}%"
        lines.append(
            f"{item.position.symbol:<8}"
            f"{_fmt_price(item.price):>14}"
            f"{change:>9}"
            f"{_fmt_money(item.value):>14}"
            f"{pnl:>12}"
            f"{weight_text:>9}"
        )

    lines.append("-" * 66)
    total_pnl_pct = snapshot.total_pnl_pct
    total_pnl_text = "n/a" if total_pnl_pct is None else f"{total_pnl_pct:+.1f}%"
    lines.append(
        f"{'TOTAL':<8}{'':>14}{'':>9}"
        f"{_fmt_money(snapshot.total_value):>14}{total_pnl_text:>12}{'':>9}"
    )
    lines.append(
        f"Cost {_fmt_money(snapshot.total_cost)} {unit} · "
        f"Unrealised {_fmt_money(snapshot.total_pnl_abs)} {unit}"
    )
    if snapshot.missing_ids:
        lines.append(f"WARNING: no price returned for {', '.join(snapshot.missing_ids)}")
    return "\n".join(lines)
