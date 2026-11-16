"""Odds-event to Polymarket-market matching.

Only a match on league, both canonical team names in home/away order, the YES outcome and a start
time inside ``matching.start_window_minutes`` is accepted automatically. Everything else goes to
the review file and stays unconfirmed until a person marks it confirmed there.
"""

from __future__ import annotations

import csv
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from mm.config import REPO_ROOT, Config
from mm.storage.store import Store
from mm.types import iso

FULL = 1.0
PARTIAL = 0.5
TRUE_WORDS = frozenset({"yes", "y", "true", "1"})

REVIEW_COLUMNS = (
    "condition_id",
    "league",
    "outcome_yes",
    "outcome_no",
    "home",
    "away",
    "game_start_utc",
    "odds_event_id",
    "odds_home",
    "odds_away",
    "commence_utc",
    "method",
    "confidence",
    "yes_is_home",
    "confirmed",
)


def norm(name: str) -> str:
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", folded.lower())


class Aliases:
    def __init__(self, table: Mapping[str, Mapping[str, Sequence[str]]]) -> None:
        self._map: dict[str, dict[str, str]] = {}
        for league, teams in table.items():
            lookup: dict[str, str] = {}
            for canonical, others in teams.items():
                key = norm(canonical)
                for name in (canonical, *others):
                    n = norm(name)
                    if lookup.get(n, key) != key:
                        raise ValueError(f"alias {name!r} maps to two {league} teams")
                    lookup[n] = key
            self._map[league.lower()] = lookup

    @classmethod
    def load(cls, path: Path) -> Aliases:
        full = path if path.is_absolute() else REPO_ROOT / path
        with full.open(encoding="utf-8") as fh:
            raw: Any = yaml.safe_load(fh)
        if not isinstance(raw, dict):
            raise ValueError(f"{full} must map league -> team -> aliases")
        return cls(raw)

    def canonical(self, league: str, name: str | None) -> str | None:
        if not name:
            return None
        n = norm(name)
        return self._map.get(league.lower(), {}).get(n, n)


@dataclass(frozen=True, slots=True)
class OddsEventRef:
    event_id: str
    sport: str
    commence: int
    home: str
    away: str


@dataclass(frozen=True, slots=True)
class MarketTeams:
    condition_id: str
    league: str
    home: str | None
    away: str | None
    outcome_yes: str
    outcome_no: str
    game_start: int | None

    @classmethod
    def from_row(cls, row: Mapping[Any, Any]) -> MarketTeams:
        return cls(
            row["condition_id"],
            row["league"],
            row["home"],
            row["away"],
            row["outcome_yes"],
            row["outcome_no"],
            row["game_start_utc"],
        )


@dataclass(frozen=True, slots=True)
class Match:
    market: MarketTeams
    event: OddsEventRef | None
    method: str
    confidence: float
    yes_is_home: bool | None

    @property
    def confirmed(self) -> bool:
        return self.event is not None and self.confidence >= FULL and self.yes_is_home is not None


class Matcher:
    def __init__(self, cfg: Config, aliases: Aliases) -> None:
        self._cfg = cfg
        self._aliases = aliases
        self._window = cfg.matching.start_window_minutes * 60_000
        self._loose = cfg.matching.loose_window_hours * 3_600_000

    def _candidate(self, m: MarketTeams, ev: OddsEventRef) -> Match | None:
        c = self._aliases.canonical
        lg = m.league
        eh, ea = c(lg, ev.home), c(lg, ev.away)
        ch, ca = c(lg, m.home), c(lg, m.away)
        cy, cn = c(lg, m.outcome_yes), c(lg, m.outcome_no)
        if ch and ca and (ch, ca) == (eh, ea):
            order = "teams"
        elif ch and ca and (ch, ca) == (ea, eh):
            order = "teams_swapped"
        elif {cy, cn} == {eh, ea}:
            order = "outcomes_only"
        else:
            return None
        dt = abs(m.game_start - ev.commence) if m.game_start is not None else math.inf
        if dt > self._loose:
            return None
        in_window = dt <= self._window
        yes_is_home = True if cy == eh else False if cy == ea else None
        full = order == "teams" and in_window and yes_is_home is not None
        method = order if in_window else f"{order}_time_loose"
        if yes_is_home is None:
            method += "_yes_unresolved"
        return Match(m, ev, method, FULL if full else PARTIAL, yes_is_home)

    def match(self, market: MarketTeams, events: Iterable[OddsEventRef]) -> Match:
        sport = self._cfg.odds.sport_keys.get(market.league)
        found = [
            cand
            for ev in events
            if ev.sport == sport and (cand := self._candidate(market, ev)) is not None
        ]
        if not found:
            return Match(market, None, "no_odds_event", 0.0, None)

        def gap(x: Match) -> float:
            assert x.event is not None
            start = market.game_start
            return abs(start - x.event.commence) if start is not None else math.inf

        found.sort(key=lambda x: (-x.confidence, gap(x), x.event.event_id if x.event else ""))
        best = found[0]
        if len(found) > 1 and found[1].confidence == best.confidence == FULL:
            return Match(market, best.event, "ambiguous", PARTIAL, best.yes_is_home)
        return best


