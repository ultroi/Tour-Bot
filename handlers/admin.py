"""Group-scoped tournament manager and tournament-admin commands."""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from utils import is_admin, is_group_chat, match_line, reply_html_chunks, tournament_summary, user_link

logger = logging.getLogger(__name__)


def _is_global_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    admins = context.application.bot_data["settings"].admin_ids
    return bool(user and is_admin(user.id, admins))


async def _ensure_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not is_group_chat(update):
        await update.effective_message.reply_text("⛔ This command can only be used in a tournament group.")
        return False
    return True


async def _get_active_tournament(update: Update, context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    chat = update.effective_chat
    service = context.application.bot_data["tournament_service"]
    return await service.get_active_tournament(chat.id)


async def _has_group_admin_rights(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if _is_global_admin(update, context):
        return True
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}
    except TelegramError:
        logger.exception("Could not verify group admin status for user %s", user.id)
        return False


async def _has_tournament_access(update: Update, context: ContextTypes.DEFAULT_TYPE, tournament: dict) -> bool:
    user = update.effective_user
    if not user:
        return False
    return (
        _is_global_admin(update, context)
        or int(user.id) == int(tournament.get("manager_id", -1))
        or int(user.id) in {int(item) for item in tournament.get("tournament_admins", [])}
    )


async def _require_tournament_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    if not await _ensure_group(update, context):
        return None
    tournament = await _get_active_tournament(update, context)
    if not tournament:
        await update.effective_message.reply_text("There is no active tournament in this group.")
        return None
    if not await _has_tournament_access(update, context, tournament):
        await update.effective_message.reply_text("⛔ Only this tournament's manager or appointed tournament admins can use this command.")
        return None
    return tournament


async def new_tournament(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Only a Telegram group admin can create the group's active tournament."""
    if not await _ensure_group(update, context):
        return
    if not await _has_group_admin_rights(update, context):
        await update.effective_message.reply_text("⛔ Only a Telegram group admin can create a tournament.")
        return

    raw = (update.effective_message.text or "").split(maxsplit=1)
    if len(raw) != 2:
        await update.effective_message.reply_text(
            "Usage:\n/newtournament Name | knockout/round_robin | [capacity] | [description]\n\n"
            "Example 1: /newtournament College Cup | knockout | 16 | Fun tournament\n"
            "Example 2: /newtournament Weekend Cup | round_robin\n\n"
            "Capacity and description are optional. Leave capacity blank or use 0 for unlimited players.\n"
            "Only one active tournament is allowed per group."
        )
        return

    fields = [part.strip() for part in raw[1].split("|", maxsplit=3)]
    
    # We only strictly require Name and Format (2 fields minimum)
    if len(fields) < 2:
        await update.effective_message.reply_text("Use: Name | knockout/round_robin | [capacity] | [optional description]")
        return

    try:
        # Handle capacity: default to 0 (unlimited) if left blank, '0', 'none', or 'unlimited'
        capacity = 0
        if len(fields) >= 3:
            cap_str = fields[2].lower()
            if cap_str not in ("", "0", "none", "unlimited"):
                capacity = int(cap_str)

        chat = update.effective_chat
        service = context.application.bot_data["tournament_service"]
        tournament = await service.create_tournament(
            name=fields[0],
            tournament_format=fields[1],
            capacity=capacity,  # 0 indicates no limit
            manager_id=update.effective_user.id,
            manager_name=update.effective_user.first_name,
            group_id=chat.id,
            group_title=chat.title or "Telegram Group",
            description=fields[3] if len(fields) == 4 else "",
        )
    except ValueError as exc:
        await update.effective_message.reply_text(f"Could not create tournament: {html.escape(str(exc))}")
        return
    except Exception:
        logger.exception("Tournament creation failed")
        await update.effective_message.reply_text("Something went wrong while creating the tournament.")
        return

    manager = user_link(update.effective_user.id, update.effective_user.first_name)
    await update.effective_message.reply_text(
        "✅ <b>TOURNAMENT CREATED</b>\n\n"
        f"{tournament_summary(tournament)}\n\n"
        f"Tournament Manager: {manager}\n\n"
        "Participants can now use <code>/register</code> in this group.\n"
        "The creator can appoint tournament admins by replying to a member's message with <code>/addtouradmin</code>.",
        parse_mode=ParseMode.HTML,
    )

async def add_tournament_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tournament = await _require_tournament_access(update, context)
    if not tournament:
        return
    if int(update.effective_user.id) != int(tournament["manager_id"]) and not _is_global_admin(update, context):
        await update.effective_message.reply_text("Only the tournament manager can appoint tournament admins.")
        return
    reply = update.effective_message.reply_to_message
    parts = (update.effective_message.text or "").split()
    if reply and reply.from_user and not reply.from_user.is_bot:
        target_id = reply.from_user.id
        target_name = reply.from_user.first_name
    elif len(parts) == 2 and parts[1].isdigit():
        target_id = int(parts[1])
        target_name = "Tournament Admin"
    else:
        await update.effective_message.reply_text(
            "Reply to a member's message with /addtouradmin, or use /addtouradmin USER_ID."
        )
        return
    service = context.application.bot_data["tournament_service"]
    result = await service.add_tournament_admin(tournament["_id"], target_id, target_name)
    messages = {
        "added": f"✅ {user_link(target_id, target_name)} is now a tournament admin.",
        "already_admin": "That member is already a tournament admin.",
        "is_manager": "The tournament manager already has full tournament permissions.",
        "not_found": "Tournament not found.",
    }
    await update.effective_message.reply_text(messages[result], parse_mode=ParseMode.HTML)


async def remove_tournament_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tournament = await _require_tournament_access(update, context)
    if not tournament:
        return
    if int(update.effective_user.id) != int(tournament["manager_id"]) and not _is_global_admin(update, context):
        await update.effective_message.reply_text("Only the tournament manager can remove tournament admins.")
        return
    reply = update.effective_message.reply_to_message
    parts = (update.effective_message.text or "").split()
    if reply and reply.from_user:
        target_id = reply.from_user.id
        target_name = reply.from_user.first_name
    elif len(parts) == 2 and parts[1].isdigit():
        target_id = int(parts[1])
        target_name = "Tournament Admin"
    else:
        await update.effective_message.reply_text(
            "Reply to an admin's message with /rmtouradmin, or use /removetouradmin USER_ID."
        )
        return
    result = await context.application.bot_data["tournament_service"].remove_tournament_admin(
        tournament["_id"], target_id
    )
    if result == "removed":
        text = f"✅ Removed tournament admin access from {user_link(target_id, target_name)}."
    elif result == "not_found":
        text = "Tournament not found."
    else:
        text = "That member is not a tournament admin."
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def set_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tournament = await _require_tournament_access(update, context)
    if not tournament:
        return
    parts = (update.effective_message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        await update.effective_message.reply_text("Usage: /setrules Write the full tournament rules here...")
        return
    if len(parts[1]) > 3500:
        await update.effective_message.reply_text("Rules are too long. Keep them under 3,500 characters.")
        return
    await context.application.bot_data["tournament_service"].set_rules(tournament["_id"], parts[1])
    await update.effective_message.reply_text("✅ Tournament rules updated.")


async def set_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tournament = await _require_tournament_access(update, context)
    if not tournament:
        return
    parts = (update.effective_message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await update.effective_message.reply_text("Usage: /setdescription Write the tournament description...")
        return
    try:
        await context.application.bot_data["tournament_service"].set_description(tournament["_id"], parts[1])
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    await update.effective_message.reply_text("✅ Tournament description updated.")


async def set_tournament_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tournament = await _require_tournament_access(update, context)
    if not tournament:
        return
    command = (update.effective_message.text or "").split(maxsplit=1)[0].split("@", 1)[0].lower()
    status = {"/open": "open", "/close": "closed", "/complete": "completed"}.get(command)
    if status is None:
        await update.effective_message.reply_text("Use /open, /close, or /complete.")
        return
    try:
        found = await context.application.bot_data["tournament_service"].set_status(tournament["_id"], status)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    if found:
        suffix = " The group can now create its next tournament." if status == "completed" else ""
        await update.effective_message.reply_text(f"✅ Tournament status set to {status}.{suffix}")
    else:
        await update.effective_message.reply_text("Tournament not found.")


async def generate_fixtures(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tournament = await _require_tournament_access(update, context)
    if not tournament:
        return
    service = context.application.bot_data["tournament_service"]
    try:
        matches = await service.generate(tournament["_id"], update.effective_chat.id)
    except (LookupError, ValueError) as exc:
        await update.effective_message.reply_text(f"Cannot generate fixtures: {html.escape(str(exc))}")
        return
    except Exception:
        logger.exception("Fixture generation failed")
        await update.effective_message.reply_text("Fixture generation failed. Check the server logs.")
        return
    preview = "\n".join(match_line(item) for item in matches[:12])
    suffix = f"\n…and {len(matches) - 12} more matches." if len(matches) > 12 else ""
    await update.effective_message.reply_text(
        f"✅ Fixtures generated for <b>{html.escape(tournament['name'])}</b>. Registration is now closed.\n\n"
        f"{preview}{suffix}\n\nParticipants can use /fixtures in this group to see the schedule.",
        parse_mode=ParseMode.HTML,
    )


async def set_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tournament = await _require_tournament_access(update, context)
    if not tournament:
        return
    parts = (update.effective_message.text or "").split()
    if len(parts) != 3:
        await update.effective_message.reply_text(
            "Usage: /setresult MATCH_ID WINNER_USER_ID\nExample: /setresult R1-M1 123456789\n"
            "Use /adminparticipants to see player IDs."
        )
        return
    try:
        winner_id = int(parts[2])
    except ValueError:
        await update.effective_message.reply_text("WINNER_USER_ID must be a numeric Telegram user ID.")
        return
    result = await context.application.bot_data["tournament_service"].set_result(
        tournament["_id"], parts[1], winner_id
    )
    messages = {
        "result_saved": "✅ Result saved. The winner has been advanced where applicable.",
        "match_not_found": "Match not found. Check the match ID, for example R1-M1.",
        "match_not_ready": "This match is not ready for a result, or it has already been completed.",
        "winner_not_in_match": "That user ID is not one of the two players in this match.",
    }
    await update.effective_message.reply_text(messages[result])
    if result == "result_saved":
        refreshed = await context.application.bot_data["tournament_service"].get_tournament(tournament["_id"])
        if refreshed and refreshed.get("status") == "completed":
            await update.effective_message.reply_text(
                f"🏆 Tournament complete! Winner: {user_link(winner_id, refreshed.get('winner_name', 'Champion'))}",
                parse_mode=ParseMode.HTML,
            )


async def eliminate_player(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Eliminate a knockout loser by replying to them or providing IDs."""
    tournament = await _require_tournament_access(update, context)
    if not tournament:
        return

    parts = (update.effective_message.text or "").split()
    reply = update.effective_message.reply_to_message
    replied_user = (
        reply.from_user
        if reply and reply.from_user and not reply.from_user.is_bot
        else None
    )

    match_id: str | None = None
    loser_id: int | None = None
    if len(parts) == 1 and replied_user:
        # Reply to the losing player's message with /eleminate.
        loser_id = replied_user.id
    elif len(parts) == 2 and replied_user:
        # Optional explicit match ID while still replying to the loser.
        match_id = parts[1]
        loser_id = replied_user.id
    elif len(parts) == 2 and parts[1].isdigit():
        # Locate the one scheduled knockout match containing this player.
        loser_id = int(parts[1])
    elif len(parts) == 3 and parts[2].isdigit():
        match_id = parts[1]
        loser_id = int(parts[2])
    else:
        await update.effective_message.reply_text(
            "Usage:\n"
            "• Reply to the losing player's message with /eleminate\n"
            "• /eleminate MATCH_ID LOSER_USER_ID\n"
            "• /eleminate LOSER_USER_ID (when they have one scheduled match)\n\n"
            "Use /adminparticipants to see player IDs."
        )
        return

    service = context.application.bot_data["tournament_service"]
    result, details = await service.eliminate(
        tournament["_id"],
        loser_id,
        match_id=match_id,
        eliminated_by=update.effective_user.id,
    )
    messages = {
        "tournament_not_found": "Tournament not found.",
        "not_knockout": "/eleminate is available only for knockout tournaments.",
        "match_not_found": "No scheduled match was found for that player. Check the match ID or player ID.",
        "multiple_matches": "This player appears in more than one scheduled match. Use /eleminate MATCH_ID LOSER_USER_ID.",
        "match_not_ready": "This match is not scheduled or has already been completed.",
        "loser_not_in_match": "The specified losing player is not part of that match.",
        "participant_not_found": "That player is not in this tournament's participant list.",
        "already_eliminated": "That player has already been eliminated.",
        "winner_not_in_match": "The opponent could not be identified as the match winner.",
    }
    if result != "eliminated" or not details:
        await update.effective_message.reply_text(messages.get(result, "Could not eliminate that player."))
        return

    await update.effective_message.reply_text(
        "❌ <b>PLAYER ELIMINATED</b>\n\n"
        f"Eliminated: {user_link(details['loser_id'], details['loser_name'])}\n"
        f"✅ Advanced: {user_link(details['winner_id'], details['winner_name'])}\n"
        f"Match: <code>{html.escape(details['match_id'])}</code>\n\n"
        "The winner has been moved to the next round and the eliminated player has been removed from the active participant list.",
        parse_mode=ParseMode.HTML,
    )

    refreshed = await service.get_tournament(tournament["_id"])
    if refreshed and refreshed.get("status") == "completed":
        await update.effective_message.reply_text(
            f"🏆 Tournament complete! Winner: {user_link(details['winner_id'], details['winner_name'])}",
            parse_mode=ParseMode.HTML,
        )


async def process_due_unpins(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Persistently process announcement unpins, including after bot restarts."""
    db = context.application.bot_data["db"]
    now = datetime.now(timezone.utc)
    due = await db.tournaments.find(
        {"announcement_pin_until": {"$lte": now}, "last_announcement_message_id": {"$ne": None}}
    ).to_list(length=100)
    for tournament in due:
        try:
            await context.bot.unpin_chat_message(
                chat_id=tournament["group_id"],
                message_id=tournament["last_announcement_message_id"],
            )
            logger.info(
                "Auto-unpinned announcement %s in group %s",
                tournament["last_announcement_message_id"], tournament["group_id"],
            )
        except TelegramError as exc:
            logger.info("Could not auto-unpin announcement %s: %s", tournament.get("_id"), exc)
        finally:
            # Clear the due marker even if Telegram reports that the message was
            # already unpinned/deleted or the bot lost pin permission.
            await db.tournaments.update_one(
                {"_id": tournament["_id"], "last_announcement_message_id": tournament["last_announcement_message_id"]},
                {"$set": {"last_announcement_message_id": None, "announcement_pin_until": None}},
            )


async def announce(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tournament = await _require_tournament_access(update, context)
    if not tournament:
        return
    parts = (update.effective_message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        await update.effective_message.reply_text("Usage: /announce Your announcement message...")
        return
    message = parts[1].strip()
    if len(message) > 3500:
        await update.effective_message.reply_text("Announcement is too long. Keep it under 3,500 characters.")
        return

    chat_id = update.effective_chat.id
    db = context.application.bot_data["db"]
    # Keep one bot-managed tournament announcement pinned per group, even when
    # the previous announcement belongs to a now-completed tournament.
    previous_announcements = await db.tournaments.find(
        {"group_id": chat_id, "last_announcement_message_id": {"$ne": None}}
    ).to_list(length=100)
    for previous in previous_announcements:
        try:
            await context.bot.unpin_chat_message(
                chat_id=chat_id, message_id=previous["last_announcement_message_id"]
            )
        except TelegramError:
            pass
        await db.tournaments.update_one(
            {"_id": previous["_id"], "last_announcement_message_id": previous["last_announcement_message_id"]},
            {"$set": {"last_announcement_message_id": None, "announcement_pin_until": None}},
        )

    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=f"📢 ANNOUNCEMENT — {tournament['name']}\n\n{message}",
    )
    try:
        await context.bot.pin_chat_message(chat_id=chat_id, message_id=sent.message_id)
        pin_hours = context.application.bot_data["settings"].announcement_pin_hours
        pin_until = datetime.now(timezone.utc) + timedelta(hours=pin_hours) if pin_hours > 0 else None
        await context.application.bot_data["db"].tournaments.update_one(
            {"_id": tournament["_id"]},
            {"$set": {
                "last_announcement_message_id": sent.message_id,
                "announcement_pin_until": pin_until,
            }},
        )
        await update.effective_message.reply_text(
            f"✅ Announcement posted and pinned. It will be auto-unpinned after {pin_hours} hour(s)."
            if pin_hours > 0 else "✅ Announcement posted and pinned. Automatic unpin is disabled."
        )
    except TelegramError as exc:
        logger.warning("Announcement was sent but pinning failed: %s", exc)
        await update.effective_message.reply_text(
            "Announcement was posted, but I could not pin it. Please give the bot permission to pin messages."
        )


async def unpin_announcement(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tournament = await _require_tournament_access(update, context)
    if not tournament:
        return
    db = context.application.bot_data["db"]
    latest = await db.tournaments.find_one(
        {"group_id": update.effective_chat.id, "last_announcement_message_id": {"$ne": None}},
        sort=[("created_at", -1)],
    )
    if not latest:
        await update.effective_message.reply_text("There is no tracked tournament announcement to unpin.")
        return
    message_id = latest["last_announcement_message_id"]
    try:
        await context.bot.unpin_chat_message(chat_id=update.effective_chat.id, message_id=message_id)
    except TelegramError as exc:
        await update.effective_message.reply_text(f"Could not unpin the announcement: {html.escape(str(exc))}")
        return
    await db.tournaments.update_one(
        {"_id": latest["_id"], "last_announcement_message_id": message_id},
        {"$set": {"last_announcement_message_id": None, "announcement_pin_until": None}},
    )
    await update.effective_message.reply_text("✅ Latest tournament announcement unpinned.")


async def admin_tournaments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_group(update, context):
        return
    tournaments = await context.application.bot_data["tournament_service"].list_tournaments(group_id=update.effective_chat.id)
    if not tournaments:
        await update.effective_message.reply_text("No tournaments have been created in this group yet.")
        return
    lines = ["🛠 <b>TOURNAMENTS IN THIS GROUP</b>"]
    for item in tournaments:
        lines.extend(["", tournament_summary(item, include_description=False)])
    await reply_html_chunks(update.effective_message, lines)


async def admin_participants(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tournament = await _require_tournament_access(update, context)
    if not tournament:
        return
    participants = await context.application.bot_data["tournament_service"].get_participants(tournament["_id"])
    if not participants:
        await update.effective_message.reply_text("No participants are registered yet.")
        return
    lines = [f"🛠 <b>ADMIN PARTICIPANTS — {html.escape(tournament['name'])}</b>"]
    for number, participant in enumerate(participants, start=1):
        lines.append(f"{number}. {user_link(participant['user_id'], participant['name'])} — <code>{participant['user_id']}</code>")
    await reply_html_chunks(update.effective_message, lines)
