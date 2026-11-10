# Jarvis | AntiNuke
# Python 3.12 + disnake
# Configuration is stored in config.json; no .env required.

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import disnake
from disnake.ext import commands

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
BACKUP_DIR = ROOT / "backups"
BACKUP_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("jarvis-antinuke")


DEFAULT_CONFIG = {
    "token": "",
    "prefix": "!",
    "alert_channel_id": ""
    "owner_bypass": True,
    "antinuke": {
        "enabled": True,
        "window_seconds": 12,
        "actions_threshold": 3,
        "response": "kick",
        "log_events": True
    },
    "antiraid": {
        "enabled": True,
        "join_window_seconds": 15,
        "joins_threshold": 8,
        "lockdown_on_raid": False,
        "raid_role_id": 0
    },
    "backup": {
        "max_messages_per_channel": 1000,
        "include_message_content": True,
        "include_attachments": True
    }
}


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        raise SystemExit(
            "Created config.json. Put your bot token in config.json, then start the bot again."
        )
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"config.json is not valid JSON: {exc}") from exc

    # Fill in keys added by newer versions without overwriting existing settings.
    def merge(dst: dict, src: dict) -> None:
        for key, value in src.items():
            if key not in dst:
                dst[key] = value
            elif isinstance(value, dict) and isinstance(dst[key], dict):
                merge(dst[key], value)
    merge(config, DEFAULT_CONFIG)
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    if not config.get("token") or config["token"] == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise SystemExit("Set your bot token in config.json.")
    return config


config = load_config()
intents = disnake.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
intents.webhooks = True

bot = commands.InteractionBot(intents=intents)
destructive_actions: dict[tuple[int, int], deque[float]] = defaultdict(deque)
join_events: dict[int, deque[float]] = defaultdict(deque)
raid_lockdown_active: set[int] = set()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def backup_path(guild_id: int) -> Path:
    return BACKUP_DIR / f"{guild_id}.json"


def get_alert_channel(guild: disnake.Guild):
    channel_id = int(config.get("alert_channel_id", 0) or 0)
    if channel_id:
        channel = guild.get_channel(channel_id)
        if channel:
            return channel
    # fallback to the first text channel where the bot can send messages
    for channel in guild.text_channels:
        perms = channel.permissions_for(guild.me)
        if perms.send_messages and perms.embed_links:
            return channel
    return None


async def send_log(guild: disnake.Guild, title: str, description: str, color=disnake.Color.blurple()):
    if not config.get("antinuke", {}).get("log_events", True):
        return
    channel = get_alert_channel(guild)
    if channel is None:
        return
    embed = disnake.Embed(title=title, description=description[:4000], color=color, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text="Jarvis | AntiNuke")
    try:
        await channel.send(embed=embed)
    except disnake.HTTPException as exc:
        log.warning("Could not send log to guild %s: %s", guild.id, exc)


async def find_audit_actor(guild: disnake.Guild, action: disnake.AuditLogAction, target_id: int | None = None):
    """Return a recent audit-log entry actor; requires View Audit Log."""
    try:
        async for entry in guild.audit_logs(limit=8, action=action):
            if target_id is not None and getattr(entry.target, "id", None) != target_id:
                continue
            created = entry.created_at.timestamp()
            if abs(time.time() - created) <= 20:
                return entry.user, entry
    except (disnake.Forbidden, disnake.HTTPException):
        log.debug("Cannot read audit log for guild %s", guild.id)
    return None, None


def is_trusted(guild: disnake.Guild, member: disnake.Member | None) -> bool:
    if member is None:
        return True
    if member.id == guild.owner_id and config.get("owner_bypass", True):
        return True
    if bot.user and member.id == bot.user.id:
        return True
    return False


