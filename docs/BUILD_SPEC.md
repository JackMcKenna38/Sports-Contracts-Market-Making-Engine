# Sports Market-Making Engine: Build Spec

Nov 16, 2026 · @Jack McKenna

## How to use this spec

Paste this whole document into a coding LLM with the prompt below, then build one phase at a time. Every endpoint, field name and fee figure here was checked against live docs or a live API call on 16 Nov 2026; anything not confirmed is marked as something to look up, not guessed.

> You are building a research project from the spec below. Read the entire spec before writing any code. Work strictly phase by phase in the order given. At the end of each phase, run its tests, show me the output, and confirm every acceptance criterion is met before starting the next phase. Never invent an endpoint, field name, fee, limit or data value: if the spec does not state it, fetch the linked official documentation or call the API and show me what it returned. If something in the spec turns out to be wrong or out of date, stop and tell me what you found instead of working around it. Never write code that places, signs or cancels a real order, and never ask me for a private key. Use only data the system has actually downloaded or recorded; synthetic data is allowed only inside unit-test fixtures and must be labelled as synthetic. Keep the repo runnable at the end of every phase.

When the LLM finishes a phase, check its acceptance criteria yourself before saying "next".

## Goal, scope and ground rules

Build a public, tested Python system that records Polymarket's NBA and NHL moneyline order books, prices each game from de-vigged bookmaker odds, and simulates a pre-game two-sided market maker on that real data, in backtest and live paper-trading modes.

**In scope**

- NBA and NHL moneyline markets only (`sportsMarketType` = `moneyline`). Other leagues and market types come later, behind a config flag.
- Pre-game quoting only. The engine stops quoting a configurable number of minutes before `gameStartTime`.
- Recording live order books, trades, odds and game status into a database.
- A conservative fill simulator, backtester, risk engine, inventory and merge simulation, and a results report.
- Live paper trading: real live data in, simulated orders and fills out.
- A small OCaml library for binary-contract statistics.

**Ground rules (non-negotiable)**

1. **No live orders, ever.** No order signing, no private keys, no wallet connection. Polymarket lists Australia as close-only on both its website and API, so opening positions from Australia is not allowed; do not route around that with a VPN or an overseas server.
2. **Nothing made up.** Every number in a result, chart or README comes from data the system downloaded or recorded. If a source is down or missing, the run fails loudly; it never fills gaps with synthetic data.
3. **Synthetic data only in tests**, under `tests/fixtures/`, with `synthetic` in the file name.
4. **No personal account data.** No wallet address, profile name or trade history of your own account anywhere in the repo, its history or its issues.
5. **No raw data in Git.** Downloaded trades include other users' wallet addresses; `data/` is git-ignored and only aggregated results are committed.
6. **No backdating.** Commits carry their real dates. The README says this is a 2026 research rebuild of a strategy run on Polymarket from 2024 to 2026.
7. **No original live code.** Everything is written fresh.
8. **Secrets in `.env` only** (the odds API key), git-ignored, with a committed `.env.example`.

## Data sources

Polymarket publishes no historical order books, so the recorder must start on day one: a backtest can only cover dates you recorded yourself. Everything else below has history or is free to poll.

