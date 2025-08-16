# utils/sportsdata.py
import os
import time
import asyncio
from typing import Any, Dict, Optional, Tuple, List

import aiohttp
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo  # py>=3.9
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = timezone(timedelta(hours=-5))  # coarse fallback; DST not handled

SPORTSDATA_BASE = "https://api.sportsdata.io"

SPORT_MAP: Dict[str, Dict[str, str]] = {
    "nfl":   {"league": "nfl",   "teams_ep": "Teams"},
    "nba":   {"league": "nba",   "teams_ep": "Teams"},
    "mlb":   {"league": "mlb",   "teams_ep": "Teams"},
    "nhl":   {"league": "nhl",   "teams_ep": "Teams"},
    "ncaaf": {"league": "cfb",   "teams_ep": "Teams"},
    "ncaab": {"league": "cbb",   "teams_ep": "Teams"},
    "wnba":  {"league": "wnba",  "teams_ep": "Teams"},
    "mls":        {"league": "soccer-mls",               "teams_ep": "Teams"},
    "epl":        {"league": "soccer-premier-league",    "teams_ep": "Teams"},
    "laliga":     {"league": "soccer-la-liga",           "teams_ep": "Teams"},
    "seriea":     {"league": "soccer-serie-a",           "teams_ep": "Teams"},
    "bundesliga": {"league": "soccer-bundesliga",        "teams_ep": "Teams"},
    "ucl":        {"league": "soccer-uefa-champions-league", "teams_ep": "Teams"},
    "f1":    {"league": "f1",    "teams_ep": "Teams"},
    "nascar":{"league": "nascar","teams_ep": "Teams"},
    "golf":  {"league": "golf",  "teams_ep": "Teams"},
    "tennis":{"league": "tennis","teams_ep": "Competitors"},
    "mma":   {"league": "mma",   "teams_ep": "Fighters"},
    "boxing":{"league": "boxing","teams_ep": "Fighters"},
}

TTL_SCORES   = 30
TTL_SCHEDULE = 300
TTL_STAND    = 1800
TTL_ODDS     = 30
TTL_TEAMS    = 86400

