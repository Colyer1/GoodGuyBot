# cogs/sports.py
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta, timezone

import disnake
from disnake.ext import commands

from utils.sportsdata import (
    SportsDataClient,
    normalize_date,
    to_et,
    SPORT_MAP,
    ET,  # <-- use ET as default tz for naive timestamps
)

SPORT_CHOICES = list(SPORT_MAP.keys())


# ---------- Time formatting helpers ----------

def parse_game_dt(dt_str: Optional[str]) -> Optional[datetime]:
    """
    Parse SportsDataIO date/time fields:
    - If the string has a timezone (or 'Z'), parse normally and convert to ET.
    - If it's naive (no timezone), TREAT IT AS ET (Sports leagues often provide local ET-like strings).
    """
    if not dt_str:
        return None
    s = dt_str
    try:
        if s.endswith("Z") or s[-6] in ("+", "-"):
            # has tz or Z
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return to_et(dt)
        # fallback attempts
        dt = datetime.fromisoformat(s[:19])  # naive
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET)  # <-- assume ET, NOT UTC
        return to_et(dt)
    except Exception:
        try:
            dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ET)
            return to_et(dt)
        except Exception:
            return None


def fmt_time(dt_str: Optional[str]) -> str:
    dt = parse_game_dt(dt_str)
    if not dt:
        return "—"
    unix = int(dt.timestamp())
    return f"<t:{unix}:f> (<t:{unix}:R>)"


# ---------- Score line helpers ----------

def pick_name(g: Dict[str, Any], side: str) -> str:
    for k in (f"{side}Team", f"{side}TeamName", f"{side}TeamKey", f"{side}GlobalTeamID"):
        v = g.get(k)
        if v:
            return str(v)
    return str(g.get(f"{side}TeamID", side))


def in_progress_label(g: Dict[str, Any], sport: str) -> str:
    status = (g.get("Status") or g.get("GameStatus") or "").lower()
    if sport == "mlb":
        inning = g.get("Inning")
        half = g.get("InningHalf") or ""
        outs = g.get("Outs")
        balls = g.get("Balls")
        strikes = g.get("Strikes")
        parts = []
        if inning:
            parts.append(f"{half} {inning}".strip())
        if outs is not None:
            parts.append(f"{outs} out")
        if balls is not None and strikes is not None:
            parts.append(f"count {balls}-{strikes}")
        return " • ".join([p for p in parts if p]) or status.title() or "Live"
    # Period/quarter/clock for other sports
    per = g.get("Quarter") or g.get("Period")
    clk = g.get("TimeRemaining") or g.get("Clock")
    if per:
        return f"Q{per}{(' ' + clk) if clk else ''}"
    if clk:
        return str(clk)
    return status.title() or "Live"


def extract_score(g: Dict[str, Any], sport: str) -> Optional[str]:
    pairs = [
        ("AwayTeamScore", "HomeTeamScore"),
        ("AwayScore", "HomeScore"),
        ("AwayPoints", "HomePoints"),
        ("AwayGoals", "HomeGoals"),
        ("AwayRuns", "HomeRuns"),
        ("AwayTeamRuns", "HomeTeamRuns"),
    ]
    for ak, hk in pairs:
        a = g.get(ak)
        h = g.get(hk)
        if a is not None and h is not None:
            try:
                return f"{int(a)}–{int(h)}"
            except Exception:
                return f"{a}–{h}"
    return None


def game_has_team(g: dict, tid: object) -> bool:
    if tid is None:
        return False
    tid_s = str(tid).lower()
    id_fields = ["HomeTeamID", "AwayTeamID", "HomeGlobalTeamID", "AwayGlobalTeamID"]
    if any(tid_s == str(g.get(f, "")).lower() for f in id_fields):
        return True
    name_fields = ["HomeTeam", "AwayTeam", "HomeTeamKey", "AwayTeamKey", "HomeTeamName", "AwayTeamName"]
    if any(tid_s == str(g.get(f, "")).lower() for f in name_fields):
        return True
    return False