async def handle_destructive_event(
    guild: disnake.Guild,
    action: disnake.AuditLogAction,
    target_id: int,
    event_label: str,
):
    if not config.get("antinuke", {}).get("enabled", True):
        return

    actor, entry = await find_audit_actor(guild, action, target_id)
    if actor is None:
        await send_log(
            guild,
            "⚠️ Подозрительное изменение",
            f"Событие: **{event_label}**\nНе удалось определить инициатора по Audit Log. Проверьте права бота.",
            disnake.Color.orange(),
        )
        return

    member = guild.get_member(actor.id)
    if is_trusted(guild, member):
        await send_log(guild, "Изменение сервера", f"**{event_label}**\nИнициатор: {actor.mention} (`{actor.id}`)")
        return

    key = (guild.id, actor.id)
    events = destructive_actions[key]
    now = time.time()
    events.append(now)
    window = max(3, int(config["antinuke"].get("window_seconds", 12)))
    while events and now - events[0] > window:
        events.popleft()

    threshold = max(2, int(config["antinuke"].get("actions_threshold", 3)))
    await send_log(
        guild,
        "🚨 AntiNuke: опасное действие",
        f"Событие: **{event_label}**\nИнициатор: {actor.mention} (`{actor.id}`)\n"
        f"Действий за {window} сек.: **{len(events)}/{threshold}**",
        disnake.Color.red() if len(events) >= threshold else disnake.Color.orange(),
    )

    if len(events) < threshold:
        return

    response = str(config["antinuke"].get("response", "kick")).lower()
    # Clear the queue so one burst does not repeatedly trigger actions.
    events.clear()
    if response == "alert":
        return

    if member is None:
        try:
            member = await guild.fetch_member(actor.id)
        except (disnake.NotFound, disnake.Forbidden, disnake.HTTPException):
            member = None

    if member is None or member.id == guild.owner_id or (bot.user and member.id == bot.user.id):
        return

    me = guild.me
    if me is None or not me.guild_permissions.kick_members:
        await send_log(guild, "AntiNuke: не хватает прав", "Достигнут порог, но у бота нет права **Kick Members**.", disnake.Color.red())
        return

    if member.top_role >= me.top_role:
        await send_log(guild, "AntiNuke: не удалось остановить", f"Роль {member.mention} выше или равна роли бота. Поднимите роль бота.", disnake.Color.red())
        return

    try:
        await member.kick(reason=f"Jarvis AntiNuke: {len(events) or threshold} destructive actions within {window}s")
        await send_log(guild, "🛡️ AntiNuke: нарушитель исключён", f"Участник: **{member}** (`{member.id}`)\nПричина: серия опасных действий.", disnake.Color.green())
    except (disnake.Forbidden, disnake.HTTPException) as exc:
        await send_log(guild, "AntiNuke: ошибка действия", f"Не удалось исключить {member.mention}: `{exc}`", disnake.Color.red())


def serialise_overwrites(channel: disnake.abc.GuildChannel) -> list[dict[str, Any]]:
    result = []
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        result.append({
            "target_id": target.id,
            "target_type": "role" if isinstance(target, disnake.Role) else "member",
            "allow": allow.value,
            "deny": deny.value
        })
    return result


