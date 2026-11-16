# Design notes

How the system is put together, which wire formats it assumes, and which of those assumptions
have never been checked against a live service. The specification is `docs/BUILD_SPEC.md`.

## Status of verification

On 16 November 2026 the Polymarket-facing code was run against the live public services from a
US cloud host (read-only; the geoblock endpoint reports that host as blocked for trading, which
does not affect public reads). `mm discover` listed 315 NBA and NHL moneyline markets (271
open), `mm record` ran for several five-minute sessions on 542 tokens, `mm verify`, `mm backtest`,
`mm paper` and `mm report` ran on those recordings, and `pytest -m integration` passed except
for the two Odds API tests, which skip without a key. Those recordings were smoke tests and were
discarded; no result in this repository comes from them.

The first versions of the models were written from the official open-source clients
(`Polymarket/rs-clob-client`, `Polymarket/py-clob-client`, read only, never installed) and the
build spec. Running them live found these differences, all now fixed and covered by tests:

- Gamma `/markets` rows carry no `homeTeamName` / `awayTeamName`. Home and away come from the
  league's `ordering` field on `/sports` (`away` means `outcomes[0]` is the away team), checked
  against the `teams[].ordering` of individual `/events` rows. Fetching every event for this was
  rejected: each is about 1 MB.
- Closed markets come back without `feeSchedule`. A market recorded while open keeps its stored
  terms and is marked closed; its winner is read from `outcomePrices` of exactly 0 and 1.
- Data API v2 `/trades` serves `{data, pagination}` with snake_case rows (`token_id`,
  `condition_id`, `transaction_hash`, `timestamp` in seconds) and an opaque
  `pagination.next_cursor`; the backfill follows it until `has_more` is false.
- CLOB REST books return `last_trade_price: ""` for tokens that never traded. The field is not
  read.
- The market channel broadcasts `new_market` for every market on the exchange. It is accepted
  and ignored; `market_resolved` is acted on only for subscribed tokens.
- `last_trade_price` carries `transaction_hash` and is emitted once per fill, on the taker's
  token. `sim.trade_assets: both` therefore mirrors NO-token trades into YES space; `yes_only`
  would drop roughly half the flow.
- The sports channel sends one JSON object per frame for every league on the platform
  (`gameId`, `leagueAbbreviation`, `homeTeam`, `awayTeam`, `status`, `score`, `period`, `live`,
  `ended`); some leagues use other id fields. Frames for leagues outside the config are dropped
  before validation. Its published AsyncAPI document describes a different, slug-keyed shape.
- The stream occasionally never delivers a level change (seen a few times per five minutes
  across 542 tokens, always on a token and its complement together), and a REST book can be a
  few milliseconds ahead of the stream during a burst. See "Book integrity" below.
- Game moneylines currently have an empty `rewards_config`, so the reward estimate is zero for
  them. The pool is read per market from `GET /rewards/markets/{condition_id}`.
- Published rate limits (docs `api-reference/rate-limits`) are far above this system's usage:
  300 requests per 10 s on Gamma `/markets`, 500 on CLOB `/books`, 300 on Data API
  `/v2/trades`. The client spaces requests 0.2 s apart per host and backs off on 429 and 5xx.

- Every `last_trade_price` in a live sample moved the traded token's ask level and the other
  token's bid level at the complementary price, which is what the YES-space fill model assumes.

A model that does not match still fails validation, saves the raw payload under
`data/failed_payloads/` and stops or quarantines; nothing is coerced. The nightly workflow
(`.github/workflows/nightly.yml`) repeats the live checks from GitHub's runners; its first
scheduled run, on 16 November 2026, passed.

## Wire formats in use

- Gamma `/sports` row: `sport`, `tags` (comma string), `primaryTagId`, `ordering`.
- Gamma `/markets` params: `tag_id`, `sports_market_types`, `closed`, `end_date_min`, `limit`,
  `offset`. Row fields read: `id`, `conditionId`, `question`, `slug`, `outcomes`,
  `outcomePrices`, `clobTokenIds` (JSON strings), `gameStartTime`, `orderPriceMinTickSize`,
  `orderMinSize`, `feeSchedule {rate, exponent, takerOnly, rebateRate}`, `secondsDelay`,
  `rewardsMinSize`, `rewardsMaxSpread` (cents), `negRisk`, `acceptingOrders`, `closed`,
  `sportsMarketType`, `events[0].gameId`.
- CLOB `GET /book`, `POST /books` (body `[{"token_id": ...}]`): `market`, `asset_id`,
  `timestamp` (ms string), `hash`, `bids`, `asks`, `min_order_size`, `tick_size`, `neg_risk`.
