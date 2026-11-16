# Running the recorder on a server

The recorder only reads public data and never needs a wallet or exchange credentials. It must
run somewhere with lawful access to Polymarket's public APIs; do not route around a block.

This procedure has been checked as far as CI can: the image builds, the compose file validates
and the test suite passes on PostgreSQL. The stack itself has not yet been run for longer than
a few minutes against live data, so watch the first day closely.

## 1. Server

A small instance is enough: 2 vCPU and 2 GB of memory (the recorder used under 100 MB in
testing). Disk is the thing to size. Recording every listed game (542 tokens on 16 November
2026) wrote about 2.9 GB of database a day off-peak, so allow 150 GB for four weeks, or set
`recorder.record_hours_before_start: 48` in `config/default.yaml` before building the image to
record only games within two days of starting (about 1.1 GB a day). Check `docker system df -v`
during the first days; game nights are busier than the off-peak hours this was measured in. Install Docker with the Compose plugin and
keep the clock synchronised (chrony or systemd-timesyncd); the recorder warns when receive time
drifts more than `recorder.drift_alert_ms` from exchange time.

## 2. Start

```bash
git clone https://github.com/JackMcKenna38/Market-Making-Engine.git && cd Market-Making-Engine
```

```bash
cp .env.example .env
```

Set `POSTGRES_PASSWORD` in `.env`, and `ODDS_API_KEY` if you have one. Then:

```bash
docker compose up -d --build
```

Both containers restart automatically after a crash or reboot. The recorder exits on purpose
when the database stays unwritable, so that the restart policy brings it back.

## 3. Check on it

```bash
docker compose ps
```

The recorder shows `healthy` while its heartbeat is fresh. For detail:

```bash
docker compose exec recorder mm status
```

```bash
docker compose logs --since 1h recorder
```

`mm status` prints the gap log by stream and reason. `lost_update` rows are single level
changes the stream never delivered, repaired at the next REST checkpoint; a handful an hour is
normal. `error`, `closed` and `idle_timeout` rows are disconnects with their duration.

## 4. Confirm odds matches

Exact matches are accepted automatically. Anything else waits in a review file inside the
recorder's data volume, and that game is not quoted until you confirm it:

```bash
docker compose cp recorder:/app/data/review/odds_matches.csv odds_matches.csv
```

Set `confirmed` to `yes` on the rows you have checked by hand, then:

```bash
docker compose cp odds_matches.csv recorder:/app/data/review/odds_matches.csv
```

The next discovery refresh (every 15 minutes) stores them.

## 5. Analyse the recordings

PostgreSQL listens on the server's loopback interface only. From a checkout on the server, or
from your own machine through `ssh -L 5432:127.0.0.1:5432 <server>`:

```bash
export MM_DATABASE_URL=postgresql+psycopg://tickbook:<password>@127.0.0.1:5432/tickbook
```

```bash
uv run mm verify
```

```bash
uv run mm backtest
```

```bash
uv run mm report
```

Reports are written to `reports/` in that checkout. Commit only those; never commit `data/` or
a database dump.

## 6. Back up

```bash
docker compose exec postgres pg_dump -U tickbook -Fc tickbook > tickbook-$(date +%F).dump
```

Keep dumps out of Git: trade rows carry public transaction hashes and the dump is large.

## 7. Paper trading

Paper mode is the recorder plus the trading engine in one process: it records everything the
recorder does. Two recorders on one database would store every book and trade twice, so the
second one refuses to start. For the night you want to compare, swap them:

```bash
docker compose stop recorder
```

```bash
docker compose run --rm recorder paper --strategy skew --no-dashboard
```

Paper mode starts once the stopped recorder's heartbeat is 90 seconds old. Stop it with Ctrl-C
after the last game; it prints the run id to pass to `mm report --run`. Then:

```bash
docker compose start recorder
```
