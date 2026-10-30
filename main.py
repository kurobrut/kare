
import logging
import os
import threading
import json

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta
from contextlib import contextmanager
from zoneinfo import ZoneInfo

import psycopg
import discord
from discord import app_commands
from discord.ext import commands, tasks


# =========================================================
# CONFIGURATION
# =========================================================

TOKEN = os.getenv("DISCORD_TOKEN")
DEFAULT_CHANNEL_ID = int(os.getenv("ALARM_CHANNEL_ID", "0"))
DEFAULT_ROLE_ID = int(os.getenv("ALARM_ROLE_ID", "0"))

DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace(
        "postgres://",
        "postgresql://",
        1
    )

PORT = int(os.getenv("PORT", "10000"))

PHT = ZoneInfo("Asia/Manila")

ALARM_INTERVAL = 18
FIRST_ALARM_MINUTE = 7
ALARM_COUNT = 80

CHECK_SECONDS = 5
CATCH_UP_MINUTES = 10


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

logger = logging.getLogger("karinderya_notifier")


# =========================================================
# REMINDER SCHEDULE
# =========================================================

# 12:07 AM, 12:25 AM, ... 11:49 PM

ALARM_MINUTES = tuple(
    FIRST_ALARM_MINUTE + i * ALARM_INTERVAL
    for i in range(ALARM_COUNT)
)

ALARM_INDEX = {
    minute: i + 1
    for i, minute in enumerate(ALARM_MINUTES)
}

ACCENT = discord.Colour.from_rgb(238, 137, 177)


def clock_text(value: datetime) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


def next_alarm(now: datetime | None = None):
    now = now or datetime.now(PHT)

    midnight = now.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    for day_offset in (0, 1):
        day = midnight + timedelta(days=day_offset)

        for index, minute in enumerate(ALARM_MINUTES, 1):
            candidate = day + timedelta(minutes=minute)

            if candidate > now:
                return candidate, index

    raise RuntimeError("Unable to calculate next alarm")


# =========================================================
# POSTGRESQL DATABASE
# =========================================================