| Source | Endpoint | What it gives | History | Cost |
| --- | --- | --- | --- | --- |
| [Gamma API](https://docs.polymarket.com/market-data/discover-markets.md) | `https://gamma-api.polymarket.com` · `GET /sports`, `GET /markets` (filters `tag_id`, `sports_market_types=moneyline`, `closed`), `GET /teams?league=nba` | Market list and metadata. Fields: `gameStartTime`, `clobTokenIds`, `conditionId`, `orderPriceMinTickSize`, `orderMinSize`, `feeSchedule`, `secondsDelay`, `rewardsMinSize`, `rewardsMaxSpread`, `negRisk`, `acceptingOrders`, `closed` ([field list](https://docs.polymarket.com/market-data/market-details.md)) | Closed markets queryable | Free, no auth |
| [CLOB REST](https://docs.polymarket.com/market-data/prices-order-books.md) | `https://clob.polymarket.com` · `GET /book?token_id=`, `POST /books` (max 500), `GET /midpoint`, `GET /spread` | Current full order book: `bids`, `asks`, `tick_size`, `min_order_size`, `hash`, `timestamp` | Current only | Free, no auth |
| [CLOB market WebSocket](https://docs.polymarket.com/market-data/realtime-data.md) | `wss://ws-subscriptions-clob.polymarket.com/ws/market` · subscribe `{"assets_ids":[…],"type":"market","custom_feature_enabled":true}` | Events `book`, `price_change`, `last_trade_price`, `tick_size_change`, plus `best_bid_ask` and `market_resolved` with the custom flag. Send text `PING` every 10 s | Live only (this is your book history) | Free |
| [Data API v2](https://data-api.polymarket.com/v2/openapi.json) | `https://data-api.polymarket.com/v2/trades?condition=<id>` (cursor paging, max `limit` 1000); `/v2/prices-history?token_id=` | Every fill on a market with price, size, taker side and timestamp; price history | Trades: full. Prices: 1-min kept ≥ 7 days, 5-min ≥ 60 days, 3-h and 12-h permanent; one request spans ≤ 15 days | Free |
| [Sports WebSocket](https://docs.polymarket.com/market-data/realtime-data.md) | `wss://sports-api.polymarket.com/ws` | Live game objects: `gameId`, `leagueAbbreviation`, `status`, `live`, `ended`, `score`, `period`. Server sends `ping` every 5 s; reply `pong` within 10 s | Live only | Free |
| [Liquidity rewards](https://docs.polymarket.com/programs/liquidity-rewards.md) | `GET https://clob.polymarket.com/rewards/markets/multi` | Per-market `rewards_min_size`, `rewards_max_spread`, `rewards_config` (may be empty) | Current | Free |
| [The Odds API](https://the-odds-api.com/liveapi/guides/v4/) | `https://api.the-odds-api.com/v4/sports/{basketball_nba\|icehockey_nhl}/odds?markets=h2h&regions=us,eu`; `/events` (free); `/v4/historical/sports/{sport}/odds?date=` | Bookmaker moneyline prices and `commence_time`. Pinnacle is in the `eu` region | Historical snapshots every 5 min since Sep 2022, paid plans only | Free tier 500 credits/month; paid from US$30/month for 20k. Live odds cost 1 credit per market per region; historical costs 10 |
| [Geoblock check](https://docs.polymarket.com/api-reference/geoblock.md) | `GET https://polymarket.com/api/geoblock` | `blocked`, `country`, `region` for your IP | n/a | Free |

**Facts the engine must read, not hardcode**

- **League tag ids** come from `GET /sports` (each league row has `tags` and `primaryTagId`). Never hardcode a tag id.
- **Fees** come from each market's `feeSchedule`. On 16 Nov 2026 a live NBA moneyline showed `{"rate": 0.05, "exponent": 1, "takerOnly": true, "rebateRate": 0.15}`. The [fee page](https://docs.polymarket.com/trading/fees.md) gives taker fee = C × feeRate × p × (1 − p), where C is shares and p is price; makers pay nothing and receive 15% of sports taker fees as a rebate.
- **Tick and minimum size** come from `orderPriceMinTickSize` and `orderMinSize` (0.001 and 5 on that same market). Round every simulated quote to the tick and drop anything under the minimum.
- **Reward parameters** come from `rewardsMaxSpread` and `rewardsMinSize` (4.5 cents and 50 shares on that market). Scoring: S(v, s) = ((v − s) / v)² × b, sampled once a minute, two-sided required outside the 0.10–0.90 midpoint band, single-sided scaled by c = 3, paid daily at midnight UTC, nothing under $1.
- **Merges** turn 1 YES + 1 NO into 1 pUSD ([positions page](https://docs.polymarket.com/trading/positions/manage.md)). The system simulates merges; it never calls the contract.
- **Rate limits:** read Polymarket's current limits from its docs before building the poller, and back off exponentially on HTTP 429. Do not guess a number.

One practical note: the Data API refuses Python's default user agent (HTTP 403). Send an explicit `User-Agent` header with every request.

## Architecture and repo layout

One pipeline feeds two modes: recorded data drives the backtester, live data drives the paper trader, and both run the same strategy, risk and inventory code so results are comparable.

**Stack:** Python 3.12, `uv` for packaging, `httpx` (REST), `websockets` (streams), `pydantic` (config and message models), SQLite for development and PostgreSQL in Docker for the long-running recorder, NumPy, matplotlib, `pytest` + `hypothesis`, `ruff`, `mypy --strict`, GitHub Actions. OCaml 5 with `dune` for the stats library. The recorder runs 24/7 on a small AWS instance via Docker Compose.

```
repo/
  README.md
  pyproject.toml
  .env.example            # ODDS_API_KEY=
  config/
    default.yaml          # leagues, quoting, risk limits, paths
  src/mm/
    discovery/            # Gamma: leagues, markets, token ids, metadata
    recorder/             # CLOB WS + REST snapshots, sports WS, odds poller
    storage/              # schema, migrations, read/write helpers
    fairvalue/            # de-vig, consensus, odds-to-market matching
    strategy/             # quoter: baseline spread, inventory skew
    risk/                 # limits, kill switch, throttling
    inventory/            # positions, merges, settlement
    sim/                  # book replay, fill model, fees, rebates, rewards
    paper/                # live paper-trading loop
    analytics/            # PnL attribution, markouts, charts, report
    cli.py                # mm discover | record | backtest | paper | report
  ocaml/binstats/         # dune project: calibration, Brier, Kelly
  tests/
    unit/  integration/  fixtures/
  reports/                # committed: aggregated results and charts only
  data/                   # git-ignored: raw recordings and downloads
  docker-compose.yml
  .github/workflows/ci.yml
```

**Database tables**

| Table | Key columns | Written by |
| --- | --- | --- |
| `markets` | condition\_id, token\_id\_yes, token\_id\_no, league, home, away, game\_start\_utc, tick\_size, min\_size, fee\_rate, rebate\_rate, rewards\_max\_spread, rewards\_min\_size, neg\_risk | discovery |
| `book_snapshots` | ts\_recv, ts\_exchange, token\_id, hash, bids\_json, asks\_json, source (ws or rest) | recorder |
| `book_deltas` | ts\_recv, ts\_exchange, token\_id, side, price, size, hash | recorder |
| `trades` | ts, condition\_id, token\_id, side (taker), price, size, tx\_hash | recorder + Data API backfill |
| `odds_snapshots` | ts\_recv, sport, event\_id, commence\_time, bookmaker, home\_price, away\_price | odds poller |
| `game_status` | ts\_recv, game\_id, league, status, live, ended, score, period | sports WS |
| `market_odds_map` | condition\_id, odds\_event\_id, match\_method, confidence | fairvalue matcher |
| `sim_orders` / `sim_fills` | run\_id, ts, token\_id, side, price, size, maker/taker, fee, rebate | sim and paper |
| `pnl_ledger` | run\_id, ts, cash, inventory\_yes, inventory\_no, mark, components | sim and paper |
| `risk_events` | run\_id, ts, rule, value, limit, action | risk |
| `runs` | run\_id, mode, git\_sha, config\_hash, data\_window, started, finished | every run |

Store two timestamps on every recorded message: when you received it and the exchange's own `timestamp`. The gap between them is your measured latency, and the backtester needs it.

&#91;embedded content: pipeline · 5 sources, 1 recorder, 2 modes\]

The recorder is the only thing that talks to the outside world; the backtester and paper trader differ only in where their data comes from.

## Build order

Build in twelve phases, and start the recorder (phase 2) as early as possible: the NBA and NHL seasons are running now, and every day not recorded is a day you can never backtest.

**Phase 0: Repo and tooling**

1. Create the repo, `pyproject.toml`, the folder layout above, `ruff`, `mypy --strict`, `pytest`, pre-commit hooks and the CI workflow.
2. Add `config/default.yaml` loaded through a `pydantic` settings model; every tunable number in later phases lives here, never in code.
3. Add `.env.example`, `.gitignore` (covering `data/`, `.env`, `*.db`) and a README stub stating the ground rules.

*Done when:* `mm --help` runs, CI passes on an empty test suite, and `git check-ignore data/x .env` confirms both are ignored.

**Phase 1: Market discovery**

1. Call `GET /sports`, find the NBA and NHL rows, and read their tag ids.
2. Page through `GET /markets` with each tag id and `sports_market_types=moneyline`; keep open and recently closed games.
3. Parse `clobTokenIds` (a JSON string inside JSON), map YES/NO tokens to home and away teams using the event's `teams` ordering and `GET /teams?league=…`, and store every field listed in the `markets` table.
4. Refresh on a schedule (default every 15 minutes) to pick up newly listed games and changed start times.

*Done when:* `mm discover` lists tonight's NBA and NHL games with start times, both token ids, tick size, minimum size and fee schedule, and the count matches the games shown on Polymarket's site.

**Phase 2: Recorder (start this early and leave it running)**

1. Market WebSocket client: subscribe to every discovered token with `custom_feature_enabled`, send `PING` every 10 s, store `book` snapshots and `price_change` deltas with both timestamps.
2. Rebuild a local book from snapshot + deltas and compare its `hash` with the hash in each message; on mismatch, fetch `GET /book` and store a fresh REST snapshot.
3. Take a REST snapshot of every book every 60 s anyway, as a checkpoint for replay.
4. Store `last_trade_price` events, and backfill trades per market from `/v2/trades?condition=` after each game resolves.
5. Sports WebSocket client: reply `pong` within 10 s, store every game update.
6. Reconnect with exponential backoff and jitter; after any reconnect, resubscribe and take REST snapshots before trusting deltas again. Log every gap.
7. Package it in Docker Compose with PostgreSQL and run it on a small AWS instance with restart-always.

*Done when:* the recorder has run 72 hours unattended, the gap log shows every disconnect and its recovery, and a replayed book matches a REST snapshot taken at the same moment.

**Phase 3: Odds ingestion and matching**

1. Poll `/v4/sports/{sport}/odds` with `markets=h2h` for `us` and `eu` regions. Budget credits: the free 500 a month does not cover frequent polling, so either poll sparingly (for example 3 snapshots per game: open, T−6 h, T−1 h) or buy the US$30 tier. Log `x-requests-remaining` on every call.
2. Use the free `/events` endpoint for schedules and `commence_time`.
3. Match each Polymarket market to an odds event by league, both team names (via a hand-checked alias table) and start time within a configurable window. Store method and confidence; send anything below full confidence to a review file, never auto-accept it.
4. Optional: buy historical odds for the dates you recorded, so the backtest uses odds as they stood at each moment, not today's.

*Done when:* every recorded game is matched or listed for review, and a spot check of 20 matches by hand finds no errors.

**Phase 4: Fair value**

1. Convert decimal odds to implied probabilities and remove the bookmaker margin. Implement multiplicative (proportional), power and Shin methods behind one interface.
2. Build a consensus probability: a configurable weighted average of bookmakers, with Pinnacle as the default anchor when present.
3. Fair value at time t uses only odds received at or before t. Enforce this in code, not by convention.

*Done when:* unit tests confirm each de-vig method's probabilities sum to 1, and a no-lookahead test fails if any future snapshot leaks in.

**Phase 5: Fill simulator and backtester**

1. Replay recorded snapshots and deltas in exchange-timestamp order, with a configurable latency added to your own order actions (default: the median receive-to-exchange gap you measured).
2. Queue model: a simulated resting order joins the back of its price level. It fills only when recorded trades at that price consume all size ahead of it, or the price trades through it. Default to this conservative model and make it impossible to switch off silently.
3. Apply each market's real tick, minimum size, taker fee formula and maker rebate. Estimate liquidity rewards with the published scoring formula, reported separately and flagged as an estimate.
4. Settle every position at resolution from the recorded `market_resolved` event or the market's final state.
5. Every run writes a `runs` row with the git commit, config hash and data window, so any result can be reproduced.

*Done when:* the same run twice gives identical output, a hand-built fixture of 10 orders fills exactly as worked out on paper, and a do-nothing strategy shows zero PnL.

**Phase 6: Quoting strategy**

1. Baseline: quote a bid and ask a fixed distance either side of fair value, rounded to tick, at a fixed size.
2. Inventory skew: shift both quotes against your inventory so the engine leans towards flat; make the skew strength a config value.
3. Requote when fair value moves more than a threshold, and cancel all quotes immediately when new odds arrive that move fair value more than a larger threshold.
4. Stop quoting at `gameStartTime` minus a configurable buffer, and cancel everything at that point.
5. Never cross your own quotes and never quote outside 0.01–0.99.

*Done when:* backtests of the baseline and the skewed version run on at least 4 recorded weeks and produce a comparison table.

**Phase 7: Inventory and merges**

1. Track YES and NO holdings per market. When both are held, simulate merging the matched quantity into cash at 1 per pair.
2. Settle unmerged holdings at resolution: winning tokens pay 1, losing tokens 0.

*Done when:* tests show merged and unmerged paths give identical total value at settlement, and cash plus inventory always reconciles to starting capital plus PnL.

**Phase 8: Risk engine**

1. Daily loss cap: stop all quoting for the rest of the UTC day once realised plus marked loss reaches the limit.
2. Per-event exposure limit on net inventory value.
3. Volume throttle: cut quote size by a set fraction after each loss on an event, and stop quoting that event after a set number of losses.
4. Stale-data kill switch: pull all quotes if the book, odds or game feed has not updated within a set time.
5. Log every trigger to `risk_events`.

*Done when:* each rule has a test that breaches it and confirms the engine stops, and a backtest shows the triggers in its risk log.

**Phase 9: Analytics and report**

1. PnL attribution: spread capture, inventory gains and losses, fees paid, rebates and estimated rewards, each separately.
2. Markouts: for every fill, the move in fair value and in the mid 1, 5 and 30 minutes later. This measures adverse selection.
3. Charts: cumulative PnL, PnL by league, markout curves, inventory over time, fill rate by distance from fair value.
4. `mm report` writes Markdown and PNGs into `reports/`, stamped with run id, data window and config hash.

*Done when:* the report rebuilds from a clean checkout plus the recorded database with one command.

**Phase 10: Live paper trading**

1. Run the same strategy, risk and inventory code against the live feeds.
2. Simulate fills against live trades with the same queue model as the backtester.
3. Write everything to the same tables with `mode = paper`, and add a terminal dashboard (for example `rich`) showing quotes, inventory, PnL and risk state.
4. The code path must contain no order client at all; add a test that fails if any order-signing package is importable from `mm.paper`.

*Done when:* it runs through a full NBA night unattended, and a backtest of that same night on recorded data lands close to the paper result, with any gap explained.

**Phase 11: OCaml library**

1. A `dune` library `binstats` with calibration curves, Brier score and Kelly sizing for binary contracts, each with tests.
2. A small CLI that reads fills and outcomes as CSV and prints calibration of your fair values.
3. Run it in CI next to the Python tests.

*Done when:* `dune test` passes in CI and the README shows one calibration table produced by it from recorded data.

## Testing and verification

The test suite must prove three things: the code is correct, the backtest cannot see the future, and every published number traces back to recorded data.

**Unit tests (every module)**

- Odds conversion and every de-vig method, including edge cases (missing bookmaker, odds of 1.01, a three-way price).
- Book rebuild from snapshot + deltas, including a delta that removes a level and an out-of-order message.
- Fill model on hand-built synthetic fixtures with answers worked out on paper.
- Fee formula against the worked example on Polymarket's fee page (100 shares at 0.50, rate 0.05, gives a fee of 1.25).
- Reward score against the published formula.
- Every risk rule, breached and not breached.
- Merge and settlement arithmetic.

**Property tests (`hypothesis`)**

- Cash plus marked inventory always equals starting capital plus PnL.
- Quotes are always on-tick, inside 0.01–0.99, at or above minimum size, and never crossed.
- Inventory never goes negative.

**Integrity tests**

- **No lookahead:** run a backtest where future data is replaced with poison values; results must not change.
- **Determinism:** two runs with the same data and config produce byte-identical output.
- **Replay accuracy:** books rebuilt from recorded deltas match the periodic REST snapshots by `hash`.
- **Provenance:** every figure in `reports/` carries the run id that produced it, and that run id exists in the database.
- **Safety:** a test fails if any order-signing or wallet library is importable anywhere in `src/mm`.

**Integration tests (run nightly, not on every push)**

- Hit each live endpoint once and validate the response against the `pydantic` model, so an API change fails loudly.
- Run the recorder for 5 minutes against live data and check rows land in every table.

**CI:** `ruff`, `mypy --strict`, unit and property tests and `dune test` on every push; integration tests nightly. Target 85% line coverage on `src/mm`, excluding the CLI.

## Contingencies

Every failure below must be detected and logged, never silently patched over; the default fallback is to stop and report rather than to guess.

| Problem | How you detect it | What the system does |
| --- | --- | --- |
| WebSocket drops | No message or `PONG` within the timeout | Reconnect with backoff and jitter, resubscribe, take REST snapshots, log the gap |
| Book hash mismatch | Rebuilt book's hash differs from the message's `hash` | Discard local book, fetch `GET /book`, log it |
| Gap in recorded data | Gap log, or no messages for a live market | Backtester excludes that market's window and reports how much was excluded |
| API field renamed or removed | `pydantic` validation error | Fail the run with the raw payload saved; fix the model, never coerce |
| Rate limited (HTTP 429) | Status code | Exponential backoff; reduce poll frequency; log it |
| Data API refuses requests (HTTP 403) | Status code | Check the `User-Agent` header first; then check the API version, since v1 was retired in favour of v2 |
| Odds API credits run out | `x-requests-remaining` near zero | Drop to the minimum snapshot schedule; never fill missing odds with old ones beyond a configured age |
| No odds for a game | No matched odds event | Do not quote that game; record why |
| Odds-to-market match uncertain | Confidence below threshold | Send to the review file; exclude until confirmed by hand |
| Start time changes or game postponed | `gameStartTime` or `commence_time` changes on refresh | Update the stop-quoting time; if postponed, cancel quotes and exclude until it reopens |
| Market closes early or `acceptingOrders` goes false | Discovery refresh or `market_resolved` event | Cancel simulated quotes; settle at resolution |
| Tick size changes mid-game | `tick_size_change` event | Re-round all quotes; log it |
| Clock drift on the server | Receive time minus exchange time drifts | Run NTP on the host; alert if drift passes a threshold |
| Database full or down | Write errors | Recorder buffers briefly, then exits loudly so the restart policy and alert fire |
| Geoblock or access change | `GET /api/geoblock` result changes, or reads start failing | Paper trading and recording continue only while public reads work; never route around a block |
| Fee schedule changes | `feeSchedule` differs from the stored value | Store the new schedule with its effective time; backtests use the schedule in force at each moment |
| Rewards program changes | `rewards_config` empty or different | Report rewards as zero for those markets; never assume old rates |
| Off-season (no NBA/NHL games) | Discovery finds no markets | Recorder idles; optionally add other leagues through config |
| Result looks too good | Sharpe or win rate far above live results | Re-run the no-lookahead and fill-model tests before publishing anything |

## Deliverables and definition of done

The project is finished when a stranger can clone the repo, read the README, and reproduce the published results from the recorded data with one command.

**README must contain**

- One paragraph: a 2026 research rebuild of a pre-game sports market-making strategy run on Polymarket from 2024 to 2026, written fresh, paper-trading only.
- The architecture diagram and a quick-start (`mm discover`, `mm record`, `mm backtest`, `mm paper`, `mm report`).
- Data sources with links, and the data window actually recorded.
- Results: PnL attribution table, markout chart, cumulative PnL chart and risk-event summary, each labelled with its run id and data window.
- Limitations, stated plainly: queue-position assumptions, odds snapshot frequency, rewards as estimates, no live execution.

**Definition of done**

- [ ] At least 4 weeks of continuous NBA and NHL recordings, with the gap log published as a summary.
- [ ] Baseline and inventory-skew strategies backtested on that data with a comparison table.
- [ ] At least one full NBA night run in paper mode, compared with its backtest.
- [ ] All tests pass in CI, including the no-lookahead, determinism and safety tests.
- [ ] OCaml `binstats` builds and tests in CI.
- [ ] No wallet address, private key, API key or raw data anywhere in Git history (check with a secret scanner such as `gitleaks`).
- [ ] Every number in the README traces to a run id in the database.

**Sources** (all checked 16 Nov 2026)

- [Polymarket: discover markets (Gamma API)](https://docs.polymarket.com/market-data/discover-markets.md)
- [Polymarket: market details and field names](https://docs.polymarket.com/market-data/market-details.md)
- [Polymarket: prices and order books (CLOB REST)](https://docs.polymarket.com/market-data/prices-order-books.md)
- [Polymarket: real-time data (market and sports WebSockets)](https://docs.polymarket.com/market-data/realtime-data.md)
- [Polymarket: trading fees](https://docs.polymarket.com/trading/fees.md)
- [Polymarket: liquidity rewards](https://docs.polymarket.com/programs/liquidity-rewards.md)
- [Polymarket: split, merge and redeem](https://docs.polymarket.com/trading/positions/manage.md)
- [Polymarket: geoblock](https://docs.polymarket.com/api-reference/geoblock.md)
- [Polymarket Data API v2 OpenAPI spec](https://data-api.polymarket.com/v2/openapi.json)
- [The Odds API v4 guide](https://the-odds-api.com/liveapi/guides/v4/)
- [The Odds API plans](https://the-odds-api.com/)
