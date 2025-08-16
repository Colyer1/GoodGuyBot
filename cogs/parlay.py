# cogs/parlay.py
from typing import List, Optional, Tuple
import asyncio
import uuid
import time
import os
import sqlite3
import disnake
from disnake.ext import commands

from utils.parlay_research import run_deep_research, ParlayResult
from utils.sportsdata import SPORT_MAP, normalize_date, ET  # ET tz for dates

# ---------- Config ----------
SPORT_ALIASES = {"ufc": "mma"}  # allow "ufc", route to "mma"
SPORT_CHOICES = sorted(set(list(SPORT_MAP.keys()) + list(SPORT_ALIASES.keys())))

PREMIUM_ROLE_ID = 1406008056516444211          # ParlayAI+ role id
DAILY_MAX_PARLAYS = 3                           # per user per day (ET)
DB_PATH = os.path.join("data", "limits.db")

# ---------- Tunables ----------
HEARTBEAT_SECONDS = 120         # heartbeat cadence for status embed
JOB_TIMEOUT_SECONDS = 14 * 60   # hard cap so you never wait forever

# Visible spinner for heartbeats
SPINNER = ["⏳", "🕐", "🕑", "🕒", "🕓", "🕔", "🕕", "🕖", "🕗", "🕘", "🕙", "🕚"]

# ---------- SQLite helpers ----------
def _ensure_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS parlay_usage (
            job_id   TEXT PRIMARY KEY,
            user_id  INTEGER NOT NULL,
            et_date  TEXT NOT NULL,
            status   TEXT NOT NULL CHECK(status IN ('pending','success','failed'))
        );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_user_date ON parlay_usage(user_id, et_date);")
        conn.commit()

def _today_et_str() -> str:
    # ET date string (YYYY-MM-DD)
    from datetime import datetime
    return datetime.now(ET).strftime("%Y-%m-%d")

def _create_pending_job(job_id: str, user_id: int, et_date: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO parlay_usage (job_id, user_id, et_date, status) VALUES (?, ?, ?, 'pending');",
            (job_id, user_id, et_date)
        )
        conn.commit()

def _set_job_status(job_id: str, status: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE parlay_usage SET status=? WHERE job_id=?;",
            (status, job_id)
        )
        conn.commit()

def _count_success_today(user_id: int, et_date: str) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT COUNT(1) FROM parlay_usage WHERE user_id=? AND et_date=? AND status='success';",
            (user_id, et_date)
        )
        row = cur.fetchone()
        return int(row[0] if row and row[0] is not None else 0)

def _remaining_today(user_id: int) -> Tuple[int, str]:
    today = _today_et_str()
    used = _count_success_today(user_id, today)
    remaining = max(0, DAILY_MAX_PARLAYS - used)
    return remaining, today

# ---------- embed helpers ----------
def _chunk(text: str, limit: int = 1010) -> List[str]:
    parts: List[str] = []
    s = (text or "").strip()
    while len(s) > limit:
        cut = s.rfind("\n", 0, limit)
        if cut == -1:
            cut = limit
        parts.append(s[:cut].rstrip())
        s = s[cut:].lstrip()
    if s:
        parts.append(s)
    return parts

def _sources_block(urls: list, max_items: int = 10) -> str:
    out = []
    for i, u in enumerate(urls[:max_items], start=1):
        if not u:
            continue
        out.append(f"{i}. <{str(u).strip()}>")
    return "\n".join(out) if out else "—"

def _status_embed(
    job_id: str,
    sport: str,
    legs: int,
    date_iso: str,
    query: str,
    status: str,
    elapsed_s: int = 0,
    started_unix: Optional[int] = None,
    last_hb_unix: Optional[int] = None,
    spinner_idx: int = 0
) -> disnake.Embed:
    color = (
        disnake.Color.yellow() if status in ("Running", "Waiting", "Processing")
        else (disnake.Color.green() if status == "Complete" else disnake.Color.red())
    )
    mm, ss = divmod(max(0, elapsed_s), 60)
    timer = f"{mm:01d}m {ss:02d}s"
    spin = SPINNER[spinner_idx % len(SPINNER)] if status in ("Running", "Waiting", "Processing") else ("🟢" if status == "Complete" else "🔴")

    emb = disnake.Embed(
        title=f"Deep Research Parlay • #{job_id}",
        description=(
            f"**League:** {sport.upper()} • **Legs:** {legs} • **Date:** {date_iso}\n"
            f"**Focus:** {query}"
        ),
        color=color
    )
    emb.add_field(name="Status", value=f"{spin} {status} • {timer}", inline=False)

    if started_unix:
        emb.add_field(name="Started", value=f"<t:{started_unix}:f> (<t:{started_unix}:R>)", inline=True)
    if last_hb_unix:
        emb.add_field(name="Last heartbeat", value=f"<t:{last_hb_unix}:T>", inline=True)

    emb.set_footer(text="Research only — not betting advice. If you need help, call/text 1-800-GAMBLER (US).")
    return emb

