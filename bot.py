# bot.py — GoodGuyStats (disnake)
import os
from dotenv import load_dotenv
import disnake
from disnake.ext import commands

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID_ENV = os.getenv("GUILD_ID")  # string from .env
TEST_GUILDS = [int(GUILD_ID_ENV)] if GUILD_ID_ENV else []  # must be a list[int]

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN not set in environment/.env")

# Intents (set BEFORE creating the bot)
intents = disnake.Intents.default()
# enable only what you actually need; slash commands don't need message content
intents.guilds = True
intents.members = True
# intents.message_content = True  # not needed for slash; requires privileged intent

# Use InteractionBot so slash commands register automatically
bot = commands.InteractionBot(
    test_guilds=TEST_GUILDS,          # instant in these guilds; empty list => global
    intents=intents,
    sync_commands_debug=True,
)

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    # If you didn't set GUILD_ID, force a global sync once on startup
    if not TEST_GUILDS:
        await bot.sync_commands()
        print("🌐 Synced slash commands globally (may take time to appear)")

# Load your sports cog (cogs/sports.py)
try:
    bot.load_extension("cogs.parlay")
    bot.load_extension("cogs.sports")
    print("📦 Loaded cog: cogs.sports & cogs.parlay")
except Exception as e:
    print(f"❌ Failed to load cogs.sports: {e}")

if __name__ == "__main__":
    bot.run(TOKEN)
