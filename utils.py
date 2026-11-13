"""Formatting and Telegram helpers for the tournament bot."""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


def is_admin(user_id: int, admin_ids: frozenset[int]) -> bool:
    return user_id in admin_ids


def is_group_chat(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type in {"group", "supergroup"})


def user_link(user_id: int | None, name: str | None) -> str:
    """Return a safely escaped Telegram mention for a registered player."""
    safe_name = html.escape((name or "Player").strip() or "Player")
    if user_id is None:
        return safe_name
    return f'<a href="tg://user?id={int(user_id)}">{safe_name}</a>'


def capacity_label(tournament: dict[str, Any]) -> str:
    """Render zero/None capacity as unlimited, never as a 0-player limit."""
    raw_capacity = tournament.get("capacity", 0)
    try:
        capacity = int(raw_capacity or 0)
    except (TypeError, ValueError):
        return "Unlimited" if str(raw_capacity).strip().lower() in {"none", "unlimited", "infinite"} else str(raw_capacity)
    return "Unlimited" if capacity <= 0 else str(capacity)


def has_custom_rules(tournament: dict[str, Any]) -> bool:
    rules = str(tournament.get("rules") or "").strip()
    if not rules:
        return False
    # Hide the legacy placeholder that older records stored as if it were rules.
    legacy_placeholders = (
        "rules have not been added yet",
        "no rules added yet",
        "the organiser will update them soon",
        "the tournament manager will update them soon",
    )
    return not any(rules.casefold().startswith(prefix) for prefix in legacy_placeholders)