# ---------- Odds formatting helpers ----------

def sgn(n: Optional[float]) -> str:
    if n is None:
        return "—"
    try:
        n = float(n)
    except Exception:
        return str(n)
    if n > 0:
        return f"+{int(n) if n.is_integer() else n}"
    return f"{int(n) if n.is_integer() else n}"


def fav_from_lines(home: Optional[float], away: Optional[float],
                   spread_home: Optional[float], spread_away: Optional[float],
                   home_name: str, away_name: str) -> Optional[str]:
    # Prefer moneyline: more negative is favorite
    if home is not None and away is not None:
        try:
            h = float(home); a = float(away)
            return home_name if h < a else away_name
        except Exception:
            pass
    # Fallback: spread (negative favored)
    for val, name in [(spread_home, home_name), (spread_away, away_name)]:
        try:
            if val is not None and float(val) < 0:
                return name
        except Exception:
            pass
    return None


# =========================
# Cog
# =========================

class Sports(commands.Cog):
    """Scores, Schedules, Standings, and Odds via SportsDataIO."""
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot
        self.client = SportsDataClient()

    def cog_unload(self):
        self.bot.loop.create_task(self.client.close())

    # ---------- /scores ----------

    @commands.slash_command(dm_permission=False)
    @commands.guild_only()
    async def scores(
        self,
        inter: disnake.ApplicationCommandInteraction,
        sport: str = commands.Param(choices=SPORT_CHOICES, description="League"),
        date: Optional[str] = commands.Param(default=None, description="YYYY-MM-DD or today/tomorrow"),
        team: Optional[str] = commands.Param(default=None, description="Filter by team"),
    ):
        """Get Scores for a Sport"""
        await inter.response.defer()
        date_iso = normalize_date(date)
        games = await self.client.games_by_date(sport, date_iso)
        if not games:
            return await inter.edit_original_response(f"No games for **{sport.upper()}** on **{date_iso}**.")

        # sort by kickoff/first pitch
        def start_key(g: Dict[str, Any]):
            when = g.get("DateTime") or g.get("Day") or g.get("DateTimeUTC")
            dt = parse_game_dt(when)
            return dt or datetime.min.replace(tzinfo=timezone.utc)

        games.sort(key=start_key)

        if team:
            teams = await self.client.get_teams(sport)
            t = self.client.match_team(teams, team)
            if not t:
                return await inter.edit_original_response(f"Couldn’t find team '{team}' in {sport.upper()}.")
            tid = t.get("TeamID") or t.get("GlobalTeamID") or t.get("Key") or t.get("Name")
            games = [g for g in games if game_has_team(g, tid)]
            if not games:
                return await inter.edit_original_response(f"No games for **{team}** on **{date_iso}**.")

        rows: List[str] = []
        for g in games[:25]:
            away = pick_name(g, "Away")
            home = pick_name(g, "Home")
            status_raw = (g.get("Status") or g.get("GameStatus") or "").lower()
            score_str = extract_score(g, sport)

            if status_raw in ("inprogress", "in progress", "in-play", "live"):
                s_label = in_progress_label(g, sport)
                line = f"**{away} @ {home}** — {score_str if score_str else ''}  {('• ' + s_label) if s_label else ''}".strip()
            elif status_raw in ("final", "complete", "closed"):
                line = f"**{away} @ {home}** — Final{(' ' + score_str) if score_str else ''}"
            else:
                when = g.get("DateTime") or g.get("Day") or g.get("DateTimeUTC")
                line = f"**{away} @ {home}** — {fmt_time(when)}"
            rows.append(line)

        embed = disnake.Embed(
            title=f"📊 {sport.upper()} — Scores ({date_iso})",
            description="\n".join(rows) if rows else "—",
            color=disnake.Color.blurple()
        )
        embed.set_footer(text="Source: SportsDataIO")
        await inter.edit_original_response(embed=embed)

    # ---------- /schedule ----------

    @commands.slash_command(dm_permission=False)
    @commands.guild_only()
    async def schedule(
        self,
        inter: disnake.ApplicationCommandInteraction,
        sport: str = commands.Param(choices=SPORT_CHOICES, description="League"),
        range: str = commands.Param(default="+48h", choices=["today", "+24h", "+48h", "+7d"], description="Window"),
        team: Optional[str] = commands.Param(default=None, description="Filter by team"),
    ):
        """"Get the Schedule for a Sport"""
        await inter.response.defer()
        now = datetime.now(timezone.utc)
        if range == "today":
            start, end = now, now + timedelta(hours=24)
        elif range == "+24h":
            start, end = now, now + timedelta(hours=24)
        elif range == "+48h":
            start, end = now, now + timedelta(hours=48)
        else:
            start, end = now, now + timedelta(days=7)

        data = await self.client.schedule_window(sport, start.isoformat(), end.isoformat())
        if not data:
            return await inter.edit_original_response(f"No upcoming games for **{sport.upper()}** in {range}.")

        if team:
            teams = await self.client.get_teams(sport)
            t = self.client.match_team(teams, team)
            if not t:
                return await inter.edit_original_response(f"Couldn’t find team '{team}' in {sport.upper()}.")
            tid = t.get("TeamID") or t.get("GlobalTeamID") or t.get("Key") or t.get("Name")
            data = [g for g in data if game_has_team(g, tid)]
            if not data:
                return await inter.edit_original_response("No matching games in this window.")

        def start_key(g: Dict[str, Any]):
            when = g.get("DateTime") or g.get("Day") or g.get("DateTimeUTC")
            dt = parse_game_dt(when)
            return dt or datetime.max.replace(tzinfo=timezone.utc)

        rows: List[str] = []
        for g in sorted(data, key=start_key)[:25]:
            away = pick_name(g, "Away")
            home = pick_name(g, "Home")
            when = g.get("DateTime") or g.get("Day") or g.get("DateTimeUTC")
            rows.append(f"**{away} @ {home}** — {fmt_time(when)}")

        embed = disnake.Embed(
            title=f"🗓️ {sport.upper()} — Schedule ({range})",
            description="\n".join(rows) if rows else "—",
            color=disnake.Color.green()
        )
        embed.set_footer(text="Source: SportsDataIO")
        await inter.edit_original_response(embed=embed)

    # ---------- /standings ----------

    @commands.slash_command(dm_permission=False)
    @commands.guild_only()
    async def standings(
        self,
        inter: disnake.ApplicationCommandInteraction,
        sport: str = commands.Param(choices=SPORT_CHOICES, description="League"),
    ):
        """"Get the Standings for a Sport"""
        await inter.response.defer()
        data = await self.client.standings(sport)
        if not data:
            return await inter.edit_original_response(
                "Standings not available right now for this league (endpoint varies by season/plan)."
            )

        def win_pct(row: Dict[str, Any]) -> float:
            for k in ("Percentage", "WinPercentage", "PCT"):
                v = row.get(k)
                if v is not None:
                    try:
                        return float(v)
                    except Exception:
                        pass
            w = row.get("Wins") or row.get("Win") or row.get("W")
            l = row.get("Losses") or row.get("Loss") or row.get("L")
            try:
                w = float(w); l = float(l)
                return w / max(1.0, (w + l))
            except Exception:
                return 0.0

        top = sorted(data, key=win_pct, reverse=True)[:10]
        lines: List[str] = []
        for r in top:
            name = r.get("Name") or r.get("Team") or r.get("Key") or "—"
            w = r.get("Wins", r.get("W", "—"))
            l = r.get("Losses", r.get("L", "—"))
            pct = r.get("Percentage") or r.get("PCT") or f"{win_pct(r):.3f}"
            extra = r.get("Division") or r.get("DivisionName") or r.get("Conference") or ""
            lines.append(f"**{name}** — {w}-{l}  (Win%: {pct}){(' • ' + extra) if extra else ''}")

        embed = disnake.Embed(
            title=f"🏆 {sport.upper()} — Standings (Top 10)",
            description="\n".join(lines) if lines else "—",
            color=disnake.Color.orange()
        )
        embed.set_footer(text="Source: SportsDataIO")
        await inter.edit_original_response(embed=embed)

    # ---------- /odds (clean layout) ----------

    @commands.slash_command(dm_permission=False)
    @commands.guild_only()
    async def odds(
        self,
        inter: disnake.ApplicationCommandInteraction,
        sport: str = commands.Param(choices=SPORT_CHOICES, description="League"),
        date: Optional[str] = commands.Param(default=None, description="YYYY-MM-DD or 'today'"),
        team: Optional[str] = commands.Param(default=None, description="Filter by team"),
        book: Optional[str] = commands.Param(default=None, description="Sportsbook name (optional)"),
        books_to_show: int = commands.Param(default=2, ge=1, le=5, description="Max books per game"),
    ):
        """"Get the Odds for a Sport"""
        await inter.response.defer()

        date_iso = normalize_date(date)
        data = await self.client.odds_by_date(sport, date_iso)
        if isinstance(data, dict) and data.get("__odds_unavailable__"):
            return await inter.edit_original_response(
                f"Odds are not enabled/available for **{sport.upper()}** (HTTP {data.get('status')})."
            )
        if not data:
            return await inter.edit_original_response(f"No odds data for **{sport.upper()}** on **{date_iso}**.")

        # Filter by team if requested
        if team:
            q = team.lower()
            data = [g for g in data if q in (g.get("HomeTeam","") or "").lower() or q in (g.get("AwayTeam","") or "").lower()]
            if not data:
                return await inter.edit_original_response(f"No odds entries for **{team}** on **{date_iso}**.")

        # Sort games by start
        def start_key(g: Dict[str, Any]):
            when = g.get("DateTime") or g.get("Day") or g.get("DateTimeUTC")
            dt = parse_game_dt(when)
            return dt or datetime.max.replace(tzinfo=timezone.utc)

        data.sort(key=start_key)

        rows: List[str] = []
        for g in data[:15]:
            home = g.get("HomeTeam") or g.get("HomeTeamName") or "Home"
            away = g.get("AwayTeam") or g.get("AwayTeamName") or "Away"

            lines: List[str] = []
            pool = (g.get("PregameOdds") or g.get("Odds") or [])
            shown = 0

            for o in pool:
                sb = o.get("Sportsbook") or o.get("SportsbookUrl") or "Book"
                if book and sb.lower() != book.lower():
                    continue

                ml_home = o.get("HomeMoneyLine")
                ml_away = o.get("AwayMoneyLine")
                ps_home = o.get("PointSpreadHome")
                ps_away = o.get("PointSpreadAway")
                total = o.get("OverUnder")

                fav = fav_from_lines(ml_home, ml_away, ps_home, ps_away, home, away)
                fav_frag = f"\n Fav: **{fav}**" if fav else ""

                block = (
                    f"• **{sb}**\n"
                    f" Moneyline — {away} **{sgn(ml_away)}**, {home} **{sgn(ml_home)}**"
                )
                if ps_home is not None or ps_away is not None:
                    block += f"\n Spread — {away} {sgn(ps_away)}, {home} {sgn(ps_home)}"
                if total is not None:
                    block += f"\n Total — **{total}**"
                block += fav_frag

                lines.append(block)
                shown += 1
                if not book and shown >= books_to_show:
                    break

            if lines:
                rows.append(f"**{away} @ {home}**\n" + "\n".join(lines))

        if not rows:
            return await inter.edit_original_response("No odds lines matched your filter.")

        embed = disnake.Embed(
            title=f"📉 {sport.upper()} — Odds ({date_iso})",
            description="\n\n".join(rows),
            color=disnake.Color.purple()
        )
        embed.set_footer(text="Source: SportsDataIO")
        await inter.edit_original_response(embed=embed)


def setup(bot: commands.InteractionBot):
    bot.add_cog(Sports(bot))
