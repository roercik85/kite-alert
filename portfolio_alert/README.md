# portfolio_alert

Watches a crypto spot portfolio and notifies you when a price, a position's
profit and loss, or the portfolio as a whole crosses a level you defined in
advance — so thresholds get chosen calmly rather than in reaction to a moving
chart.

Self-contained: nothing here touches the rest of the repository, and the
directory can be moved elsewhere as a unit.

All commands below run from the repository root.

## What it checks

**Per position**

| Rule | Fires when |
|---|---|
| `below` | price falls to or under an absolute level |
| `above` | price rises to or over an absolute level |
| `stop_loss_pct` | profit and loss vs. average cost falls to or under this percentage |
| `take_profit_pct` | profit and loss vs. average cost rises to or over this percentage |
| `move_24h_pct` | absolute 24h move reaches this percentage |

**Market guards** — a regime check on an asset you may not even hold, e.g.
"tell me if BTC loses 73 000", because that level tends to decide what the rest
of the portfolio does.

**Portfolio level** — total value bands, total profit and loss, drawdown from
the highest value the tool has observed, and 24h move bands.

`move_24h_pct` takes one number or several, e.g. `[2, 5]`. The move is
reconstructed from each position's own 24h change, weighted by position size,
so a 13% jump in a 3% holding is not mistaken for the same event as a 13% jump
in a 30% one. Positions whose 24h change is unavailable are left out of the
calculation rather than counted as flat, and the alert says what share of the
portfolio it covers when that happens.

Every band that is breached fires, so a +6% day with `[2, 5]` sends two
notifications rather than one. That is deliberate: it keeps both bands marked
as active, so a portfolio oscillating between them does not re-notify on every
crossing. The widest band downward is a `warn`; everything else is `info`.

Pick these bands against how much your portfolio actually moves. A basket of
high-beta altcoins can cross 2% on most days, and an alert you learn to ignore
is worse than no alert.

Alerts are graded `critical` / `warn` / `info` and sorted with the worst first.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Only dependency is `requests`. Prices come from the free CoinGecko API; no
account or key is required.

## Configure

```bash
cp portfolio.example.json portfolio.json
$EDITOR portfolio.json
python3 -m portfolio_alert --check-config
```

`portfolio.json` and `alert_state.json` are gitignored. **Keep real amounts and
cost basis there — the committed example carries placeholder amounts on
purpose.**

Each position needs a `coingecko_id`, which is the `id` field from
`https://api.coingecko.com/api/v3/coins/list` (the API's own name for the coin,
e.g. `akash-network`). A typo there surfaces as a warning about a missing price
rather than a silently empty position.

```json
{
  "positions": [
    {
      "symbol": "AKT",
      "coingecko_id": "akash-network",
      "amount": 1000,
      "avg_cost": 0.6727,
      "alerts": { "below": 0.45, "stop_loss_pct": -35, "move_24h_pct": 15 }
    }
  ],
  "market_guards": [
    { "symbol": "BTC", "coingecko_id": "bitcoin", "below": 73000 }
  ],
  "portfolio_alerts": { "drawdown_pct": 15 },
  "notifiers": [{ "type": "console" }]
}
```

Any threshold left out is simply not checked.

## Run

```bash
python3 -m portfolio_alert                      # check once, print the table, alert
python3 -m portfolio_alert --report             # table only, no alerts, no state written
python3 -m portfolio_alert --watch              # keep checking every 15 minutes
python3 -m portfolio_alert --quiet              # alerts only, good for cron
python3 -m portfolio_alert --check-config       # validate config, no network call
python3 -m portfolio_alert --test-notification  # send one test alert through every sink
```

`--test-notification` fetches no prices and writes no state. It exists because
a quiet run and a broken delivery path look the same from the outside: both
produce no notification.

Exit codes: `0` success, `1` error, `2` with `--fail-on-alert` when something fired.

## Notifications

```json
"notifiers": [
  { "type": "console" },
  { "type": "ntfy", "topic": "your-private-topic-name" },
  { "type": "webhook", "url": "https://discord.com/api/webhooks/..." }
]
```

- **console** — prints to stdout.
- **ntfy** — pushes to your phone. Install the ntfy app, subscribe to a topic,
  put the same topic here. Topic names are public to anyone who guesses them,
  so choose something long and random, or point `server` at your own instance.
- **webhook** — one JSON POST containing every alert. The payload carries both
  `text` and `content`, so the same config works for Slack and for Discord.

A failing notifier is reported on stderr but does not stop the others.

## Privacy