async def create_backup(guild: disnake.Guild, include_messages: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "format_version": 1,
        "guild_id": guild.id,
        "guild_name": guild.name,
        "created_at": utc_now(),
        "roles": [],
        "categories": [],
        "channels": [],
        "messages": {}
    }

    for role in sorted(guild.roles, key=lambda r: r.position):
        if role.is_default():
            continue
        data["roles"].append({
            "id": role.id,
            "name": role.name,
            "permissions": role.permissions.value,
            "color": role.color.value,
            "hoist": role.hoist,
            "mentionable": role.mentionable,
            "position": role.position
        })

    for category in sorted(guild.categories, key=lambda c: c.position):
        data["categories"].append({
            "id": category.id,
            "name": category.name,
            "position": category.position,
            "overwrites": serialise_overwrites(category)
        })

    max_messages = max(0, int(config["backup"].get("max_messages_per_channel", 1000)))
    for channel in sorted(guild.channels, key=lambda c: (getattr(c, "position", 0), c.id)):
        if isinstance(channel, disnake.CategoryChannel):
            continue
        item: dict[str, Any] = {
            "id": channel.id,
            "name": channel.name,
            "type": str(channel.type),
            "position": getattr(channel, "position", 0),
            "category_id": channel.category_id,
            "overwrites": serialise_overwrites(channel),
        }
        if isinstance(channel, disnake.TextChannel):
            item.update({
                "topic": channel.topic,
                "nsfw": channel.nsfw,
                "slowmode_delay": channel.slowmode_delay,
                "news": channel.is_news(),
            })
        elif isinstance(channel, disnake.VoiceChannel):
            item.update({
                "bitrate": channel.bitrate,
                "user_limit": channel.user_limit,
                "rtc_region": str(channel.rtc_region) if channel.rtc_region else None,
            })
        elif isinstance(channel, disnake.ForumChannel):
            item.update({
                "topic": channel.topic,
                "nsfw": channel.nsfw,
                "slowmode_delay": channel.slowmode_delay,
            })
        data["channels"].append(item)

        if include_messages and isinstance(channel, disnake.TextChannel) and max_messages:
            history = []
            try:
                async for message in channel.history(limit=max_messages, oldest_first=True):
                    msg = {
                        "author": str(message.author),
                        "author_id": message.author.id,
                        "created_at": message.created_at.isoformat(),
                        "content": message.content if config["backup"].get("include_message_content", True) else "",
                        "attachments": [],
                        "embeds": []
                    }
                    if config["backup"].get("include_attachments", True):
                        msg["attachments"] = [
                            {"filename": a.filename, "url": a.url}
                            for a in message.attachments
                        ]
                    for embed in message.embeds[:5]:
                        try:
                            msg["embeds"].append(embed.to_dict())
                        except Exception:
                            pass
                    history.append(msg)
            except (disnake.Forbidden, disnake.HTTPException) as exc:
                log.warning("Cannot archive history from #%s: %s", channel.name, exc)
            data["messages"][str(channel.id)] = history

    return data


async def apply_overwrites(guild: disnake.Guild, overwrite_data: list[dict[str, Any]]) -> dict:
    result = {}
    for item in overwrite_data:
        target = None
        if item["target_type"] == "role":
            old_id = int(item["target_id"])
            target = guild.get_role(old_id)
            if target is None:
                # In a backup restored to a different server, old IDs won't match.
                target = disnake.utils.get(guild.roles, name="@everyone") if old_id == int(item.get("guild_id", 0)) else None
        else:
            target = guild.get_member(int(item["target_id"]))
        if target is None:
            continue
        allow = disnake.Permissions(int(item["allow"]))
        deny = disnake.Permissions(int(item["deny"]))
        result[target] = disnake.PermissionOverwrite.from_pair(allow, deny)
    return result