- CLOB `GET /rewards/markets/{condition_id}`: `data[].rewards_config[] {start_date, end_date,
  rate_per_day}`.
- Market channel subscribe: `{"type":"market","operation":"subscribe","markets":[],
  "assets_ids":[...],"initial_dump":true,"custom_feature_enabled":true}`; text `PING` every
  10 s, answered `PONG`. Messages carry `event_type` and a millisecond-string `timestamp`:
  `book`, `price_change` (`price_changes[] {asset_id, price, size, side, hash}`, both tokens of
  a market in one message), `last_trade_price`, `tick_size_change`, `best_bid_ask` (validated,
  not stored), `market_resolved`, `new_market` (ignored).
- Data API `GET /v2/trades?condition=<id>&limit=<n>` then `?condition=<id>&cursor=<c>`.
- The Odds API v4 `GET /sports/{sport}/odds` and `/events`: `id`, `sport_key`,
  `commence_time`, `home_team`, `away_team`, `bookmakers[].markets[].outcomes[] {name, price}`;
  headers `x-requests-remaining`, `x-requests-used`.

## Still unverified

- **The Odds API.** No key was available, so its client, the polling schedule and the credit
  handling have only run against synthetic payloads. The request parameters, response fields,
  credit headers and costs were re-read from its live v4 guide on 16 November 2026 and match.
  The matcher was run over all 271 open Polymarket markets against events named with the full
  team names in `config/team_aliases.yaml` and auto-confirmed every one with the right home
  side, but real bookmaker spellings have not been seen. Nothing has been quoted on real odds.
- `config/team_aliases.yaml`: every outcome name Polymarket listed on 16 November 2026 resolves
  through it (the NHL spelling `Utah` was added), but the bookmaker-side names have not been
  checked against real Odds API responses.
- `tick_size_change`, `market_resolved` and NBA or NHL frames on the sports channel did not
  occur during the live sessions; their models follow the official client and the frames seen
  for other leagues.
- Fee `exponent` is applied as `(p(1-p))**exponent`; only exponent 1 has been seen.
- Reward scoring applies `rewards_min_size` per recorded price level (the book has no per-order
  sizes), and `b` in S(v, s) is `rewards.multiplier` (default 1).
- `rewardsMaxSpread` is read as cents and stored divided by 100.
- The Docker Compose stack builds and passes its checks in CI and the test suite passes on
  PostgreSQL, but the stack itself has not recorded live data.
- No run longer than a few minutes has been made. The 72-hour and 4-week recordings are what
  show reconnect behaviour, memory use and database growth.

## Book integrity

The recorder keeps a local book per token from `book` dumps and `price_change` deltas and
compares it with a REST checkpoint every `recorder.rest_snapshot_seconds`:

- same hash, different levels: a lost update, logged at once;
- REST newer with different levels: adopted (it is the truth either way) and held as a suspect.
  A delta stamped up to the checkpoint's time, plus `analytics.replay_skew_ms`, means the change
  was only in flight. A suspect still unexplained at the token's next checkpoint is logged.

Each lost update is one `recorder_gaps` row with reason `lost_update`. `mm verify` rebuilds
every book from the recorded stream alone, compares levels with every checkpoint using the same
skew window, and exits non-zero only for a mismatch the recorder did not log. The backtest
replays REST checkpoints too, so its book is repaired at the same moments the live one was;
between a lost update and the next checkpoint (at most a minute) one level's size is wrong.

## Design decisions

- Matching: auto-confirmed only when canonical home/away equal the odds event's in order, YES
  maps to one of them and the start is inside `start_window_minutes`. Swapped teams, outcome-only
  matches, loose times (within `loose_window_hours`) and ties go to the review CSV with
  confidence 0.5. A person sets `confirmed=yes` there; the next run stores it as `manual`.
  The engine must use `confirmed_only=True` mappings.
- Engine: quotes are re-centred only when fair moves by `requote_threshold` (the anchor);
  a move of `cancel_threshold` on new odds cancels both quotes first. A resting order is kept
  while its price is unchanged (queue priority), even if the wanted size changed. A market
  does not quote while it is unmatched, has no fair value, is past its stop time, its game is
  live or ended, or any market-WS gap covering it is open (time in gaps is `excluded_ms`).
  Orders whose cost would exceed cash minus what open orders reserve are not placed. Marks use
  fair value. Markouts are read from the state just before the first event after `t + h`.
- Shin and power fall back to the raw implied probabilities when a book has no overround.