`ntfy.sh` and webhook endpoints are third-party servers, and an ntfy topic is
readable by anyone who knows or guesses its name. Two settings limit what a
push actually discloses.

**Pick an unguessable topic.** Generate one rather than choosing it:

```bash
python3 -c "import secrets,string; print('pa-'+''.join(secrets.choice(string.ascii_lowercase+string.digits) for _ in range(28)))"
```

**Turn on redaction.** With `"redact_amounts": true` at the top level of the
config, currency amounts are stripped from everything that leaves the machine,
while prices, thresholds and percentages — all public information — are kept,
so alerts stay actionable:

| | Sent |
|---|---|
| without redaction | `AKT at 0.42 USD is -37.6% vs average cost 0.6727 USD. Unrealised P&L -1,365.61 USD.` |
| with redaction | `AKT at 0.42 USD is -37.6% vs average cost 0.6727 USD.` |
| without redaction | `Value 7,831.56 USD is 23.3% below the observed peak of 10,204.91 USD.` |
| with redaction | `Portfolio is 23.3% below its observed peak.` |

Redaction also withholds the portfolio table from every sink, including the
webhook payload that would otherwise embed it. The full table is still printed
to your own terminal — redaction governs what leaves the machine, not what you
see locally.

What redaction cannot hide is which coins you watch: a push naming AKT says you
follow AKT. To conceal that too, self-host ntfy by pointing `server` at your own
instance, or add a `token` for an access-controlled topic.

## No alert spam

An alert fires once when its condition becomes true and then stays quiet. It
notifies again only after the condition clears and comes back, or after
`cooldown_minutes` (default 720, i.e. twice a day) while it is still true.

That state lives in `alert_state.json`, alongside the running peak value used
for drawdown. Delete the file to start fresh; use `--reset-peak` to rebaseline
drawdown to today's value without losing anything else.

## Scheduling

Nothing is checked unless something runs the tool on a schedule. Subscribing a
phone to a topic does not by itself poll anything.

### GitHub Actions (no machine of your own)

`.github/workflows/portfolio-alert.yml` runs the check every 5 minutes, which is the shortest interval GitHub accepts.

1. Add your finished `portfolio.json` as a repository secret named
   `PORTFOLIO_CONFIG` (Settings → Secrets and variables → Actions → New
   repository secret). Paste the whole file as the value. This keeps holdings
   out of the repository while still letting the run read them.
2. Merge the workflow into the default branch. **Scheduled workflows only run
   from the default branch** — on a feature branch it will never fire on its
   own.
3. Trigger it once by hand from the Actions tab (*Run workflow*) to confirm the
   setup before relying on the schedule. Tick **Send a test notification** on
   that dialog to prove the delivery path reaches your phone, rather than
   waiting for a real threshold to break — a successful scheduled run that
   found nothing to report looks identical to one whose push never arrived.

De-duplication state and the drawdown peak are carried between runs through the
Actions cache. Polling more often does not mean more notifications: an alert is
suppressed while its condition holds, so the interval controls how quickly a
breach is noticed, not how often it is announced. If that cache is ever evicted the tool simply re-notifies on
conditions that are still true; it does not lose money-relevant state.

Two limits worth knowing. GitHub schedules on a best-effort basis and delays
or drops runs under load, and it does so most at the shortest intervals, so a
5-minute cron in practice lands somewhere between 5 and 20 minutes apart.
Treat the interval as a ceiling on staleness, not a promise. And runners share
outbound IP addresses, so the free ntfy.sh tier — whose message quota is
per-IP — can intermittently answer `429`. A failed delivery is rolled back and
retried on the next run rather than being silently dropped, but if it recurs,
move to a Discord webhook, an ntfy access token, or a self-hosted ntfy server.

### Your own machine

```cron
*/15 * * * * cd /path/to/repo && .venv/bin/python -m portfolio_alert --quiet >> alerts.log 2>&1
```

Use absolute paths — config and state files resolve against the working
directory. This runs from your own IP, which avoids the shared-quota problem
above, but only while the machine is awake.

## Delivery failures

An alert is marked as sent before the notifier runs. If every sink fails, that
mark is rolled back so the next run retries instead of sitting on an alert
nobody received, and the process exits non-zero.

When several sinks are configured and only one fails, the retry sends a
duplicate to the sinks that already succeeded. That is deliberate: a repeated
alert is recoverable, a missed stop-loss is not.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

No test reaches the network: the price client is exercised through a fake
session, and the CLI tests stub the fetch.

## Scope

A monitoring tool. It reads public prices and reports when a level you chose
has been crossed. It holds no keys, connects to no exchange, and cannot place
or cancel an order. It does not predict prices and is not financial advice.