async def restore_structure(guild: disnake.Guild, data: dict[str, Any], restore_messages: bool = False) -> list[str]:
    warnings = []
    role_map: dict[int, disnake.Role] = {}
    category_map: dict[int, disnake.CategoryChannel] = {}
    channel_map: dict[int, disnake.abc.GuildChannel] = {}

    # Role creation requires Manage Roles. The @everyone role is not recreated.
    for item in data.get("roles", []):
        existing = disnake.utils.get(guild.roles, name=item["name"])
        if existing:
            role_map[int(item["id"])] = existing
            continue
        try:
            role = await guild.create_role(
                name=item["name"],
                permissions=disnake.Permissions(int(item["permissions"])),
                colour=disnake.Colour(int(item["color"])),
                hoist=bool(item["hoist"]),
                mentionable=bool(item["mentionable"]),
                reason="Jarvis AntiNuke backup restore"
            )
            role_map[int(item["id"])] = role
        except (disnake.Forbidden, disnake.HTTPException) as exc:
            warnings.append(f"Role `{item['name']}`: {exc}")

    # Create categories first. Permission overwrites are mapped by role name when IDs differ.
    for item in data.get("categories", []):
        existing = disnake.utils.get(guild.categories, name=item["name"])
        if existing:
            category_map[int(item["id"])] = existing
            continue
        overwrites = {}
        for ow in item.get("overwrites", []):
            target = None
            if ow["target_type"] == "role":
                target = role_map.get(int(ow["target_id"]))
                if target is None and int(ow["target_id"]) == int(data["guild_id"]):
                    target = guild.default_role
            if target:
                overwrites[target] = disnake.PermissionOverwrite.from_pair(
                    disnake.Permissions(int(ow["allow"])),
                    disnake.Permissions(int(ow["deny"]))
                )
        try:
            category = await guild.create_category(item["name"], overwrites=overwrites, reason="Jarvis AntiNuke backup restore")
            category_map[int(item["id"])] = category
        except (disnake.Forbidden, disnake.HTTPException) as exc:
            warnings.append(f"Category `{item['name']}`: {exc}")

    for item in data.get("channels", []):
        old_category = category_map.get(int(item.get("category_id") or 0))
        overwrites = {}
        for ow in item.get("overwrites", []):
            target = None
            if ow["target_type"] == "role":
                target = role_map.get(int(ow["target_id"]))
                if target is None and int(ow["target_id"]) == int(data["guild_id"]):
                    target = guild.default_role
            if target:
                overwrites[target] = disnake.PermissionOverwrite.from_pair(
                    disnake.Permissions(int(ow["allow"])),
                    disnake.Permissions(int(ow["deny"]))
                )
        try:
            ctype = item.get("type", "text")
            if ctype in ("text", "news"):
                channel = await guild.create_text_channel(
                    item["name"], category=old_category, overwrites=overwrites,
                    topic=item.get("topic"), nsfw=bool(item.get("nsfw", False)),
                    slowmode_delay=int(item.get("slowmode_delay", 0)),
                    reason="Jarvis AntiNuke backup restore"
                )
            elif ctype == "voice":
                channel = await guild.create_voice_channel(
                    item["name"], category=old_category, overwrites=overwrites,
                    bitrate=min(int(item.get("bitrate", 64000)), guild.bitrate_limit),
                    user_limit=int(item.get("user_limit", 0)),
                    reason="Jarvis AntiNuke backup restore"
                )
            elif ctype == "stage_voice":
                channel = await guild.create_stage_channel(
                    item["name"], category=old_category, overwrites=overwrites,
                    reason="Jarvis AntiNuke backup restore"
                )
            elif ctype == "forum":
                channel = await guild.create_forum_channel(
                    item["name"], category=old_category, overwrites=overwrites,
                    topic=item.get("topic"), nsfw=bool(item.get("nsfw", False)),
                    reason="Jarvis AntiNuke backup restore"
                )
            else:
                warnings.append(f"Skipped unsupported channel type `{ctype}`: {item['name']}")
                continue
            channel_map[int(item["id"])] = channel
        except (disnake.Forbidden, disnake.HTTPException, TypeError) as exc:
            warnings.append(f"Channel `{item['name']}`: {exc}")

    if restore_messages:
        for old_channel_id, messages in data.get("messages", {}).items():
            destination = channel_map.get(int(old_channel_id))
            if not isinstance(destination, disnake.TextChannel):
                # If restoring onto the same guild, try to use an existing matching channel.
                old_item = next((c for c in data.get("channels", []) if int(c["id"]) == int(old_channel_id)), None)
                if old_item:
                    destination = disnake.utils.get(guild.text_channels, name=old_item["name"])
            if not isinstance(destination, disnake.TextChannel):
                continue
            for msg in messages:
                content = (msg.get("content") or "").strip()
                attachment_urls = "\n".join(
                    f"📎 **{a.get('filename', 'attachment')}**: {a.get('url', '')}"
                    for a in msg.get("attachments", [])
                )
                stamp = msg.get("created_at", "unknown time")
                body = f"**{msg.get('author', 'Unknown user')}** · `{stamp}`\n"
                body += content if content else "_(no text content)_"
                if attachment_urls:
                    body += "\n" + attachment_urls
                if len(body) > 1900:
                    body = body[:1890] + "…"
                try:
                    await destination.send(body, allowed_mentions=disnake.AllowedMentions.none())
                    await asyncio.sleep(0.35)  # avoid hitting message rate limits
                except (disnake.Forbidden, disnake.HTTPException) as exc:
                    warnings.append(f"Messages in #{destination.name}: {exc}")
                    break

    return warnings


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "?")
    log.info("Connected to %d guild(s)", len(bot.guilds))