- Odds polling: a league poll is due when any open game in it is new (`poll_on_new_game`) or has
  crossed one of `poll_minutes_before_start`; one poll covers every due key. At or below
  `min_credits_reserve` only `reduced_poll_minutes_before_start` applies; below one poll's cost
  polling stops. Without `ODDS_API_KEY` the recorder logs an error and records everything else.

- One `Engine` consumes normalised events (`mm.types`) in both modes. Backtest merges DB rows into
  a single ordered stream plus synthetic `Tick`s; paper taps the live recorder.
- Everything is priced in YES-token space. A bid is BUY YES at p; an ask is BUY NO at 1 - p. Holding
  both merges to cash at 1 per pair, so inventory is never negative.
- Fill model: post-only; an order joins the back of its level (queue = displayed size at activation);
  trades at the price eat the queue first; a trade through the price fills up to the trade size;
  optionally a book that crosses the order fills it (`sim.fill_on_book_cross`, pessimistic). Cancels
  ahead never shorten the queue. All fills are maker: zero fee, rebate = rebate_rate x taker fee,
  reported as an estimate.
- Order actions take effect at event time + latency (median recv - exchange gap unless configured).
- REST snapshots are replayed at receive time and only applied when not older than the book.
- Fair value is push-fed (`observe`) so it cannot see the future; consensus is a weighted mean of
  de-vigged books, quotes older than `fairvalue.max_odds_age_minutes` are dropped.
- Attribution: spread capture = edge to fair at fill; inventory = position x change in fair;
  identity cash + marked inventory = start + spread + inventory + rebates - fees must hold.
- Risk "loss on an event" = an inventory round trip (flat to flat) with negative cash flow.
- Backtest run ids are a hash of config, git sha, window and strategy, so reruns replace rows and
  output is byte-identical. Paper run ids include the start time.
- Validation failures on WS messages go to the `quarantine` table and open a gap; REST discovery
  failures stop the run.
- Reward estimate treats the whole recorded book as one competing maker (conservative share).
- Paper trading runs the same `Engine` on the recorder's live events. The recorder publishes
  book, trade, game, odds, metadata and gap events to listeners; markets and confirmed mappings
  are pushed after every discovery refresh; odds already in the database warm fair value at
  start. An engine exception stops the run instead of being absorbed by a feed's reconnect loop.
- The game feed counts as stale when the sports stream has been disconnected for longer than
  `risk.stale_game_feed_seconds`, because the feed only speaks when a game changes state.
- Risk events are edge-triggered: one row when a rule starts to bind, none while it stays bound.
- Reports contain no wall-clock value, so a rebuilt report is byte-identical. Every PNG carries
  its run id in the footer and in the file's metadata, and each report folder has a
  `manifest.json` naming the run, config hash and git commit.

## Run tables

- `sim_fills`: one row per fill. `side`/`price`/`token_id` are in real-token terms (BUY YES at p,
  or BUY NO at 1 - p); `yes_side` (`BUY`/`SELL`) and `yes_price` are the same fill in YES space;
  `fair` and `mid` are the YES-space fair value and book mid at fill time (null if unknown);
  `liquidity` is `maker`; `seq` is unique and increasing within a run.
- `sim_markouts`: one row per fill and horizon. `fair_move = fair(t + h) - fair(t)` and
  `mid_move = mid(t + h) - mid(t)`: raw YES-space moves, not signed by fill side (analytics
  multiplies by +1 for `yes_side = BUY`, -1 for `SELL`). Null when either end is unavailable.
- `sim_orders`: `action` is `place`, `cancel` or `reject`; `fair` is fair value when the action
  was taken; `order_id` matches `sim_fills.order_id`.
- `pnl_ledger`: per-market rows have `cash = Position.cash_flow`, `inventory_yes`,
  `inventory_no`, `mark` and `components = json(Position.components())` (keys `spread`,
  `inventory`, `rebates`, `fees`, `merged`, `volume`). Portfolio rows use `condition_id = "*"`,
  `cash = Portfolio.cash`, summed inventories, `mark = null` and `components =
  json(Portfolio.components() | {"equity": ..., "rewards_estimate": ...})`. One row per
  `sim.ledger_interval_seconds` and a final row at the end of the run.
- `risk_events`: the fields of `mm.risk.engine.RiskEvent`, portfolio-wide rules with
  `condition_id = "*"`.
- `runs.summary_json`: `equity`, `pnl`, `components` (portfolio totals), `rewards_estimate`,
  `latency_ms`, `markets`, `fills`, `settled`, `unsettled`, `excluded_ms` (market-time dropped
  because of recorder gaps). No wall-clock values, so reruns are byte-identical.
- `runs.summary_json` also carries `events` (backtest) and `never_quoted` (reason -> number of
  markets that never had an order placed).
