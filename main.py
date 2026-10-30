import os
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ============================================================
# Configuration
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("ALARM_CHANNEL_ID", "0"))
ROLE_ID = int(os.getenv("ALARM_ROLE_ID", "0"))

TIMEZONE = ZoneInfo("Asia/Manila")
CHECK_INTERVAL_SECONDS = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("karinderya-alarm")

# 12:07 AM, then every 18 minutes. This produces exactly 80
# joining times, ending at 11:49 PM (Philippines time).
ALARM_TIMES = tuple(
    (7 + index * 18) % (24 * 60)
    for index in range(80)
)
ALARM_MINUTES = set(ALARM_TIMES)


def format_time(minutes_after_midnight: int) -> str:
    hour = minutes_after_midnight // 60
    minute = minutes_after_midnight % 60
    hour_12 = hour % 12 or 12
    suffix = "AM" if hour < 12 else "PM"
    return f"{hour_12}:{minute:02d} {suffix}"


def get_next_alarm(now: datetime | None = None):
    """Return (alarm_datetime, minutes_until) for the next schedule slot."""
    now = now or datetime.now(TIMEZONE)
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    for day_offset in (0, 1):
        day = today_midnight + timedelta(days=day_offset)
        for minute_of_day in ALARM_TIMES:
            candidate = day + timedelta(
                hours=minute_of_day // 60,
                minutes=minute_of_day % 60,
            )
            if candidate > now:
                return candidate, int((candidate - now).total_seconds())

    raise RuntimeError("Could not calculate the next alarm")


def make_alarm_embed(alarm_time: datetime | None = None) -> discord.Embed:
    alarm_time = alarm_time or datetime.now(TIMEZONE)
    embed = discord.Embed(
        title="🍜 KARINDERYA ALARM!",
        description=(
            "⏰ **IT'S JOINING TIME!**\n\n"
            "Join Karinderya now!\n"
            "The joining time is here — be ready before getting garbage. 🗑️"
        ),
        color=discord.Color.from_rgb(255, 143, 194),
        timestamp=alarm_time,
    )
    embed.add_field(
        name="🕒 Joining Time",
        value=alarm_time.strftime("%I:%M %p").lstrip("0"),
        inline=True,
    )
    embed.add_field(
        name="🌏 Timezone",
        value="Philippines (PHT, UTC+8)",
        inline=True,
    )
    embed.set_footer(text="Karinderya Alarm • Daily schedule")
    return embed


intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# Prevent duplicate sends during one running process.
last_sent_key: str | None = None


async def get_alarm_channel():
    if not CHANNEL_ID:
        log.error("ALARM_CHANNEL_ID is not configured.")
        return None

    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.error("Could not access alarm channel %s: %s", CHANNEL_ID, exc)
            return None

    if not hasattr(channel, "send"):
        log.error("Configured alarm channel does not support messages.")
        return None

    return channel


async def send_alarm(*, alarm_time: datetime | None = None, test: bool = False):
    channel = await get_alarm_channel()
    if channel is None:
        return False

    role = None
    content = None

    if ROLE_ID:
        role = channel.guild.get_role(ROLE_ID) if hasattr(channel, "guild") else None
        if role is None and hasattr(channel, "guild"):
            try:
                role = await channel.guild.fetch_role(ROLE_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                role = None

        if role is None:
            log.error("ALARM_ROLE_ID %s was not found in the alarm server.", ROLE_ID)
            return False

        content = role.mention

    embed = make_alarm_embed(alarm_time)
    if test:
        embed.title = "🧪 TEST — KARINDERYA ALARM"
        embed.description = (
            "This is a test notification. Your role mention and alarm channel "
            "are configured to receive Karinderya reminders."
        )

    allowed_mentions = discord.AllowedMentions(
        everyone=False,
        users=False,
        roles=[role] if role else [],
        replied_user=False,
    )

    try:
        await channel.send(
            content=content,
            embed=embed,
            allowed_mentions=allowed_mentions,
        )
        log.info("Sent %s alarm to channel %s.", "test" if test else "scheduled", CHANNEL_ID)
        return True
    except discord.HTTPException as exc:
        log.exception("Failed to send alarm: %s", exc)
        return False


@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def alarm_checker():
    global last_sent_key

    now = datetime.now(TIMEZONE)
    minute_of_day = now.hour * 60 + now.minute

    if minute_of_day not in ALARM_MINUTES:
        return

    alarm_key = now.strftime("%Y-%m-%d %H:%M")
    if last_sent_key == alarm_key:
        return

    # Mark only after a successful send so a temporary Discord error
    # can be retried on the next five-second check within this minute.
    sent = await send_alarm(alarm_time=now)
    if sent:
        last_sent_key = alarm_key


@alarm_checker.before_loop
async def before_alarm_checker():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id if bot.user else "unknown")

    if not alarm_checker.is_running():
        alarm_checker.start()

    # Sync slash commands on startup. Global command propagation can take
    # a little while the first time they are deployed.
    try:
        synced = await bot.tree.sync()
        log.info("Synced %s slash command(s).", len(synced))
    except discord.HTTPException:
        log.exception("Could not sync slash commands.")


@bot.tree.command(name="schedule", description="Show the daily Karinderya joining-time schedule.")
async def schedule_command(interaction: discord.Interaction):
    lines = []
    for index, minute in enumerate(ALARM_TIMES, start=1):
        lines.append(f"`{index:02d}.` {format_time(minute)}")

    # Split into fields to stay within Discord's 1024-character field limit.
    embed = discord.Embed(
        title="🍜 Karinderya Daily Schedule",
        description="80 joining times • Every 18 minutes • Philippines time (PHT)",
        color=discord.Color.from_rgb(255, 143, 194),
    )
    for start in range(0, len(lines), 20):
        chunk = "\n".join(lines[start:start + 20])
        embed.add_field(
            name=f"Joining times {start + 1}–{min(start + 20, 80)}",
            value=chunk,
            inline=True,
        )
    embed.set_footer(text="First: 12:07 AM • Last: 11:49 PM")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="nextalarm", description="Show when the next Karinderya alarm will happen.")
async def nextalarm_command(interaction: discord.Interaction):
    alarm, seconds = get_next_alarm()
    total_minutes = seconds // 60
    hours, minutes = divmod(total_minutes, 60)

    embed = discord.Embed(
        title="⏰ Next Karinderya Alarm",
        description=f"**{alarm.strftime('%A, %B %d')} at {alarm.strftime('%I:%M %p').lstrip('0')} PHT**",
        color=discord.Color.from_rgb(255, 143, 194),
    )
    embed.add_field(name="Time remaining", value=f"{hours}h {minutes}m", inline=True)
    embed.add_field(name="Timezone", value="Asia/Manila (UTC+8)", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="testalarm", description="Send a test Karinderya alarm to the configured channel.")
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def testalarm_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    sent = await send_alarm(test=True)
    if sent:
        await interaction.followup.send(
            "✅ Test alarm sent to the configured alarm channel.",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            "❌ The test alarm could not be sent. Check the Render logs and channel/role IDs.",
            ephemeral=True,
        )


@testalarm_command.error
async def testalarm_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        message = "Only server administrators can use `/testalarm`."
    else:
        log.exception("Slash command error: %s", error)
        message = "Something went wrong while running that command."

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Set it in Render Environment Variables.")

if not CHANNEL_ID:
    raise RuntimeError("ALARM_CHANNEL_ID is missing. Set it to your Discord text channel ID.")

if not ROLE_ID:
    raise RuntimeError("ALARM_ROLE_ID is missing. Set it to the role ID you want to ping.")

bot.run(TOKEN)