def _result_embed(title: str, result: Optional[ParlayResult], err: Optional[str]) -> disnake.Embed:
    if err:
        emb = disnake.Embed(title=title, color=disnake.Color.red())
        for chunk in _chunk(err):
            emb.add_field(name="Error", value=chunk, inline=False)
        emb.set_footer(text="Research only — not betting advice. If you need help, call/text 1-800-GAMBLER (US).")
        return emb

    assert result is not None
    emb = disnake.Embed(title=title, color=disnake.Color.blurple())

    if result.parlay:
        for i, leg in enumerate(result.parlay, start=1):
            market = leg.market.title()
            selection = leg.selection
            books = ", ".join(leg.book_examples[:3]) if leg.book_examples else "—"
            conf = leg.confidence.upper()
            value = (
                f"**Selection:** {selection}\n"
                f"**Market:** {market}\n"
                f"**Books:** {books}\n"
                f"**Confidence:** {conf}"
            )
            emb.add_field(name=f"Leg {i}", value=value, inline=False)
    else:
        emb.add_field(name="Parlay", value="No legs returned.", inline=False)

    if result.rationales:
        joined = "• " + "\n• ".join([r for r in result.rationales if r.strip()])
        for idx, chunk in enumerate(_chunk(joined), start=1):
            emb.add_field(name="Rationale" if idx == 1 else "Rationale (cont.)", value=chunk, inline=False)

    if result.risks:
        for idx, chunk in enumerate(_chunk(result.risks), start=1):
            emb.add_field(name="Risks" if idx == 1 else "Risks (cont.)", value=chunk, inline=False)

    emb.add_field(name="Sources", value=_sources_block(result.sources), inline=False)
    emb.set_footer(text="Research only — not betting advice. If you need help, call/text 1-800-GAMBLER (US).")
    return emb