@bot.event
async def on_guild_channel_delete(channel: disnake.abc.GuildChannel):
    await handle_destructive_event(channel.guild, disnake.AuditLogAction.channel_delete, channel.id, f"Удаление канала `{channel.name}`")


@bot.event
async def on_guild_role_delete(role: disnake.Role):
    await handle_destructive_event(role.guild, disnake.AuditLogAction.role_delete, role.id, f"Удаление роли `{role.name}`")


@bot.event
async def on_member_ban(guild: disnake.Guild, user: disnake.User):
    await handle_destructive_event(guild, disnake.AuditLogAction.ban, user.id, f"Бан участника `{user}`")


@bot.event
async def on_webhooks_update(channel: disnake.abc.GuildChannel):
    # Webhook update event has no target details; report for visibility rather than auto-kick.
    await send_log(channel.guild, "⚠️ Изменение вебхуков", f"В канале `{channel.name}` изменились вебхуки. Проверьте Audit Log.", disnake.Color.orange())


@bot.event
async def on_member_join(member: disnake.Member):
    settings = config.get("antiraid", {})
    if not settings.get("enabled", True):
        return
    guild = member.guild
    now = time.time()
    events = join_events[guild.id]
    events.append(now)
    window = max(5, int(settings.get("join_window_seconds", 15)))
    while events and now - events[0] > window:
        events.popleft()
    threshold = max(3, int(settings.get("joins_threshold", 8)))

    if len(events) >= threshold and guild.id not in raid_lockdown_active:
        await send_log(
            guild,
            "🚨 AntiRaid: возможен рейд",
            f"За последние **{window} секунд** вошло **{len(events)} участников**.\n"
            f"Порог: **{threshold}**. Проверьте участников и включите Lockdown при необходимости.",
            disnake.Color.red(),
        )
        if settings.get("lockdown_on_raid", False):
            raid_lockdown_active.add(guild.id)
            role_id = int(settings.get("raid_role_id", 0) or 0)
            raid_role = guild.get_role(role_id) if role_id else None
            if raid_role:
                try:
                    await member.add_roles(raid_role, reason="Jarvis AntiRaid temporary raid quarantine")
                except (disnake.Forbidden, disnake.HTTPException):
                    pass

    await send_log(guild, "Участник присоединился", f"{member.mention} (`{member.id}`) · аккаунт создан <t:{int(member.created_at.timestamp())}:R>", disnake.Color.blurple())


@bot.slash_command(name="antinuke", description="Управление защитой AntiNuke")
async def antinuke(inter: disnake.ApplicationCommandInteraction):
    pass


@antinuke.sub_command(name="status", description="Показать статус защиты")
async def antinuke_status(inter: disnake.ApplicationCommandInteraction):
    settings = config["antinuke"]
    raid = config["antiraid"]
    embed = disnake.Embed(title="🛡️ Jarvis | AntiNuke", color=disnake.Color.blurple())
    embed.add_field(name="AntiNuke", value="Включён" if settings.get("enabled") else "Выключен", inline=True)
    embed.add_field(name="AntiRaid", value="Включён" if raid.get("enabled") else "Выключен", inline=True)
    embed.add_field(name="Порог действий", value=f"{settings.get('actions_threshold')} за {settings.get('window_seconds')} сек.", inline=False)
    embed.add_field(name="Порог входов", value=f"{raid.get('joins_threshold')} за {raid.get('join_window_seconds')} сек.", inline=False)
    embed.set_footer(text="Настройки хранятся в config.json")
    await inter.response.send_message(embed=embed, ephemeral=True)