async def ensure_private_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    db = context.application.bot_data["db"]
    if user and chat and chat.type == "private":
        now = datetime.now(timezone.utc)
        await db.users.update_one(
            {"_id": user.id},
            {
                "$set": {
                    "chat_id": chat.id,
                    "username": user.username,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "last_seen_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )


async def ensure_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    if not chat or chat.type not in {"group", "supergroup"}:
        if update.effective_message:
            await update.effective_message.reply_text(
                "This command works inside the tournament's Telegram group."
            )
        return False
    db = context.application.bot_data["db"]
    await db.groups.update_one(
        {"_id": chat.id},
        {
            "$set": {"title": chat.title or "Telegram Group", "type": chat.type},
            "$setOnInsert": {"created_at": datetime.now(timezone.utc)},
        },
        upsert=True,
    )
    return True


def tournament_summary(tournament: dict[str, Any], include_description: bool = True) -> str:
    name = html.escape(tournament["name"])
    registered = int(tournament.get("registered_count", 0) or 0)
    lines = [
        f"🏆 <b>{name}</b>",
        f"ID: <code>{html.escape(str(tournament['_id']))}</code>",
        f"Format: {html.escape(str(tournament['format']).replace('_', ' ').title())}",
        f"Status: {html.escape(str(tournament['status']).replace('_', ' ').title())}",
        f"Registered: {registered}/{capacity_label(tournament)}",
    ]
    if include_description and tournament.get("description"):
        description = str(tournament["description"]).strip()
        if len(description) > 400:
            description = description[:397] + "..."
        lines.extend(["", "<b>Description</b>", html.escape(description)])
    return "\n".join(lines)


def tournament_deep_link(bot_username: str, payload: str) -> str:
    """Build a Telegram private-chat deep link. Payloads are kept URL-safe."""
    username = (bot_username or "").strip().lstrip("@")
    if not username:
        raise ValueError("The bot username is not available yet.")
    safe_payload = payload.strip()
    if len(safe_payload) > 64 or not all(ch.isalnum() or ch in "_-" for ch in safe_payload):
        raise ValueError("Invalid Telegram start payload.")
    return f"https://t.me/{username}?start={safe_payload}"


def tournament_announcement_text(tournament: dict[str, Any]) -> str:
    """Create the single public tournament post that is kept pinned and edited."""
    name = html.escape(str(tournament.get("name", "Tournament")))
    tournament_id = html.escape(str(tournament.get("_id", "")))
    fmt = html.escape(str(tournament.get("format", "knockout")).replace("_", " ").title())
    status = str(tournament.get("status", "open")).lower()
    status_labels = {
        "open": "🟢 OPEN",
        "closed": "🔴 CLOSED",
        "generating": "🟠 GENERATING FIXTURES",
        "ongoing": "⚔️ IN PROGRESS",
        "completed": "🏆 COMPLETED",
        "cancelled": "❌ CANCELLED",
    }
    registered = int(tournament.get("registered_count", 0) or 0)
    capacity = capacity_label(tournament)
    is_full = capacity != "Unlimited" and registered >= int(capacity)
    if status == "open" and is_full:
        status_text = "🔴 FULL"
    else:
        status_text = status_labels.get(status, html.escape(status.replace("_", " ").upper()))
    manager = user_link(tournament.get("manager_id"), tournament.get("manager_name", "Tournament Manager"))

    lines = [
        "🏆 <b>OFFICIAL TOURNAMENT</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"<b>{name}</b>",
        f"🆔 ID: <code>{tournament_id}</code>",
        f"🎮 Format: <b>{fmt}</b>",
        f"📊 Registered: <b>{registered} / {html.escape(capacity)}</b>",
        f"📌 Registration: <b>{status_text}</b>",
        f"👤 Manager: {manager}",
    ]

    description = str(tournament.get("description") or "").strip()
    if description:
        lines.extend(["", "📝 <b>About this tournament</b>", html.escape(description)])

    if status == "completed" and tournament.get("winner_id"):
        lines.extend(["", f"🏆 Winner: {user_link(tournament.get('winner_id'), tournament.get('winner_name', 'Champion'))}"])

    if tournament.get("fixtures_generated"):
        lines.extend(["", "⚔️ Fixtures have been generated. Check <code>/fixtures</code> for the schedule."])
    elif status == "open" and is_full:
        lines.extend(["", "Registration is full. No additional entries can be accepted unless the manager reopens places."])
    elif status == "open":
        lines.extend(["", "Tap <b>Register in DM</b>, then press <b>Start</b> in the bot chat. Your registration will be completed automatically."])
    elif status == "closed":
        lines.extend(["", "Registration is currently paused by the tournament team."])
    elif status == "cancelled":
        lines.extend(["", "This tournament has been cancelled."])
    elif status == "completed":
        lines.extend(["", "This tournament has been completed. Thank you to everyone who participated!"])

    lines.extend(["", "━━━━━━━━━━━━━━━━━━", "<i>This message updates automatically when tournament details change.</i>"])
    return "\n".join(lines)


def tournament_announcement_keyboard(tournament: dict[str, Any], bot_username: str) -> InlineKeyboardMarkup | None:
    """Build the public controls. Admin actions are authorized again on callback."""
    tournament_id = str(tournament["_id"])
    group_id = int(tournament["group_id"])
    status = str(tournament.get("status", "open")).lower()
    is_active = bool(tournament.get("is_active", False))
    rows: list[list[InlineKeyboardButton]] = []

    registered = int(tournament.get("registered_count", 0) or 0)
    raw_capacity = tournament.get("capacity", 0)
    try:
        capacity = int(raw_capacity or 0)
    except (TypeError, ValueError):
        capacity = 0
    is_full = capacity > 0 and registered >= capacity

    if status == "open" and is_active and not is_full:
        register_url = tournament_deep_link(bot_username, f"treg_{group_id}_{tournament_id}")
        rows.append([InlineKeyboardButton("✅ Register Now", callback_data=f"tour:register:{tournament_id}")])
    elif status == "open" and is_active and is_full:
        rows.append([InlineKeyboardButton("🔴 Registration Full", callback_data=f"tour:status:{tournament_id}")])
    elif status in {"closed", "generating", "ongoing"} and is_active:
        rows.append([InlineKeyboardButton("🔒 Registration Closed", callback_data=f"tour:status:{tournament_id}")])

    if has_custom_rules(tournament):
        rules_url = tournament_deep_link(bot_username, f"trules_{tournament_id}")
        rows.append([InlineKeyboardButton("📜 Tournament Rules", url=rules_url)])

    if is_active and status in {"open", "closed"} and not tournament.get("fixtures_generated"):
        toggle_label = "🔒 Turn Registration OFF" if status == "open" else "🟢 Turn Registration ON"
        rows.append([InlineKeyboardButton(toggle_label, callback_data=f"tour:toggle:{tournament_id}")])

    if is_active and status not in {"completed", "cancelled"}:
        rows.append([InlineKeyboardButton("❌ Cancel Tournament", callback_data=f"tour:cancel:{tournament_id}")])

    return InlineKeyboardMarkup(rows) if rows else None


async def refresh_tournament_announcement(
    context: ContextTypes.DEFAULT_TYPE,
    tournament_id: str,
    *,
    create_if_missing: bool = False,
) -> bool:
    """Create or edit the one tracked tournament post, and ensure it is pinned."""
    service = context.application.bot_data["tournament_service"]
    db = context.application.bot_data["db"]
    tournament = await service.get_tournament(tournament_id)
    if not tournament:
        return False

    bot_username = context.application.bot_data.get("bot_username")
    if not bot_username:
        me = await context.bot.get_me()
        bot_username = me.username
        context.application.bot_data["bot_username"] = bot_username
    if not bot_username:
        logger.error("Cannot create tournament deep links: bot username is unavailable")
        return False

    chat_id = int(tournament["group_id"])
    message_id = tournament.get("tournament_message_id")
    text = tournament_announcement_text(tournament)
    markup = tournament_announcement_keyboard(tournament, bot_username)
    broken_existing_message = False

    if message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(message_id),
                text=text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                if "message to edit not found" not in str(exc).lower() and "message can't be edited" not in str(exc).lower():
                    logger.warning("Could not edit tournament announcement %s: %s", tournament_id, exc)
                    return False
                message_id = None
                broken_existing_message = True
        except TelegramError:
            logger.exception("Could not refresh tournament announcement %s", tournament_id)
            return False

    if not message_id:
        if not create_if_missing and not broken_existing_message:
            return False
        if broken_existing_message:
            # If the previous post is still present but Telegram no longer lets
            # the bot edit it, remove its pin before replacing it.
            old_message_id = tournament.get("tournament_message_id")
            if old_message_id:
                try:
                    await context.bot.unpin_chat_message(
                        chat_id=chat_id,
                        message_id=int(old_message_id),
                    )
                except TelegramError:
                    pass
        # Unpin previous bot-managed tournament posts in this group; keep them in history.
        previous_posts = await db.tournaments.find(
            {
                "group_id": chat_id,
                "_id": {"$ne": tournament_id},
                "tournament_message_id": {"$ne": None},
                "tournament_message_pinned": True,
            }
        ).to_list(length=100)
        for previous in previous_posts:
            try:
                await context.bot.unpin_chat_message(
                    chat_id=chat_id,
                    message_id=int(previous["tournament_message_id"]),
                )
            except TelegramError as exc:
                logger.info("Could not unpin previous tournament post %s: %s", previous.get("_id"), exc)
            await db.tournaments.update_one(
                {"_id": previous["_id"]},
                {"$set": {"tournament_message_pinned": False}},
            )

        try:
            sent = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except TelegramError:
            logger.exception("Could not send tournament announcement %s", tournament_id)
            return False
        message_id = sent.message_id
        await db.tournaments.update_one(
            {"_id": tournament_id},
            {"$set": {
                "tournament_message_id": int(message_id),
                "tournament_message_chat_id": chat_id,
                "tournament_message_pinned": False,
            }},
        )

    # Re-pin on every refresh: an organiser may have manually unpinned the post
    # since the previous edit. Telegram accepts pinning an already-pinned post.
    try:
        await context.bot.pin_chat_message(
            chat_id=chat_id,
            message_id=int(message_id),
            disable_notification=True,
        )
        await db.tournaments.update_one(
            {"_id": tournament_id},
            {"$set": {"tournament_message_pinned": True}},
        )
    except TelegramError as exc:
        logger.warning("Tournament post %s was sent/updated but could not be pinned: %s", tournament_id, exc)
        await db.tournaments.update_one(
            {"_id": tournament_id},
            {"$set": {"tournament_message_pinned": False}},
        )
        return False
    return True


async def reply_html_chunks(message: Any, lines: list[str], limit: int = 3800) -> None:
    """Send HTML-safe message fragments without cutting tags or links in half."""
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in lines:
        safe_line = line
        if len(safe_line) > limit:
            safe_line = safe_line[: limit - 32] + "... (truncated)"
        addition = len(safe_line) + (1 if current else 0)
        if current and current_length + addition > limit:
            chunks.append("\n".join(current))
            current = []
            current_length = 0
        current.append(safe_line)
        current_length += len(safe_line) + (1 if len(current) > 1 else 0)
    if current:
        chunks.append("\n".join(current))
    for chunk in chunks:
        await message.reply_text(chunk, parse_mode="HTML")


def match_line(match: dict[str, Any]) -> str:
    match_id = html.escape(match["match_id"])
    first = user_link(match.get("player1_id"), match.get("player1_name") or "TBD")
    second = user_link(match.get("player2_id"), match.get("player2_name") or "TBD")
    status = match.get("status", "waiting")
    if status == "bye":
        return f"<code>{match_id}</code>: {first} — BYE (advances)"
    if status == "completed":
        winner = user_link(match.get("winner_id"), match.get("winner_name", "Unknown"))
        eliminated_id = match.get("eliminated_id")
        if eliminated_id is not None:
            eliminated = user_link(eliminated_id, match.get("eliminated_name", "Player"))
            return f"<code>{match_id}</code>: {first} vs {second} — Winner: {winner} ✅ — Eliminated: {eliminated} ❌"
        return f"<code>{match_id}</code>: {first} vs {second} — Winner: {winner} ✅"
    if status == "waiting":
        return f"<code>{match_id}</code>: {first} vs {second} — Waiting for previous results"
    return f"<code>{match_id}</code>: {first} vs {second} — Scheduled"


# ---------------------------------------------------------------------------
# Public, round-by-round fixture messages
# ---------------------------------------------------------------------------

def fixture_round_title(tournament: dict[str, Any], round_number: int, matches: list[dict[str, Any]]) -> str:
    if tournament.get("format") == "round_robin":
        return f"ROUND {round_number}"
    total_rounds = max((int(item.get("round", 1)) for item in matches), default=round_number)
    remaining = total_rounds - round_number
    if remaining <= 0:
        return "GRAND FINAL"
    if remaining == 1:
        return "SEMI-FINALS"
    if remaining == 2:
        return "QUARTER-FINALS"
    return f"ROUND OF {2 ** (remaining + 1)}"


def fixture_round_text(
    tournament: dict[str, Any],
    matches: list[dict[str, Any]],
    round_number: int,
) -> str:
    """Build one Telegram-safe message for one tournament round."""
    all_rounds = sorted({int(item.get("round", 1)) for item in matches})
    round_matches = [item for item in matches if int(item.get("round", 1)) == round_number]
    title = fixture_round_title(tournament, round_number, matches)
    total = len(all_rounds)
    page = all_rounds.index(round_number) + 1 if round_number in all_rounds else 1
    lines = [
        "🏆 <b>OFFICIAL FIXTURES</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"<b>{html.escape(str(tournament.get('name', 'Tournament')))}</b>",
        f"🆔 Tournament: <code>{html.escape(str(tournament.get('_id', '')))}</code>",
        f"🎮 Format: <b>{html.escape(str(tournament.get('format', '')).replace('_', ' ').title())}</b>",
        f"📍 <b>{html.escape(title)}</b>  ·  Round {round_number}/{total}",
        "━━━━━━━━━━━━━━━━━━",
        "",
    ]
    for match in round_matches:
        match_id = html.escape(str(match.get("match_id", "MATCH")))
        status = str(match.get("status", "waiting")).lower()
        first_id, second_id = match.get("player1_id"), match.get("player2_id")
        first_name = match.get("player1_name") or ("TBD" if status == "waiting" else "Player")
        second_name = match.get("player2_name") or ("TBD" if status == "waiting" else "Player")
        first = user_link(first_id, first_name)
        second = user_link(second_id, second_name)
        lines.append(f"⚔️ <b>{match_id}</b>")
        if first_id is not None:
            lines.append(f"   🔵 {first}  <code>{int(first_id)}</code>")
        else:
            lines.append(f"   🔵 {html.escape(str(first_name))}")
        if status == "bye":
            lines.append("   🟡 <b>BYE — advances automatically</b>")
        else:
            lines.append("   <b>VS</b>")
            if second_id is not None:
                lines.append(f"   🔴 {second}  <code>{int(second_id)}</code>")
            else:
                lines.append(f"   🔴 {html.escape(str(second_name))}")
        if status == "completed":
            winner = user_link(match.get("winner_id"), match.get("winner_name", "Winner"))
            lines.append(f"   ✅ <b>Completed</b> · Winner: {winner}")
            if match.get("eliminated_id") is not None:
                lines.append(
                    f"   ❌ Eliminated: {user_link(match.get('eliminated_id'), match.get('eliminated_name', 'Player'))}"
                )
        elif status == "scheduled":
            lines.append("   🟢 <b>Scheduled</b>")
        elif status == "waiting":
            lines.append("   ⏳ <b>Waiting for previous round</b>")
        elif status == "cancelled":
            lines.append("   🚫 <b>Cancelled</b>")
        elif status == "bye":
            lines.append("   🏁 <b>Qualified</b>")
        lines.append("")
    lines.extend([
        "━━━━━━━━━━━━━━━━━━",
        "<i>Use Previous / Next to view other rounds. Results update this schedule automatically.</i>",
    ])
    return "\n".join(lines)


def fixture_round_keyboard(tournament_id: str, round_numbers: list[int], current_round: int) -> InlineKeyboardMarkup | None:
    index = round_numbers.index(current_round) if current_round in round_numbers else 0
    previous_round = round_numbers[index - 1] if index > 0 else None
    next_round = round_numbers[index + 1] if index + 1 < len(round_numbers) else None
    row: list[InlineKeyboardButton] = []
    if previous_round is not None:
        row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"fxp:{tournament_id}:{previous_round}"))
    if next_round is not None:
        row.append(InlineKeyboardButton("Next ➡️", callback_data=f"fxp:{tournament_id}:{next_round}"))
    return InlineKeyboardMarkup([row]) if row else None


async def refresh_published_fixture_messages(
    context: ContextTypes.DEFAULT_TYPE,
    tournament_id: str,
) -> None:
    """Refresh every stored round message after a result/advancement changes."""
    service = context.application.bot_data["tournament_service"]
    tournament = await service.get_tournament(tournament_id)
    if not tournament:
        return
    message_ids = tournament.get("fixture_round_message_ids") or {}
    if not message_ids:
        return
    matches = await service.get_matches(tournament_id)
    rounds = sorted({int(item.get("round", 1)) for item in matches})
    if not rounds:
        return
    for round_text, message_id in message_ids.items():
        try:
            round_number = int(round_text)
            text = fixture_round_text(tournament, matches, round_number)
            markup = fixture_round_keyboard(tournament_id, rounds, round_number)
            await context.bot.edit_message_text(
                chat_id=int(tournament["group_id"]),
                message_id=int(message_id),
                text=text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logger.warning("Could not refresh fixture round %s for %s: %s", round_text, tournament_id, exc)
        except TelegramError:
            logger.exception("Could not refresh published fixtures for tournament %s", tournament_id)
