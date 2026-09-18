"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
import time

from .config import ConfigError, load_config
from .notify import NotifierError, build_notifiers, dispatch
from .prices import PriceFetchError, fetch_quotes
from .rules import build_snapshot, evaluate_all, format_report
from .state import AlertState

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ALERTS = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="portfolio-alert",
        description="Watch a crypto spot portfolio and alert on price thresholds.",
    )
    parser.add_argument(
        "-c", "--config", default="portfolio.json", help="config file (default: portfolio.json)"
    )
    parser.add_argument(
        "-s",
        "--state",
        default="alert_state.json",
        help="alert state file used for de-duplication (default: alert_state.json)",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="keep running and re-check on an interval instead of exiting",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=900,
        help="seconds between checks in --watch mode (default: 900)",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="print the portfolio table and exit without evaluating or sending alerts",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate the config file and exit without any network call",
    )
    parser.add_argument(
        "--reset-peak",
        action="store_true",
        help="reset the stored portfolio peak to the current value (drawdown baseline)",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="do not print the table")
    parser.add_argument(
        "--fail-on-alert",
        action="store_true",
        help=f"exit with status {EXIT_ALERTS} when any alert fires",
    )
    return parser


def run_once(args: argparse.Namespace) -> int:
    config = load_config(args.config)

    quotes = fetch_quotes(
        config.coingecko_ids,
        config.vs_currency,
        api_key=config.coingecko_api_key,
        plan=config.coingecko_plan,
    )
    snapshot = build_snapshot(config, quotes)
    report = format_report(snapshot, config)

    if not args.quiet:
        print(report)

    if args.report:
        return EXIT_OK

    state = AlertState(args.state)
    if args.reset_peak:
        state.reset_peak(snapshot.total_value)

    # Read the peak before observing this run's value, so a fresh all-time high
    # cannot mask the drawdown that the same run is meant to detect.
    peak_before = state.peak_value
    state.observe_value(snapshot.total_value)

    alerts = evaluate_all(config, snapshot, quotes, peak_before)
    to_send = state.select_new(alerts, cooldown_seconds=config.cooldown_minutes * 60)

    exit_code = EXIT_OK
    if to_send:
        notifiers = build_notifiers(config.notifiers)
        # The table carries totals and position values. With redaction on it
        # stays local: it is printed to stdout above but never sent to a sink.
        outbound_report = "" if config.redact_amounts else report
        errors = dispatch(notifiers, to_send, outbound_report)
        for error in errors:
            print(f"notifier failed: {error}", file=sys.stderr)
        if errors:
            # Delivery failed, so do not let these count as sent: the next run
            # must retry rather than sit on an alert nobody received.
            state.rollback(to_send)
            exit_code = EXIT_ERROR
        elif args.fail_on_alert:
            exit_code = EXIT_ALERTS

    state.save()

    if snapshot.missing_ids:
        print(
            f"warning: no price returned for {', '.join(snapshot.missing_ids)} "
            "- check the coingecko_id values in your config",
            file=sys.stderr,
        )
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.interval <= 0:
        print("error: --interval must be positive", file=sys.stderr)
        return EXIT_ERROR

    try:
        if args.check_config:
            config = load_config(args.config)
            build_notifiers(config.notifiers)
            print(
                f"config OK: {len(config.positions)} positions, "
                f"{len(config.market_guards)} market guards, "
                f"{len(config.notifiers)} notifiers"
            )
            return EXIT_OK

        if not args.watch:
            return run_once(args)

        last_code = EXIT_OK
        while True:
            try:
                last_code = run_once(args)
            except PriceFetchError as exc:
                # A transient outage must not kill a long-running watcher.
                print(f"price fetch failed: {exc}", file=sys.stderr)
                last_code = EXIT_ERROR
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return EXIT_OK
    except (ConfigError, NotifierError, PriceFetchError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