class TTLCache:
    def __init__(self):
        self._store: Dict[str, Tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        item = self._store.get(key)
        if not item:
            return None
        exp, val = item
        if time.time() > exp:
            self._store.pop(key, None)
            return None
        return val

    def set(self, key: str, val: Any, ttl: int):
        self._store[key] = (time.time() + ttl, val)

cache = TTLCache()

def to_et(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET)

def normalize_date(date_str: Optional[str]) -> str:
    if not date_str or date_str.lower() == "today":
        return datetime.now(ET).strftime("%Y-%m-%d")
    if date_str.lower() in ("tomorrow", "tmr", "tommorow", "tomorow"):
        d = datetime.now(ET) + timedelta(days=1)
        return d.strftime("%Y-%m-%d")
    return date_str

def iso_to_sportsdata_date(iso_date: str) -> str:
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d")
        return d.strftime("%Y-%b-%d").upper()
    except Exception:
        return iso_date

class SportsDataClient:
    def __init__(self, api_key: Optional[str] = None, session: Optional[aiohttp.ClientSession] = None):
        self.api_key = api_key or os.getenv("SPORTSDATAIO_KEY")
        if not self.api_key:
            raise RuntimeError("Missing SPORTSDATAIO_KEY in environment.")
        self._session = session

    async def _get_sess(self) -> aiohttp.ClientSession:
        if self._session and not self._session.closed:
            return self._session
        self._session = aiohttp.ClientSession(
            headers={"Ocp-Apim-Subscription-Key": self.api_key},
            timeout=aiohttp.ClientTimeout(total=20)
        )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str, ttl: int, key: str) -> Any:
        cached = cache.get(key)
        if cached is not None:
            return cached
        sess = await self._get_sess()
        url = f"{SPORTSDATA_BASE}{path}"
        async with sess.get(url) as resp:
            if resp.status == 429:
                await asyncio.sleep(1.0)
            resp.raise_for_status()
            data = await resp.json()
            cache.set(key, data, ttl)
            return data

    def _league(self, sport: str) -> str:
        m = SPORT_MAP.get(sport)
        if not m:
            raise ValueError(f"Unsupported sport '{sport}'")
        return m["league"]

    async def get_teams(self, sport: str) -> List[Dict[str, Any]]:
        league = self._league(sport)
        ep = SPORT_MAP[sport]["teams_ep"]
        path = f"/v3/{league}/scores/json/{ep}"
        return await self._get(path, TTL_TEAMS, f"teams:{league}")

    async def games_by_date(self, sport: str, date_iso: str) -> Any:
        league = self._league(sport)
        sd_date = iso_to_sportsdata_date(date_iso)
        for endpoint in ("GamesByDate", "ScoresByDate"):
            path = f"/v3/{league}/scores/json/{endpoint}/{sd_date}"
            try:
                return await self._get(path, TTL_SCORES, f"scores:{league}:{sd_date}:{endpoint}")
            except aiohttp.ClientResponseError as e:
                if e.status == 404:
                    continue
                raise
        return []

    async def schedule_window(self, sport: str, start_iso: str, end_iso: str) -> Any:
        start = datetime.fromisoformat(start_iso)
        end = datetime.fromisoformat(end_iso)
        days = max(0, min((end.date() - start.date()).days, 14))
        out: List[Dict[str, Any]] = []
        for i in range(days + 1):
            d_iso = (start + timedelta(days=i)).astimezone(ET).strftime("%Y-%m-%d")
            day_games = await self.games_by_date(sport, d_iso)
            if day_games:
                out.extend(day_games)
        return out

    async def standings(self, sport: str) -> Any:
        """
        Try several variants because SportsDataIO differs per league.
        """
        league = self._league(sport)

        # 1) Direct "Standings"
        try:
            return await self._get(f"/v3/{league}/scores/json/Standings", TTL_STAND, f"stand:{league}:current")
        except aiohttp.ClientResponseError as e:
            if e.status not in (400, 404):
                raise

        # 2) Season-based fallbacks: try current year and (current-1)
        year_now = datetime.now(ET).year
        candidates = [year_now, year_now - 1]

        # Some leagues want suffixes like 2025REG
        suffixes = ["", "REG", "POST", "PRE"]

        endpoints = [
            "/v3/{lg}/scores/json/StandingsBySeason/{season}",
            "/v3/{lg}/scores/json/Standings/{season}",
            "/v3/{lg}/scores/json/StandingsBasic/{season}",
        ]

        for yr in candidates:
            for suf in suffixes:
                season_str = f"{yr}{suf}" if suf else f"{yr}"
                for ep in endpoints:
                    path = ep.format(lg=league, season=season_str)
                    try:
                        data = await self._get(path, TTL_STAND, f"stand:{league}:{season_str}:{ep}")
                        if isinstance(data, list) and data:
                            return data
                    except aiohttp.ClientResponseError as e:
                        if e.status in (400, 404):
                            continue
                        raise
        return []

    async def odds_by_date(self, sport: str, date_iso: str) -> Any:
        league = self._league(sport)
        sd_date = iso_to_sportsdata_date(date_iso)
        path = f"/v3/{league}/odds/json/GameOddsByDate/{sd_date}"
        try:
            return await self._get(path, TTL_ODDS, f"odds:{league}:{sd_date}")
        except aiohttp.ClientResponseError as e:
            if e.status in (403, 404):
                return {"__odds_unavailable__": True, "status": e.status}
            raise

    @staticmethod
    def match_team(teams: List[Dict[str, Any]], query: str) -> Optional[Dict[str, Any]]:
        q = query.strip().lower()
        keys = ("Name", "FullName", "City", "Team", "Nickname", "Key")
        for t in teams:
            for k in keys:
                v = str(t.get(k, "")).lower()
                if v and (q == v or q in v):
                    return t
        return None