# ---------- Cog ----------
class Parlay(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot
        _ensure_db()

    @commands.slash_command(
        description="Deep-research a value parlay (background job with live status; DM or channel delivery).",
        dm_permission=False
    )
    @commands.guild_only()
    async def parlay(
        self,
        inter: disnake.AppCmdInter,
        sport: str = commands.Param(choices=SPORT_CHOICES, description="League to target"),
        legs: int = commands.Param(default=3, ge=2, le=6, description="How many legs"),
        query: str = commands.Param(description="What exactly do you want to research? (teams, props, angles)"),
        date: Optional[str] = commands.Param(default=None, description="YYYY-MM-DD (defaults to today)"),
        region: Optional[str] = commands.Param(default=None, description="e.g., KY, NJ, ON; affects book availability"),
        constraints: Optional[str] = commands.Param(default=None, description="e.g., no alt lines, avoid -200+ juice"),
        deliver: str = commands.Param(default="dm", choices=["dm", "channel"], description="Where to send the result"),
    ):
        """
        Only Admins or members with the ParlayAI+ role can run this.
        Limit: 3 successful runs per user per day (ET). Failed/timeouts don't count.
        """
        # ---------- Access control: Admin OR role ----------
        is_admin = inter.author.guild_permissions.administrator
        has_role = any(r.id == PREMIUM_ROLE_ID for r in getattr(inter.author, "roles", []))
        if not (is_admin or has_role):
            await inter.response.send_message(
                "⛔ You need **Administrator** or the **ParlayAI+** role to use this command.",
                ephemeral=True
            )
            return

        # ---------- Daily limit (only counts successes) ----------
        remaining, today = _remaining_today(inter.author.id)
        if remaining <= 0:
            await inter.response.send_message(
                f"⏱️ Daily limit reached. You’ve already completed **{DAILY_MAX_PARLAYS}** parlays today (ET). "
                f"Try again tomorrow.",
                ephemeral=True
            )
            return

        # Ephemeral unless sending to DMs
        await inter.response.defer(ephemeral=(deliver != "dm"))

        # Map aliases (e.g., 'ufc' -> 'mma')
        sport_key = SPORT_ALIASES.get(sport.lower(), sport.lower())

        job_id = uuid.uuid4().hex[:8].upper()
        date_iso = normalize_date(date)
        start_monotonic = time.monotonic()
        started_unix = int(time.time())
        spinner_idx = 0
        current_status = "Waiting"

        # Create pending record (doesn't count toward limit until success)
        _create_pending_job(job_id, inter.author.id, today)

        # 1) Initial status
        status_emb = _status_embed(
            job_id, sport_key, legs, date_iso, query,
            status=current_status, elapsed_s=0,
            started_unix=started_unix, last_hb_unix=started_unix, spinner_idx=spinner_idx
        )
        status_msg: Optional[disnake.Message] = None
        if deliver == "channel":
            status_msg = await inter.followup.send(
                embed=status_emb,
                allowed_mentions=disnake.AllowedMentions.none()
            )
        else:
            await inter.edit_original_response(embed=status_emb)

        # LOG
        print(
            f"[DeepResearch] START job #{job_id} | user={inter.author.id} | sport={sport_key} legs={legs} date={date_iso} "
            f"deliver={deliver} | focus={query!r}"
        )

        # Update "Waiting"
        spinner_idx += 1
        try:
            emb = _status_embed(
                job_id, sport_key, legs, date_iso, query, "Waiting",
                elapsed_s=int(time.monotonic()-start_monotonic),
                started_unix=started_unix, last_hb_unix=int(time.time()), spinner_idx=spinner_idx
            )
            if deliver == "channel" and isinstance(status_msg, disnake.Message):
                await status_msg.edit(embed=emb)
            else:
                await inter.edit_original_response(embed=emb)
        except Exception:
            pass

        # 2) Kick off the worker
        worker_task = asyncio.create_task(asyncio.to_thread(
            run_deep_research,
            user_query=query,
            sport=sport_key,
            legs=legs,
            date_iso=date_iso,
            region=region,
            constraints=constraints,
        ))

        # 3) Heartbeat loop + timeout
        async def heartbeat_updater():
            nonlocal spinner_idx, current_status
            while not worker_task.done():
                spinner_idx += 1
                current_status = "Running"
                elapsed = int(time.monotonic() - start_monotonic)
                hb_unix = int(time.time())
                emb = _status_embed(
                    job_id, sport_key, legs, date_iso, query, current_status,
                    elapsed_s=elapsed, started_unix=started_unix, last_hb_unix=hb_unix, spinner_idx=spinner_idx
                )
                if deliver == "channel" and isinstance(status_msg, disnake.Message):
                    try:
                        await status_msg.edit(embed=emb)
                    except Exception:
                        pass
                else:
                    try:
                        await inter.edit_original_response(embed=emb)
                    except Exception:
                        pass
                await asyncio.sleep(HEARTBEAT_SECONDS)

        heartbeat_task = asyncio.create_task(heartbeat_updater())

        try:
            await asyncio.wait_for(worker_task, timeout=JOB_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            worker_task.cancel()
            print(f"[DeepResearch] TIMEOUT job #{job_id} after {JOB_TIMEOUT_SECONDS}s")
            _set_job_status(job_id, "failed")  # does NOT count against daily limit

            title = f"Deep Research Parlay • #{job_id} • {sport_key.upper()} • {legs} legs • {date_iso}"
            timeout_embed = _result_embed(title, None, "Timed out waiting for research (took too long). Try narrowing the query or choosing fewer legs.")
            done_status = _status_embed(
                job_id, sport_key, legs, date_iso, query, status="Failed",
                elapsed_s=JOB_TIMEOUT_SECONDS, started_unix=started_unix, last_hb_unix=int(time.time()), spinner_idx=spinner_idx
            )
            if deliver == "channel" and isinstance(status_msg, disnake.Message):
                try:
                    await status_msg.edit(embed=done_status)
                except Exception:
                    pass
                try:
                    await inter.followup.send(embed=timeout_embed, allowed_mentions=disnake.AllowedMentions.none())
                except Exception:
                    pass
            else:
                try:
                    await inter.edit_original_response(embed=done_status)
                except Exception:
                    pass
                try:
                    await inter.author.send(embed=timeout_embed)
                except Exception:
                    try:
                        await inter.followup.send(embed=timeout_embed, allowed_mentions=disnake.AllowedMentions.none())
                    except Exception:
                        pass
            return
        finally:
            heartbeat_task.cancel()

        # 4) Processing + finalize
        spinner_idx += 1
        processing_emb = _status_embed(
            job_id, sport_key, legs, date_iso, query, "Processing",
            elapsed_s=int(time.monotonic()-start_monotonic),
            started_unix=started_unix, last_hb_unix=int(time.time()), spinner_idx=spinner_idx
        )
        try:
            if deliver == "channel" and isinstance(status_msg, disnake.Message):
                await status_msg.edit(embed=processing_emb)
            else:
                await inter.edit_original_response(embed=processing_emb)
        except Exception:
            pass

        result, err = worker_task.result()
        elapsed = int(time.monotonic() - start_monotonic)
        if err:
            print(f"[DeepResearch] END   job #{job_id} | ERROR after {elapsed}s -> {err}")
            _set_job_status(job_id, "failed")  # does NOT count against daily limit
        else:
            legs_returned = len(result.parlay) if result else 0
            print(f"[DeepResearch] END   job #{job_id} | OK after {elapsed}s | legs_returned={legs_returned}")
            _set_job_status(job_id, "success")  # counts toward daily limit

        title = f"Deep Research Parlay • #{job_id} • {sport_key.upper()} • {legs} legs • {date_iso}"
        final_emb = _result_embed(title, result, err)

        done_status = _status_embed(
            job_id, sport_key, legs, date_iso, query,
            status=("Complete" if not err else "Failed"),
            elapsed_s=elapsed, started_unix=started_unix, last_hb_unix=int(time.time()), spinner_idx=spinner_idx
        )

        if deliver == "dm":
            delivered = False
            try:
                await inter.author.send(embed=final_emb)
                delivered = True
            except Exception:
                delivered = False

            try:
                await inter.edit_original_response(embed=done_status)
                if not delivered:
                    await inter.followup.send(
                        content="⚠️ I couldn't DM you the result (DMs closed?). Posting here instead.",
                        embed=final_emb,
                        allowed_mentions=disnake.AllowedMentions.none()
                    )
            except Exception:
                pass
        else:
            if isinstance(status_msg, disnake.Message):
                try:
                    await status_msg.edit(embed=done_status)
                except Exception:
                    pass
            try:
                await inter.followup.send(embed=final_emb, allowed_mentions=disnake.AllowedMentions.none())
            except Exception:
                try:
                    await inter.edit_original_response(content="⚠️ Couldn’t post the result due to permissions.")
                except Exception:
                    pass

    # -------- How-to / Help --------
    @commands.slash_command(description="How to use /parlay (requirements, limits, and options).", dm_permission=False)
    @commands.guild_only()
    async def parlayhelp(self, inter: disnake.AppCmdInter):
        remaining, today = _remaining_today(inter.author.id)
        desc = (
            "**ParlayAI+ — How it works**\n\n"
            "• Run `/parlay` to research a value parlay using high-end AI + live web sources.\n"
            "• You must be an **Admin** or have the **ParlayAI+** role to use it.\n"
            f"• Daily limit: **{DAILY_MAX_PARLAYS} successful runs per user** (resets at ET midnight).\n"
            "• Failed/timeout runs **do not** count against your daily limit.\n"
            "• Set **deliver** to `dm` (default) to receive the final result in DMs; otherwise it posts in-channel.\n"
            "• While it runs, you’ll see a live status with heartbeats. Research can take several minutes.\n\n"
            "**Parameters**\n"
            "• `sport` — league (supports `ufc` via MMA backend)\n"
            "• `legs` — number of legs (2–6)\n"
            "• `query` — what to target (teams, props, angles)\n"
            "• `date` — YYYY-MM-DD (defaults to today)\n"
            "• `region` — e.g., KY, NJ, ON (book availability)\n"
            "• `constraints` — e.g., no alt lines, avoid -200+ juice\n"
            "• `deliver` — `dm` or `channel`\n\n"
            f"**Your remaining runs today (ET):** {remaining}/{DAILY_MAX_PARLAYS}"
        )
        emb = disnake.Embed(title="📘 ParlayAI+ Help", description=desc, color=disnake.Color.blurple())
        await inter.response.send_message(embed=emb, ephemeral=True)


def setup(bot: commands.InteractionBot):
    bot.add_cog(Parlay(bot))