@bot.slash_command(name="backup_create", description="Создать резервную копию структуры сервера")
async def backup_create(
    inter: disnake.ApplicationCommandInteraction,
    include_messages: bool = commands.Param(default=False, description="Архивировать доступную историю сообщений (может занять время)")
):
    if not inter.guild or not isinstance(inter.author, disnake.Member) or not inter.author.guild_permissions.administrator:
        await inter.response.send_message("Эта команда доступна администраторам сервера.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True)
    try:
        data = await create_backup(inter.guild, include_messages=include_messages)
        path = backup_path(inter.guild.id)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        await inter.followup.send(
            f"✅ Резервная копия создана: `{path.name}`\n"
            f"Ролей: **{len(data['roles'])}**, категорий: **{len(data['categories'])}**, каналов: **{len(data['channels'])}**.\n"
            f"Сообщения: **{'включены' if include_messages else 'не включены'}**.",
            ephemeral=True
        )
        await send_log(inter.guild, "📦 Создана резервная копия", f"Автор: {inter.author.mention}\nСообщений включено: `{include_messages}`")
    except Exception as exc:
        log.exception("Backup failed")
        await inter.followup.send(f"❌ Ошибка создания копии: `{type(exc).__name__}: {exc}`", ephemeral=True)


@bot.slash_command(name="backup_restore", description="Восстановить структуру из резервной копии")
async def backup_restore(
    inter: disnake.ApplicationCommandInteraction,
    restore_messages: bool = commands.Param(default=False, description="Перепубликовать архивные сообщения с указанием автора и даты")
):
    if not inter.guild or not isinstance(inter.author, disnake.Member) or not inter.author.guild_permissions.administrator:
        await inter.response.send_message("Эта команда доступна администраторам сервера.", ephemeral=True)
        return
    path = backup_path(inter.guild.id)
    if not path.exists():
        await inter.response.send_message("Для этого сервера ещё нет копии. Сначала выполни `/backup_create`.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        warnings = await restore_structure(inter.guild, data, restore_messages=restore_messages)
        message = "✅ Восстановление завершено."
        if warnings:
            message += f"\nПредупреждений: **{len(warnings)}**\n" + "\n".join(f"• {w}" for w in warnings[:8])
        await inter.followup.send(message[:1900], ephemeral=True)
        await send_log(inter.guild, "♻️ Запущено восстановление", f"Автор: {inter.author.mention}\nПредупреждений: `{len(warnings)}`")
    except Exception as exc:
        log.exception("Restore failed")
        await inter.followup.send(f"❌ Ошибка восстановления: `{type(exc).__name__}: {exc}`", ephemeral=True)


@bot.slash_command(name="server_clone", description="Скопировать структуру на другой сервер")
async def server_clone(
    inter: disnake.ApplicationCommandInteraction,
    target_guild_id: str = commands.Param(description="ID сервера назначения, куда добавлен этот бот"),
    include_messages: bool = commands.Param(default=False, description="Перенести доступную историю сообщений")
):
    if not inter.guild or not isinstance(inter.author, disnake.Member) or not inter.author.guild_permissions.administrator:
        await inter.response.send_message("Эта команда доступна администраторам исходного сервера.", ephemeral=True)
        return
    try:
        target_id = int(target_guild_id)
    except ValueError:
        await inter.response.send_message("ID сервера должен быть числом.", ephemeral=True)
        return
    target = bot.get_guild(target_id)
    if target is None:
        await inter.response.send_message("Сервер назначения не найден. Убедись, что бот добавлен на него.", ephemeral=True)
        return
    if target.id == inter.guild.id:
        await inter.response.send_message("Исходный и целевой серверы должны отличаться.", ephemeral=True)
        return
    if not target.me or not target.me.guild_permissions.manage_channels or not target.me.guild_permissions.manage_roles:
        await inter.response.send_message("На целевом сервере боту нужны права Manage Channels и Manage Roles.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True)
    try:
        data = await create_backup(inter.guild, include_messages=include_messages)
        warnings = await restore_structure(target, data, restore_messages=include_messages)
        await inter.followup.send(
            f"✅ Копирование на **{target.name}** завершено.\n"
            f"Создано ролей: {len(data['roles'])}, категорий: {len(data['categories'])}, каналов: {len(data['channels'])}.\n"
            f"Предупреждений: {len(warnings)}.",
            ephemeral=True
        )
        await send_log(inter.guild, "📋 Копирование сервера", f"Автор: {inter.author.mention}\nЦелевой сервер: **{target.name}** (`{target.id}`)")
        await send_log(target, "📋 Копирование сервера", f"Копия перенесена с **{inter.guild.name}** (`{inter.guild.id}`).")
    except Exception as exc:
        log.exception("Clone failed")
        await inter.followup.send(f"❌ Ошибка копирования: `{type(exc).__name__}: {exc}`", ephemeral=True)


@bot.slash_command(name="lockdown", description="Экстренно включить медленный режим в текстовых каналах")
async def lockdown(inter: disnake.ApplicationCommandInteraction, slowmode_seconds: int = commands.Param(default=10, ge=1, le=21600)):
    if not inter.guild or not isinstance(inter.author, disnake.Member) or not inter.author.guild_permissions.administrator:
        await inter.response.send_message("Эта команда доступна администраторам сервера.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True)
    changed = 0
    for channel in inter.guild.text_channels:
        try:
            await channel.edit(slowmode_delay=slowmode_seconds, reason=f"Jarvis Lockdown by {inter.author}")
            changed += 1
            await asyncio.sleep(0.15)
        except (disnake.Forbidden, disnake.HTTPException):
            continue
    await inter.followup.send(f"🔒 Lockdown применён: медленный режим {slowmode_seconds} сек. в **{changed}** каналах.", ephemeral=True)
    await send_log(inter.guild, "🔒 Включён Lockdown", f"Администратор: {inter.author.mention}\nИзменено каналов: `{changed}`")


@bot.slash_command(name="unlockdown", description="Снять медленный режим во всех текстовых каналах")
async def unlockdown(inter: disnake.ApplicationCommandInteraction):
    if not inter.guild or not isinstance(inter.author, disnake.Member) or not inter.author.guild_permissions.administrator:
        await inter.response.send_message("Эта команда доступна администраторам сервера.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True)
    changed = 0
    for channel in inter.guild.text_channels:
        try:
            await channel.edit(slowmode_delay=0, reason=f"Jarvis Unlockdown by {inter.author}")
            changed += 1
            await asyncio.sleep(0.15)
        except (disnake.Forbidden, disnake.HTTPException):
            continue
    await inter.followup.send(f"🔓 Медленный режим снят в **{changed}** каналах.", ephemeral=True)
    await send_log(inter.guild, "🔓 Lockdown снят", f"Администратор: {inter.author.mention}")


@bot.slash_command(name="jarvis_help", description="Показать команды Jarvis | AntiNuke")
async def jarvis_help(inter: disnake.ApplicationCommandInteraction):
    embed = disnake.Embed(title="Jarvis | AntiNuke — команды", color=disnake.Color.blurple())
    embed.description = (
        "`/antinuke status` — статус защиты\n"
        "`/backup_create` — резервная копия структуры\n"
        "`/backup_create include_messages:true` — добавить архив доступной истории\n"
        "`/backup_restore` — восстановить структуру\n"
        "`/backup_restore restore_messages:true` — также перепубликовать архивные сообщения\n"
        "`/server_clone target_guild_id:<ID>` — копия на другой сервер\n"
        "`/lockdown` — включить медленный режим\n"
        "`/unlockdown` — снять медленный режим"
    )
    embed.add_field(name="Важно", value="Для аудита боту нужны View Audit Log, а для восстановления — Manage Roles, Manage Channels и Send Messages.", inline=False)
    await inter.response.send_message(embed=embed, ephemeral=True)


if __name__ == "__main__":
    bot.run(config["token"])