@contextmanager
def database():

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. "
            "Add your Render PostgreSQL connection string."
        )

    conn = psycopg.connect(
        DATABASE_URL,
        connect_timeout=10
    )

    try:
        yield conn
        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def initialize_database():

    with database() as conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS sent (
                alarm_key TEXT PRIMARY KEY,
                sent_at TEXT NOT NULL
            )
        """)

    logger.info("PostgreSQL database initialized successfully.")


def setting(name: str, fallback: int) -> int:

    with database() as conn:

        row = conn.execute(
            "SELECT value FROM settings WHERE name=%s",
            (name,)
        ).fetchone()

    return int(row[0]) if row else fallback


def save_setting(name: str, value: int):

    with database() as conn:

        conn.execute(
            """
            INSERT INTO settings(name, value)
            VALUES(%s, %s)
            ON CONFLICT(name)
            DO UPDATE SET value=excluded.value
            """,
            (name, str(value))
        )


def alarm_key(when: datetime) -> str:
    return when.strftime("%Y-%m-%d %H:%M")


def already_sent(key: str) -> bool:

    with database() as conn:

        return conn.execute(
            "SELECT 1 FROM sent WHERE alarm_key=%s",
            (key,)
        ).fetchone() is not None


def mark_sent(key: str):

    with database() as conn:

        conn.execute(
            """
            INSERT INTO sent(alarm_key, sent_at)
            VALUES(%s, %s)
            ON CONFLICT (alarm_key) DO NOTHING
            """,
            (
                key,
                datetime.now(PHT).isoformat()
            )
        )


def sent_today(now: datetime | None = None) -> int:

    now = now or datetime.now(PHT)

    with database() as conn:

        return conn.execute(
            """
            SELECT COUNT(*)
            FROM sent
            WHERE alarm_key LIKE %s
            """,
            (now.strftime("%Y-%m-%d") + " %",)
        ).fetchone()[0]


# =========================================================
# RENDER HTTP HEALTH SERVER
# =========================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        if self.path not in ("/", "/health"):
            self.send_response(404)
            self.end_headers()
            return

        response = {
            "status": "ok",
            "service": "karinderya-notifier",
            "discord_connected": bot.is_ready()
        }

        body = json.dumps(response).encode("utf-8")

        self.send_response(200)
        self.send_header(
            "Content-Type",
            "application/json"
        )
        self.send_header(
            "Content-Length",
            str(len(body))
        )
        self.end_headers()

        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def start_health_server():

    try:

        server = ThreadingHTTPServer(
            ("0.0.0.0", PORT),
            HealthHandler
        )

        server.daemon_threads = True

        thread = threading.Thread(
            target=server.serve_forever,
            daemon=True,
            name="health-server"
        )

        thread.start()

        logger.info(
            "HTTP health server listening on 0.0.0.0:%s",
            PORT
        )

    except Exception:

        logger.exception(
            "Failed to start HTTP health server."
        )

        raise


# =========================================================
# DISCORD EMBED
# =========================================================

def alarm_embed(
    when: datetime,
    number: int,
    *,
    test=False,
    late=False
):

    if test:

        title = "Notification test"
        description = (
            "This is a test post. "
            "The notification channel and role are connected."
        )

    elif late:

        title = "Karinderya reminder · delayed"
        description = (
            "The bot has just restarted, "
            "so this reminder is being sent late."
        )

    else:

        title = "Karinderya reminder"
        description = (
            "It's time to join. "
            "Please check in when you're ready."
        )

    embed = discord.Embed(
        title=title,
        description=description,
        colour=ACCENT
    )

    embed.add_field(
        name="Scheduled",
        value=clock_text(when),
        inline=True
    )

    embed.add_field(
        name="Reminder",
        value=f"{number} of {ALARM_COUNT}" if number else "Test",
        inline=True
    )

    embed.set_footer(
        text="Daily reminders · Philippine time"
    )

    return embed


# =========================================================
# DISCORD BOT
# =========================================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix=commands.when_mentioned,
    intents=intents
)

started_at = datetime.now(PHT)


# =========================================================
# CHANNEL AND ROLE
# =========================================================

async def resolve_channel():

    channel_id = setting(
        "channel_id",
        DEFAULT_CHANNEL_ID
    )

    if not channel_id:

        logger.error(
            "No channel configured. "
            "Set ALARM_CHANNEL_ID or run /setchannel."
        )

        return None

    try:

        channel = (
            bot.get_channel(channel_id)
            or await bot.fetch_channel(channel_id)
        )

    except (
        discord.NotFound,
        discord.Forbidden,
        discord.HTTPException
    ):

        logger.exception(
            "Could not access notification channel %s",
            channel_id
        )

        return None

    if not hasattr(channel, "send"):

        logger.error(
            "Configured destination is not a message channel."
        )

        return None

    return channel


# =========================================================
# SEND REMINDER
# =========================================================

async def post_reminder(
    when: datetime,
    number: int,
    *,
    test=False,
    late=False
) -> bool:

    channel = await resolve_channel()

    if channel is None:
        return False

    role_id = setting(
        "role_id",
        DEFAULT_ROLE_ID
    )

    role = None

    if role_id:

        guild = getattr(channel, "guild", None)

        if guild is None:

            logger.error(
                "The configured channel is not in a server."
            )

            return False

        role = guild.get_role(role_id)

        if role is None:

            try:

                role = await guild.fetch_role(role_id)

            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException
            ):

                logger.error(
                    "Configured notification role %s was not found.",
                    role_id
                )

                return False

    mentions = discord.AllowedMentions(
        everyone=False,
        users=False,
        roles=[role] if role else [],
        replied_user=False
    )

    try:

        await channel.send(
            content=role.mention if role else None,
            embed=alarm_embed(
                when,
                number,
                test=test,
                late=late
            ),
            allowed_mentions=mentions
        )

        logger.info(
            "Posted reminder %s/%s for %s",
            number,
            ALARM_COUNT,
            when.isoformat()
        )

        return True

    except discord.HTTPException:

        logger.exception(
            "Discord rejected the reminder post."
        )

        return False


async def deliver_if_due(
    when: datetime,
    number: int,
    *,
    late=False
):

    key = alarm_key(when)

    if already_sent(key):
        return

    if await post_reminder(
        when,
        number,
        late=late
    ):

        mark_sent(key)


# =========================================================
# AUTOMATIC REMINDER LOOP
# =========================================================

@tasks.loop(seconds=CHECK_SECONDS)
async def reminder_loop():

    now = datetime.now(PHT)

    minute = now.hour * 60 + now.minute

    number = ALARM_INDEX.get(minute)

    if number is not None:

        await deliver_if_due(
            now.replace(
                second=0,
                microsecond=0
            ),
            number
        )


@reminder_loop.before_loop
async def before_reminder_loop():

    await bot.wait_until_ready()


# =========================================================
# CATCH-UP AFTER RESTART
# =========================================================

async def catch_up_recent_reminder():

    now = datetime.now(PHT)

    midnight = now.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    due = None

    for index in range(
        len(ALARM_MINUTES) - 1,
        -1,
        -1
    ):

        candidate = midnight + timedelta(
            minutes=ALARM_MINUTES[index]
        )

        if candidate <= now:

            due = (
                candidate,
                index + 1
            )

            break

    if due is None:
        return

    when, number = due

    delay = now - when

    if timedelta(0) <= delay <= timedelta(
        minutes=CATCH_UP_MINUTES
    ):

        if not already_sent(alarm_key(when)):

            await deliver_if_due(
                when,
                number,
                late=True
            )


# =========================================================
# BOT STARTUP
# =========================================================

@bot.event
async def setup_hook():

    initialize_database()

    commands_synced = await bot.tree.sync()

    logger.info(
        "Synced %d application commands.",
        len(commands_synced)
    )


@bot.event
async def on_ready():

    logger.info(
        "Connected as %s (%s)",
        bot.user,
        bot.user.id if bot.user else "unknown"
    )

    if not reminder_loop.is_running():

        await catch_up_recent_reminder()

        reminder_loop.start()


# =========================================================
# ADMIN CHECK
# =========================================================

def admin():

    return app_commands.checks.has_permissions(
        administrator=True
    )


# =========================================================
# /SCHEDULE
# =========================================================

@bot.tree.command(
    name="schedule",
    description="Show today's reminder times."
)
async def schedule_cmd(
    interaction: discord.Interaction
):

    embed = discord.Embed(
        title="Daily reminder schedule",
        description=(
            "80 reminders · every 18 minutes · Philippine time"
        ),
        colour=ACCENT
    )

    for start in range(0, ALARM_COUNT, 20):

        lines = []

        for index in range(
            start,
            min(start + 20, ALARM_COUNT)
        ):

            minute = ALARM_MINUTES[index]

            hour, minute_part = divmod(
                minute,
                60
            )

            stamp = datetime(
                2000,
                1,
                1,
                hour,
                minute_part
            )

            lines.append(
                f"`{index + 1:02}`  {clock_text(stamp)}"
            )

        embed.add_field(
            name=(
                f"Reminders {start + 1}–"
                f"{min(start + 20, ALARM_COUNT)}"
            ),
            value="\n".join(lines),
            inline=True
        )

    embed.set_footer(
        text="First reminder: 12:07 AM · Last reminder: 11:49 PM"
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# /NEXT
# =========================================================

@bot.tree.command(
    name="next",
    description="See when the next reminder is due."
)
async def next_cmd(
    interaction: discord.Interaction
):

    when, number = next_alarm()

    seconds = max(
        0,
        int(
            (when - datetime.now(PHT)).total_seconds()
        )
    )

    hours, remainder = divmod(
        seconds,
        3600
    )

    minutes, seconds = divmod(
        remainder,
        60
    )

    embed = discord.Embed(
        title="Next reminder",
        colour=ACCENT
    )

    embed.description = (
        f"**{when.strftime('%A, %B %-d')} · "
        f"{clock_text(when)} PHT**"
    )

    embed.add_field(
        name="Time remaining",
        value=f"{hours:02}:{minutes:02}:{seconds:02}",
        inline=True
    )

    embed.add_field(
        name="Reminder",
        value=f"{number}/{ALARM_COUNT}",
        inline=True
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# /STATUS
# =========================================================

@bot.tree.command(
    name="status",
    description="Check the notifier and today's delivery count."
)
async def status_cmd(
    interaction: discord.Interaction
):

    now = datetime.now(PHT)

    when, number = next_alarm(now)

    uptime = int(
        (now - started_at).total_seconds()
    )

    hours, rem = divmod(
        uptime,
        3600
    )

    minutes, _ = divmod(
        rem,
        60
    )

    channel_id = setting(
        "channel_id",
        DEFAULT_CHANNEL_ID
    )

    role_id = setting(
        "role_id",
        DEFAULT_ROLE_ID
    )

    embed = discord.Embed(
        title="Notifier status",
        colour=ACCENT
    )

    embed.add_field(
        name="Connection",
        value="Online",
        inline=True
    )

    embed.add_field(
        name="Today's posts",
        value=f"{sent_today(now)}/{ALARM_COUNT}",
        inline=True
    )

    embed.add_field(
        name="Uptime",
        value=f"{hours}h {minutes}m",
        inline=True
    )

    embed.add_field(
        name="Next reminder",
        value=f"{clock_text(when)} · #{number}",
        inline=True
    )

    embed.add_field(
        name="Channel",
        value=f"<#{channel_id}>" if channel_id else "Not configured",
        inline=True
    )

    embed.add_field(
        name="Role",
        value=f"<@&{role_id}>" if role_id else "Not configured",
        inline=True
    )

    embed.set_footer(
        text="Timezone: Asia/Manila"
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# /TEST
# =========================================================

@bot.tree.command(
    name="test",
    description="Send a test notification to the configured channel."
)
@admin()
async def test_cmd(
    interaction: discord.Interaction
):

    await interaction.response.defer(
        ephemeral=True
    )

    success = await post_reminder(
        datetime.now(PHT),
        0,
        test=True
    )

    await interaction.followup.send(
        (
            "Test notification sent."
            if success
            else "Could not send. Check the channel and role permissions."
        ),
        ephemeral=True
    )


# =========================================================
# /SETROLE
# =========================================================

@bot.tree.command(
    name="setrole",
    description="Choose the role mentioned by reminders."
)
@admin()
@app_commands.describe(
    role="Role to mention when a reminder is posted."
)
async def setrole_cmd(
    interaction: discord.Interaction,
    role: discord.Role
):

    save_setting(
        "role_id",
        role.id
    )

    await interaction.response.send_message(
        f"Notification role updated to {role.mention}.",
        allowed_mentions=discord.AllowedMentions.none(),
        ephemeral=True
    )


# =========================================================
# /SETCHANNEL
# =========================================================

@bot.tree.command(
    name="setchannel",
    description="Choose where reminders are posted."
)
@admin()
@app_commands.describe(
    channel="Text channel for scheduled reminders."
)
async def setchannel_cmd(
    interaction: discord.Interaction,
    channel: discord.TextChannel
):

    save_setting(
        "channel_id",
        channel.id
    )

    await interaction.response.send_message(
        f"Notification channel updated to {channel.mention}.",
        allowed_mentions=discord.AllowedMentions.none(),
        ephemeral=True
    )


# =========================================================
# /HELP
# =========================================================

@bot.tree.command(
    name="help",
    description="List the notifier commands."
)
async def help_cmd(
    interaction: discord.Interaction
):

    embed = discord.Embed(
        title="Notifier commands",
        colour=ACCENT
    )

    embed.add_field(
        name="Available to everyone",
        value=(
            "`/next` — next reminder and countdown\n"
            "`/schedule` — full daily schedule\n"
            "`/status` — connection and delivery status\n"
            "`/help` — command list"
        ),
        inline=False
    )

    embed.add_field(
        name="Server administrators",
        value=(
            "`/test` — send a test post\n"
            "`/setrole` — change the notification role\n"
            "`/setchannel` — change the notification channel"
        ),
        inline=False
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# COMMAND ERROR HANDLER
# =========================================================

@bot.tree.error
async def command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):

    if isinstance(
        error,
        app_commands.MissingPermissions
    ):

        message = (
            "You need the Administrator permission "
            "to use that command."
        )

    else:

        logger.exception(
            "Application command failed: %s",
            error
        )

        message = (
            "That command failed. "
            "Please check the bot logs or try again."
        )

    if interaction.response.is_done():

        await interaction.followup.send(
            message,
            ephemeral=True
        )

    else:

        await interaction.response.send_message(
            message,
            ephemeral=True
        )


# =========================================================
# STARTUP VALIDATION
# =========================================================

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set.")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. "
        "Add your Render PostgreSQL connection string."
    )

if not DEFAULT_CHANNEL_ID:

    logger.warning(
        "ALARM_CHANNEL_ID is empty; "
        "configure a channel with /setchannel."
    )

if not DEFAULT_ROLE_ID:

    logger.warning(
        "ALARM_ROLE_ID is empty; "
        "configure a role with /setrole."
    )


# =========================================================
# START HTTP SERVER, THEN DISCORD BOT
# =========================================================

start_health_server()

bot.run(TOKEN)
