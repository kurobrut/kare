# 🍜 Karinderya Discord Alarm Bot

A Discord bot that sends a role-pinged Karinderya joining-time reminder at each of the 80 daily schedule times, using the Philippines timezone (`Asia/Manila`, UTC+8).

## Schedule

- First alarm: **12:07 AM**
- Repeats every **18 minutes**
- Last alarm: **11:49 PM**
- Total: **80 alarms per day**
- Notification timing: **exact scheduled joining time**
- Mentions: **one configured Discord role only** (never `@everyone`)

The schedule is generated from the interval, so all 80 times are consistent.

## Slash commands

| Command | What it does |
|---|---|
| `/schedule` | Shows all 80 joining times (ephemeral to the requester). |
| `/nextalarm` | Shows the next alarm and approximate time remaining. |
| `/testalarm` | Sends a test message to the configured channel and pings the configured role. Server administrators only. |

## 1. Create the Discord application

1. Open the [Discord Developer Portal](https://discord.com/developers/applications).
2. Create an application and add a Bot.
3. Copy the bot token. Keep it secret; do not commit it to GitHub.
4. Under **OAuth2 → URL Generator**, select the `bot` and `applications.commands` scopes.
5. Select these bot permissions:
   - View Channels
   - Send Messages
   - Embed Links
   - Mention @everyone, @here, and All Roles (needed for role mentions when the role is not otherwise mentionable; alternatively make the target role mentionable).
6. Open the generated invite URL and add the bot to your server.
7. In Discord, enable **Developer Mode** under User Settings → Advanced. Right-click the alarm text channel and select **Copy Channel ID**. Right-click the target role and select **Copy Role ID**.
8. Make sure the bot can view and send messages in the alarm channel. If relying on the bot's role hierarchy to mention a role, place the bot's highest role above the target role.

Do not enable Message Content Intent; this bot does not need it.

## 2. Create the GitHub repository

Create a repository, for example `karinderya-alarm-bot`, and upload:

```text
karinderya-alarm-bot/
├── main.py
├── requirements.txt
├── render.yaml
├── README.md
└── .gitignore
```

Keep the repository free of bot tokens and other secrets.

## 3. Deploy to Render

This project uses a **Background Worker** because the bot needs to stay running continuously.

1. Sign in at [Render](https://dashboard.render.com/).
2. Connect your GitHub account and select this repository.
3. Choose **New → Blueprint** and select the repository. Render detects `render.yaml`.
4. The Blueprint creates a Background Worker. The included `starter` plan is paid; Render does not provide a free always-on Background Worker.
5. Set the three environment variables when prompted (or later in the worker's Environment settings):

| Variable | Value |
|---|---|
| `DISCORD_TOKEN` | Your Discord bot token |
| `ALARM_CHANNEL_ID` | Numeric ID of the Discord text channel |
| `ALARM_ROLE_ID` | Numeric ID of the role to ping |

6. Deploy and check the Render logs. You should see `Logged in as ...` and `Karinderya Alarm System`-related startup logs.

If you use Render's dashboard instead of the Blueprint, create a **Background Worker** with:
- Runtime: Python
- Build command: `pip install -r requirements.txt`
- Start command: `python main.py`

## 4. Test it

After deployment:
1. Wait for the bot to appear online in Discord.
2. Run `/testalarm` as a server administrator. It should send a test embed and ping the configured role.
3. Run `/schedule` to view the full 80-time schedule.
4. Run `/nextalarm` to see the next scheduled alarm.

Global slash commands can take a little time to appear after the first deployment.

## Notes

- All schedule calculations use `Asia/Manila`, not the Render server's timezone.
- The bot checks every five seconds and sends at most once per scheduled minute while its process remains running.
- If the bot is offline at an alarm time, that reminder is skipped; it does not send missed reminders after reconnecting.
- A restart during the same scheduled minute can cause a duplicate notification because Render's ordinary worker filesystem is not used as durable alarm state.
- Keep the Render worker running continuously for reliable daily reminders.
