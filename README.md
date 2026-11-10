# Jarvis | AntiNuke

Discord AntiNuke / AntiRaid bot. Python 3.12+, disnake. Configuration is in `config.json`; no `.env`.

## Included
- AntiNuke detection for channel deletion, role deletion, and bans using Discord Audit Log.
- Burst threshold per actor and configurable response (`kick` or `alert`).
- AntiRaid join-rate alerts.
- `/backup_create` structural backup, optionally with accessible message history.
- `/backup_restore` restores channels/roles and can repost archived messages with original author/date shown as text.
- `/server_clone` copies structure and optionally reposts archived messages to another server where the bot is installed.
- `/lockdown` and `/unlockdown` slowmode controls.
- `/antinuke status` and `/jarvis_help`.

## Setup (Windows)
1. Install Python 3.12 from python.org.
2. Extract the ZIP.
3. Open `config.json` and paste your bot token into `token`.
4. Install dependencies:
   ```powershell
   py -3.12 -m pip install -r requirements.txt
   ```
5. In Discord Developer Portal -> Bot, enable **Server Members Intent** and **Message Content Intent**.
6. Invite the bot with the required permissions listed below.
7. Run:
   ```powershell
   py -3.12 bot.py
   ```

## Required permissions
- View Audit Log (required to identify who caused a destructive action)
- View Channels, Read Message History, Send Messages, Embed Links
- Manage Channels, Manage Roles
- Kick Members (only if `antinuke.response` is `kick`)
- Manage Webhooks is not required for the current detection-only webhook alert.
- The bot's role must be above roles it needs to manage/kick. Discord does not allow the bot to manage roles above its highest role.

## Configuration
- `alert_channel_id`: ID of the channel for logs; `0` uses the first suitable text channel.
- `antinuke.enabled`: enable/disable AntiNuke.
- `antinuke.window_seconds`: detection time window.
- `antinuke.actions_threshold`: destructive events before response.
- `antinuke.response`: `kick` or `alert`. For first tests, `alert` is safer.
- `antiraid.joins_threshold`: number of joins within the configured window to alert.
- `antiraid.lockdown_on_raid`: currently reserved for an optional quarantine workflow; keep false unless configured.
- `backup.max_messages_per_channel`: max number of messages archived per text channel.

## Important limitations
- Discord bots cannot copy messages while preserving the original author identity, message ID, reactions, original timestamp, or all message metadata. Archived messages are reposted as text with author and date attribution. Attachment links are saved, not re-uploaded; a link may expire or become inaccessible.
- A backup includes channel/category structure and basic settings, plus role permissions and role appearance. It does not fully reproduce integrations, server boosts, vanity URL, invite codes, scheduled events, automod rules, every forum setting, or all emoji/sticker data.
- Restoring creates missing roles/channels but does not delete existing ones or perfectly reorder all roles/channels. Check permissions after restore.
- Message history requires Message Content Intent and Read Message History permission. Discord API rate limits apply; large archives may take a long time.
- The first version archives to local JSON files in `backups/`. Keep that folder private and make separate copies of it.
- AntiNuke is not a guarantee against every attack. Audit log entries can arrive late, some actions are not observable, and the bot cannot undo all damage. Keep server owner/admin accounts secure and enable Discord's own security settings.
- Test first on a private test server. Set response to `alert` until you verify thresholds, audit log access, and trusted-user handling.

## Safety
Never share your bot token. If it leaks, reset it in the Discord Developer Portal immediately.