def mapping_row(m: Match, ts: int) -> dict[str, Any]:
    assert m.event is not None and m.yes_is_home is not None
    return {
        "condition_id": m.market.condition_id,
        "odds_event_id": m.event.event_id,
        "match_method": m.method,
        "confidence": m.confidence,
        "confirmed": m.confirmed,
        "yes_is_home": m.yes_is_home,
        "updated_ts": ts,
    }


def review_row(m: Match) -> dict[str, str]:
    ev = m.event
    mk = m.market
    return {
        "condition_id": mk.condition_id,
        "league": mk.league,
        "outcome_yes": mk.outcome_yes,
        "outcome_no": mk.outcome_no,
        "home": mk.home or "",
        "away": mk.away or "",
        "game_start_utc": iso(mk.game_start) if mk.game_start is not None else "",
        "odds_event_id": ev.event_id if ev else "",
        "odds_home": ev.home if ev else "",
        "odds_away": ev.away if ev else "",
        "commence_utc": iso(ev.commence) if ev else "",
        "method": m.method,
        "confidence": f"{m.confidence:g}",
        "yes_is_home": "" if m.yes_is_home is None else str(m.yes_is_home).lower(),
        "confirmed": "",
    }


def read_confirmations(path: Path, ts: int) -> list[dict[str, Any]]:
    """Rows a person marked ``confirmed`` in the review file, as confirmed mapping rows."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("confirmed", "").strip().lower() not in TRUE_WORDS:
                continue
            event_id = row.get("odds_event_id", "").strip()
            yes_home = row.get("yes_is_home", "").strip().lower()
            if not event_id or yes_home not in {"true", "false"}:
                raise ValueError(
                    f"{path}: confirmed row {row.get('condition_id')} needs odds_event_id and "
                    "yes_is_home true/false"
                )
            out.append(
                {
                    "condition_id": row["condition_id"],
                    "odds_event_id": event_id,
                    "match_method": "manual",
                    "confidence": FULL,
                    "confirmed": True,
                    "yes_is_home": yes_home == "true",
                    "updated_ts": ts,
                }
            )
    return out


def write_review(path: Path, matches: Iterable[Match]) -> int:
    rows = [review_row(m) for m in matches if not m.confirmed]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=REVIEW_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def run_matching(
    cfg: Config,
    store: Store,
    events: Iterable[OddsEventRef],
    ts: int,
    review_path: Path | None = None,
) -> list[Match]:
    path = review_path or cfg.matching.review_file
    for confirmation in read_confirmations(path, ts):
        store.upsert_mapping(confirmation)
    confirmed = {r["condition_id"] for r in store.mappings(confirmed_only=True)}
    matcher = Matcher(cfg, Aliases.load(cfg.matching.aliases_file))
    pool = list(events)
    results: list[Match] = []
    for row in store.markets():
        if row["condition_id"] in confirmed:
            continue
        m = matcher.match(MarketTeams.from_row(row), pool)
        if m.event is not None and m.yes_is_home is not None:
            store.upsert_mapping(mapping_row(m, ts))
        results.append(m)
    write_review(path, results)
    return results
