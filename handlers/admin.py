"""Group-scoped tournament manager and tournament-admin commands."""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes
from services.fixture_service import generate_fixtures

from utils import (
    is_admin, is_group_chat, match_line, refresh_tournament_announcement,
    reply_html_chunks, tournament_summary, user_link, fixture_round_text,
    fixture_round_keyboard, refresh_published_fixture_messages,
)

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
        # Capacity 0 is the database representation of unlimited registrations.
        capacity = 0
        if len(fields) >= 3:
            cap_str = fields[2].strip().casefold()
            if cap_str not in ("", "0", "none", "unlimited", "no limit", "infinite"):
                capacity = int(cap_str)

        chat = update.effective_chat
        service = context.application.bot_data["tournament_service"]
        tournament = await service.create_tournament(
            name=fields[0],
            tournament_format=fields[1],
            capacity=capacity,
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

    # Create and pin the single public tournament card, then remove the command
    # so the group is left with the polished interactive announcement.
    try:
        posted = await refresh_tournament_announcement(
            context, tournament["_id"], create_if_missing=True
        )
    except TelegramError:
        logger.exception("Could not send or pin the tournament announcement")
        posted = False
    try:
        await update.effective_message.delete()
    except TelegramError as exc:
        logger.info("Could not delete /newtournament command message: %s", exc)

    if not posted:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                "Tournament created, but I could not publish its interactive pinned post. "
                "Please check that I can send and pin messages in this group."
            ),
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
    await refresh_tournament_announcement(context, tournament["_id"])
    await update.effective_message.reply_text("✅ Tournament rules updated. The pinned tournament post has been refreshed.")


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
    await refresh_tournament_announcement(context, tournament["_id"])
    await update.effective_message.reply_text("✅ Tournament description updated. The pinned tournament post has been refreshed.")


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
        await refresh_tournament_announcement(context, tournament["_id"])
        suffix = " The group can now create its next tournament." if status == "completed" else ""
        await update.effective_message.reply_text(f"✅ Tournament status set to {status}.{suffix}")
    else:
        await update.effective_message.reply_text("Tournament not found.")


async def generate_fixtures(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate fixtures, then open the private admin draft editor."""
    tournament = await _resolve_fixture_admin_tournament(update, context)
    if not tournament:
        return
    service = context.application.bot_data["tournament_service"]
    try:
        matches = await service.generate(tournament["_id"], int(tournament["group_id"]))
    except (LookupError, ValueError) as exc:
        await update.effective_message.reply_text(f"Cannot generate fixtures: {html.escape(str(exc))}")
        return
    except Exception:
        logger.exception("Fixture generation failed")
        await update.effective_message.reply_text("Fixture generation failed. Check the server logs.")
        return
    await _start_fixture_editor(update, context, tournament, matches)


async def set_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tournament = await _require_tournament_access(update, context)
    if not tournament:
        return
    if tournament.get("fixtures_finalized") is False:
        await update.effective_message.reply_text("Finalize the fixture draft with Done before recording match results.")
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
        await refresh_tournament_announcement(context, tournament["_id"])
        await refresh_published_fixture_messages(context, tournament["_id"])
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
    if tournament.get("fixtures_finalized") is False:
        await update.effective_message.reply_text("Finalize the fixture draft with Done before recording match results.")
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
            "• /eleminate LOSER_USER_ID\n\n"
            "Knockout: specify MATCH_ID when needed. Round robin: this disqualifies the player and cancels their pending fixtures.\n"
            "Use /adminparticipants to see player IDs."
        )
        return

    service = context.application.bot_data["tournament_service"]
    if tournament.get("format") == "round_robin":
        if match_id:
            await update.effective_message.reply_text(
                "For round-robin disqualification, use /eleminate USER_ID or reply to the player's message with /eleminate."
            )
            return
        result, details = await service.disqualify_round_robin(
            tournament["_id"], loser_id, disqualified_by=update.effective_user.id
        )
        rr_messages = {
            "tournament_not_found": "Tournament not found.",
            "not_round_robin": "This command is not available for this tournament format.",
            "fixtures_not_generated": "Generate fixtures before disqualifying a round-robin participant.",
            "participant_not_found": "That player is not in this tournament's participant list.",
            "already_disqualified": "That player has already been disqualified.",
        }
        if result != "disqualified" or not details:
            await update.effective_message.reply_text(rr_messages.get(result, "Could not disqualify that player."))
            return
        cancelled = details["cancelled_matches"]
        await refresh_tournament_announcement(context, tournament["_id"])
        await refresh_published_fixture_messages(context, tournament["_id"])
        await update.effective_message.reply_text(
            "🚫 <b>PLAYER DISQUALIFIED</b>\n\n"
            f"Player: {user_link(details['user_id'], details['name'])}\n"
            f"Pending fixtures cancelled: <b>{cancelled}</b>\n\n"
            "The player has been removed from active participants. Their completed match records are retained, "
            "but results involving them are excluded from the standings. Cancelled fixtures award no points.",
            parse_mode=ParseMode.HTML,
        )
        return

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

    await refresh_tournament_announcement(context, tournament["_id"])
    await refresh_published_fixture_messages(context, tournament["_id"])
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


async def tournament_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    parts = query.data.split(":")
    if len(parts) != 3 or parts[0] != "tour":
        await query.answer("Invalid tournament action.", show_alert=True)
        return

    action, tournament_id = parts[1], parts[2].strip().upper()
    service = context.application.bot_data["tournament_service"]
    tournament = await service.get_tournament(tournament_id)
    if not tournament:
        await query.answer("This tournament could not be found.", show_alert=True)
        return

    message = query.message
    if (
        not message
        or int(message.chat.id) != int(tournament.get("group_id", 0))
        or int(message.message_id) != int(tournament.get("tournament_message_id", -1))
    ):
        await query.answer("This is not the current tournament control message.", show_alert=True)
        return

    if action == "status":
        registered = int(tournament.get("registered_count", 0) or 0)
        capacity = int(tournament.get("capacity", 0) or 0)
        if capacity > 0 and registered >= capacity:
            await query.answer("Registration is full. Contact the tournament manager if a place opens.", show_alert=True)
        else:
            await query.answer("Registration is currently closed.", show_alert=True)
        return

    # --- NEW BLOCK: INSTANT REGISTRATION ---
    if action == "register":
        # 1. Check if registration is open
        if str(tournament.get("status", "")).lower() != "open":
            await query.answer("Registration is currently closed.", show_alert=True)
            return

        # 2. Check if user already exists in the bot's database (has started the bot)
        db = context.application.bot_data["db"]
        user_doc = await db.users.find_one({"_id": query.from_user.id})
        
        if not user_doc:
            # User hasn't started the bot in DM yet. Seamlessly redirect them to DM.
            bot_username = context.bot.username
            deep_link = f"https://t.me/{bot_username}?start=treg_{message.chat.id}_{tournament_id}"
            await query.answer("Please start the bot in DM first!", url=deep_link)
            return

        result = await service.register(
            tournament_id, message.chat.id, query.from_user.id, 
            query.from_user.first_name, query.from_user.username
        )
        
        messages = {
            "registered": "✅ You have been registered successfully!",
            "already_registered": "You are already registered for this tournament.",
            "closed": "Registration is closed.",
            "full": "The tournament is full."
        }
        
        # Show instant popup message (toast) in the group
        await query.answer(messages.get(result, "Registration processed."), show_alert=True)
        
        # Update the pinned message so the player count increments immediately
        if result == "registered":
            await refresh_tournament_announcement(context, tournament_id)
            
        return
    
    if not await _has_tournament_access(update, context, tournament):
        await query.answer(
            "⛔ Only the tournament manager or appointed tournament admins can use this control.",
            show_alert=True,
        )
        return

    if not tournament.get("is_active", False):
        await query.answer("This tournament is no longer active.", show_alert=True)
        return

    if action == "toggle":
        if tournament.get("fixtures_generated") or tournament.get("status") not in {"open", "closed"}:
            await query.answer("Registration cannot be toggled after fixtures are generated.", show_alert=True)
            return
        new_status = "closed" if tournament.get("status") == "open" else "open"
        try:
            changed = await service.set_status(tournament_id, new_status)
        except ValueError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        if not changed:
            await query.answer("Tournament not found.", show_alert=True)
            return
        await refresh_tournament_announcement(context, tournament_id)
        await query.answer(f"Registration turned {'OFF' if new_status == 'closed' else 'ON'}.")
        return

    if action == "cancel":
        confirm_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirm Cancel", callback_data=f"tour:cancelconfirm:{tournament_id}"),
                InlineKeyboardButton("↩️ Keep Tournament", callback_data=f"tour:cancelabort:{tournament_id}"),
            ]
        ])
        try:
            await query.edit_message_reply_markup(reply_markup=confirm_markup)
        except TelegramError as exc:
            logger.warning("Could not show tournament cancellation confirmation: %s", exc)
        await query.answer("Confirm cancellation using the button shown below.", show_alert=True)
        return

    if action == "cancelconfirm":
        try:
            changed = await service.set_status(tournament_id, "cancelled")
        except ValueError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        if not changed:
            await query.answer("Tournament not found.", show_alert=True)
            return
        await refresh_tournament_announcement(context, tournament_id)
        await query.answer("Tournament cancelled.", show_alert=True)
        return

    if action == "cancelabort":
        await refresh_tournament_announcement(context, tournament_id)
        await query.answer("Cancellation dismissed.")
        return

    await query.answer("Unknown tournament action.", show_alert=True)


# ---------------------------------------------------------------------------
# Fixture editor (available in the tournament group or in DM)
# ---------------------------------------------------------------------------

FIXTURE_EDITOR_PAGE_SIZE = 4
FIXTURE_PLAYER_PAGE_SIZE = 8


async def _resolve_fixture_admin_tournament(update: Update, context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    """Group: active tournament. DM: explicit tournament ID."""
    service = context.application.bot_data["tournament_service"]
    parts = (update.effective_message.text or "").split(maxsplit=1)
    requested_id = parts[1].strip().upper() if len(parts) == 2 else None
    if is_group_chat(update):
        if requested_id:
            tournament = await service.get_tournament(requested_id)
            if tournament and int(tournament.get("group_id", 0)) != int(update.effective_chat.id):
                tournament = None
        else:
            tournament = await service.get_active_tournament(update.effective_chat.id)
    else:
        if not requested_id:
            await update.effective_message.reply_text(
                "In DM, provide the tournament ID:\n/editfixtures TOURNAMENT_ID\n"
                "or /generatefixtures TOURNAMENT_ID"
            )
            return None
        tournament = await service.get_tournament(requested_id)
    if not tournament:
        await update.effective_message.reply_text("Tournament not found. Check the tournament ID or active group tournament.")
        return None
    if not await _has_tournament_access(update, context, tournament):
        await update.effective_message.reply_text(
            "⛔ Only this tournament's manager or appointed tournament admins can edit fixtures."
        )
        return None
    if not tournament.get("fixtures_generated"):
        await update.effective_message.reply_text("Generate fixtures first using /generatefixtures.")
        return None
    completed = await context.application.bot_data["db"].matches.count_documents({
        "tournament_id": tournament["_id"], "status": "completed"
    })
    if completed:
        await update.effective_message.reply_text(
            "Fixtures are locked because match results have already been recorded. "
            "This prevents changing completed matches or corrupting bracket progression."
        )
        return None
    return tournament


async def edit_fixtures(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tournament = await _resolve_fixture_admin_tournament(update, context)
    if not tournament:
        return
    matches = await context.application.bot_data["tournament_service"].get_matches(tournament["_id"])
    if not matches:
        await update.effective_message.reply_text("No generated fixtures were found.")
        return
    await _start_fixture_editor(update, context, tournament, matches)


async def _start_fixture_editor(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    tournament: dict,
    matches: list[dict],
) -> None:
    # Strip Mongo-specific data and keep a per-admin draft until Done is pressed.
    draft = [dict(item) for item in matches]
    context.user_data["fixture_editor"] = {
        "tournament_id": tournament["_id"],
        "format": tournament["format"],
        "group_id": int(tournament["group_id"]),
        "matches": draft,
        "participants": await context.application.bot_data["tournament_service"].get_participants(tournament["_id"]),
        "page": 0,
        "selected_match": None,
        "selected_slot": None,
        "player_page": 0,
        "editor_chat_id": int(update.effective_chat.id),
    }
    await _render_fixture_editor(update, context)


def _fixture_editor_markup(state: dict) -> InlineKeyboardMarkup:
    matches = state["matches"]
    page = int(state.get("page", 0))
    start = page * FIXTURE_EDITOR_PAGE_SIZE
    page_matches = matches[start:start + FIXTURE_EDITOR_PAGE_SIZE]
    rows: list[list[InlineKeyboardButton]] = []
    for offset, match in enumerate(page_matches):
        index = start + offset
        if state.get("format") == "knockout" and int(match.get("round", 1)) > 1:
            rows.append([InlineKeyboardButton(f"🔒 {match['match_id']} · Winner-fed", callback_data=f"fxe:locked:{index}")])
        else:
            rows.append([
                InlineKeyboardButton(f"✏️ Edit {match['match_id']}", callback_data=f"fxe:edit:{index}"),
                InlineKeyboardButton("🔀 Swap", callback_data=f"fxe:swap:{index}"),
            ])
    total_pages = max(1, (len(matches) + FIXTURE_EDITOR_PAGE_SIZE - 1) // FIXTURE_EDITOR_PAGE_SIZE)
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"fxe:page:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"fxe:page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton("♻️ Regenerate Fixture", callback_data="fxe:regen"),
        InlineKeyboardButton("✅ Done", callback_data="fxe:done"),
    ])
    rows.append([InlineKeyboardButton("✖️ Cancel", callback_data="fxe:cancel")])
    return InlineKeyboardMarkup(rows)


def _fixture_editor_text(tournament: dict, state: dict) -> str:
    matches = state["matches"]
    page = int(state.get("page", 0))
    start = page * FIXTURE_EDITOR_PAGE_SIZE
    total_pages = max(1, (len(matches) + FIXTURE_EDITOR_PAGE_SIZE - 1) // FIXTURE_EDITOR_PAGE_SIZE)
    lines = [
        "🛠 <b>FIXTURE EDITOR — DRAFT</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"🏆 <b>{html.escape(tournament['name'])}</b>",
        f"🎮 Format: <b>{html.escape(tournament['format'].replace('_', ' ').title())}</b>",
        f"📋 Matches: <b>{len(matches)}</b> · Page {page + 1}/{total_pages}",
        "",
        "<i>Choose Edit to change either player. Swap exchanges the two players in that match. "
        "Changes remain private until you press Done.</i>",
        "",
    ]
    for index, match in enumerate(matches[start:start + FIXTURE_EDITOR_PAGE_SIZE], start=start + 1):
        lines.append(f"<b>{index}. {html.escape(match['match_id'])}</b>")
        lines.append(match_line(match))
        lines.append("")
    lines.extend([
        "♻️ <b>Regenerate Fixture</b> rebuilds the draft from the current active participant list.",
        "✅ <b>Done</b> saves the draft and publishes one message per round in the tournament group.",
    ])
    return "\n".join(lines)


async def _render_fixture_editor(target, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data.get("fixture_editor")
    if not state:
        return
    tournament = await context.application.bot_data["tournament_service"].get_tournament(state["tournament_id"])
    if not tournament:
        return
    text = _fixture_editor_text(tournament, state)
    markup = _fixture_editor_markup(state)
    if hasattr(target, "callback_query") and target.callback_query:
        query = target.callback_query
        await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    elif hasattr(target, "effective_message") and target.effective_message:
        await target.effective_message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def _render_fixture_match_menu(query, state: dict) -> None:
    index = int(state["selected_match"])
    match = state["matches"][index]
    text = (
        "✏️ <b>EDIT MATCH</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"Match: <code>{html.escape(match['match_id'])}</code>\n"
        f"Round: <b>{match['round']}</b>\n\n"
        f"Player 1: {user_link(match.get('player1_id'), match.get('player1_name') or 'TBD')}\n"
        f"Player 2: {user_link(match.get('player2_id'), match.get('player2_name') or 'TBD')}\n\n"
        "Choose a slot to replace from the registered participant list, or swap the two players."
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👤 Edit Player 1", callback_data=f"fxe:slot:{index}:1"),
            InlineKeyboardButton("👤 Edit Player 2", callback_data=f"fxe:slot:{index}:2"),
        ],
        [InlineKeyboardButton("🔀 Swap Player 1 ↔ Player 2", callback_data=f"fxe:swap:{index}")],
        [
            InlineKeyboardButton("↔️ Swap P1 with another match", callback_data=f"fxe:crossstart:{index}:1"),
            InlineKeyboardButton("↔️ Swap P2 with another match", callback_data=f"fxe:crossstart:{index}:2"),
        ],
        [InlineKeyboardButton("↩️ Back to Fixtures", callback_data="fxe:back")],
    ])
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def _render_fixture_player_picker(query, state: dict) -> None:
    index = int(state["selected_match"])
    slot = int(state["selected_slot"])
    participants = state.get("participants", [])
    page = int(state.get("player_page", 0))
    start = page * FIXTURE_PLAYER_PAGE_SIZE
    selected = state["matches"][index].get(f"player{slot}_id")
    rows: list[list[InlineKeyboardButton]] = []
    for participant in participants[start:start + FIXTURE_PLAYER_PAGE_SIZE]:
        uid = int(participant["user_id"])
        marker = "✓ " if uid == selected else ""
        rows.append([InlineKeyboardButton(
            f"{marker}{participant['name'][:35]} · {uid}",
            callback_data=f"fxe:choose:{uid}",
        )])
    total_pages = max(1, (len(participants) + FIXTURE_PLAYER_PAGE_SIZE - 1) // FIXTURE_PLAYER_PAGE_SIZE)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"fxe:pickpage:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"fxe:pickpage:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("↩️ Back to Match", callback_data="fxe:matchback")])
    await query.edit_message_text(
        f"👥 <b>SELECT PLAYER {slot}</b>\n"
        f"Match: <code>{html.escape(state['matches'][index]['match_id'])}</code>\n"
        f"Players {page + 1}/{total_pages}. Select a registered participant:",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
    )


async def fixture_editor_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    state = context.user_data.get("fixture_editor")
    if not state or not query or not query.data:
        await query.answer("This editor session has expired. Run /editfixtures again.", show_alert=True)
        return
    service = context.application.bot_data["tournament_service"]
    tournament = await service.get_tournament(state["tournament_id"])
    if not tournament or not await _has_tournament_access(update, context, tournament):
        await query.answer("Only this tournament's admins can edit this draft.", show_alert=True)
        return
    if int(query.message.chat.id) != int(state["editor_chat_id"]):
        await query.answer("Open the editor from the same chat where it was started.", show_alert=True)
        return
    completed = await context.application.bot_data["db"].matches.count_documents({
        "tournament_id": tournament["_id"], "status": "completed"
    })
    if completed:
        await query.answer("Editing is locked after the first result.", show_alert=True)
        context.user_data.pop("fixture_editor", None)
        return

    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "page":
        state["page"] = max(0, int(parts[2]))
        await query.answer()
        await _render_fixture_editor(update, context)
        return
    if action == "locked":
        await query.answer("This knockout match is filled automatically by previous-round winners.", show_alert=True)
        return
    if action == "edit":
        if state.get("format") == "knockout" and int(state["matches"][int(parts[2])].get("round", 1)) > 1:
            await query.answer("Later knockout rounds are filled automatically by match winners.", show_alert=True)
            return
        state["selected_match"] = int(parts[2])
        await query.answer()
        await _render_fixture_match_menu(query, state)
        return
    if action == "back":
        state["selected_match"] = None
        await query.answer()
        await _render_fixture_editor(update, context)
        return
    if action == "matchback":
        await query.answer()
        await _render_fixture_match_menu(query, state)
        return
    if action == "slot":
        if state.get("format") == "knockout" and int(state["matches"][int(parts[2])].get("round", 1)) > 1:
            await query.answer("Later knockout rounds are filled automatically by match winners.", show_alert=True)
            return
        state["selected_match"] = int(parts[2])
        state["selected_slot"] = int(parts[3])
        state["player_page"] = 0
        await query.answer()
        await _render_fixture_player_picker(query, state)
        return
    if action == "pickpage":
        state["player_page"] = max(0, int(parts[2]))
        await query.answer()
        await _render_fixture_player_picker(query, state)
        return
    if action == "choose":
        uid = int(parts[2])
        participant = next((p for p in state["participants"] if int(p["user_id"]) == uid), None)
        if not participant:
            await query.answer("Player is not in the active participant list.", show_alert=True)
            return
        index = int(state["selected_match"])
        slot = int(state["selected_slot"])
        match = state["matches"][index]
        match[f"player{slot}_id"] = uid
        match[f"player{slot}_name"] = participant["name"]
        _recalculate_draft_match_status(match, tournament)
        _rebuild_knockout_bye_slots(state["matches"], tournament)
        await query.answer("Player updated in the draft.")
        await _render_fixture_match_menu(query, state)
        return
    if action == "swap":
        index = int(parts[2])
        if state.get("format") == "knockout" and int(state["matches"][index].get("round", 1)) > 1:
            await query.answer("Later knockout rounds are filled automatically by match winners.", show_alert=True)
            return
        match = state["matches"][index]
        for key in ("id", "name"):
            left, right = match.get(f"player1_{key}"), match.get(f"player2_{key}")
            match[f"player1_{key}"], match[f"player2_{key}"] = right, left
        _recalculate_draft_match_status(match, tournament)
        _rebuild_knockout_bye_slots(state["matches"], tournament)
        state["selected_match"] = index
        await query.answer("Players swapped in the draft.")
        await _render_fixture_match_menu(query, state)
        return
    if action == "crossstart":
        source_index = int(parts[2])
        source_slot = int(parts[3])
        source_match = state["matches"][source_index]
        if state.get("format") == "knockout" and int(source_match.get("round", 1)) > 1:
            await query.answer("Only first-round knockout pairings can be changed manually.", show_alert=True)
            return
        targets = [
            (idx, item) for idx, item in enumerate(state["matches"])
            if idx != source_index and int(item.get("round", 1)) == int(source_match.get("round", 1))
        ]
        if not targets:
            await query.answer("There is no other match in this round to swap with.", show_alert=True)
            return
        state["cross_source"] = source_index
        state["cross_source_slot"] = source_slot
        rows = [[InlineKeyboardButton(
            f"{item['match_id']} · {item.get('player1_name') or 'TBD'} vs {item.get('player2_name') or 'TBD'}",
            callback_data=f"fxe:crosstarget:{idx}",
        )] for idx, item in targets]
        rows.append([InlineKeyboardButton("↩️ Back to Match", callback_data="fxe:matchback")])
        await query.answer()
        await query.edit_message_text(
            "↔️ <b>SWAP WITH ANOTHER MATCH</b>\n"
            f"Source: <code>{html.escape(source_match['match_id'])}</code> · Player {source_slot}\n\n"
            "Choose another match from the same round:",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode=ParseMode.HTML,
        )
        return
    if action == "crosstarget":
        target_index = int(parts[2])
        state["cross_target"] = target_index
        target_match = state["matches"][target_index]
        await query.answer()
        await query.edit_message_text(
            "↔️ <b>SELECT TARGET SLOT</b>\n"
            f"Target match: <code>{html.escape(target_match['match_id'])}</code>\n\n"
            f"Player 1: {html.escape(str(target_match.get('player1_name') or 'TBD'))}\n"
            f"Player 2: {html.escape(str(target_match.get('player2_name') or 'TBD'))}",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Swap with P1", callback_data="fxe:crossslot:1"),
                    InlineKeyboardButton("Swap with P2", callback_data="fxe:crossslot:2"),
                ],
                [InlineKeyboardButton("↩️ Back to Matches", callback_data="fxe:back")],
            ]),
            parse_mode=ParseMode.HTML,
        )
        return
    if action == "crossslot":
        source_index = int(state["cross_source"])
        source_slot = int(state["cross_source_slot"])
        target_index = int(state["cross_target"])
        target_slot = int(parts[2])
        source_match = state["matches"][source_index]
        target_match = state["matches"][target_index]
        left_id, left_name = source_match.get(f"player{source_slot}_id"), source_match.get(f"player{source_slot}_name")
        right_id, right_name = target_match.get(f"player{target_slot}_id"), target_match.get(f"player{target_slot}_name")
        source_match[f"player{source_slot}_id"], source_match[f"player{source_slot}_name"] = right_id, right_name
        target_match[f"player{target_slot}_id"], target_match[f"player{target_slot}_name"] = left_id, left_name
        _recalculate_draft_match_status(source_match, tournament)
        _recalculate_draft_match_status(target_match, tournament)
        _rebuild_knockout_bye_slots(state["matches"], tournament)
        state["selected_match"] = source_index
        state.pop("cross_source", None)
        state.pop("cross_source_slot", None)
        state.pop("cross_target", None)
        await query.answer("Players swapped between matches.")
        await _render_fixture_match_menu(query, state)
        return
    if action == "regen":
        try:
            participants = await service.get_participants(tournament["_id"])
            state["matches"] = generate_fixtures(participants, tournament["_id"], tournament["format"])
            state["participants"] = participants
            state["page"] = 0
            state["selected_match"] = None
            await query.answer("Fixture draft regenerated.")
            await _render_fixture_editor(update, context)
        except ValueError as exc:
            await query.answer(str(exc), show_alert=True)
        return
    if action == "cancel":
        context.user_data.pop("fixture_editor", None)
        await query.edit_message_text("✖️ Fixture editing cancelled. No draft changes were published.")
        await query.answer()
        return
    if action == "done":
        tournament["_editor_participants"] = state.get("participants", [])
        error = _validate_fixture_draft(state["matches"], tournament)
        if error:
            await query.answer(error, show_alert=True)
            return
        try:
            await service.replace_fixture_draft(
                tournament["_id"], int(tournament["group_id"]), state["matches"]
            )
        except (LookupError, ValueError) as exc:
            await query.answer(str(exc), show_alert=True)
            return
        except Exception:
            logger.exception("Could not commit fixture draft for %s", tournament["_id"])
            await query.answer("Could not save fixtures. Check server logs.", show_alert=True)
            return

        matches = await service.get_matches(tournament["_id"])
        rounds = sorted({int(item.get("round", 1)) for item in matches})
        message_ids: dict[str, int] = {}
        try:
            for round_number in rounds:
                text = fixture_round_text(tournament, matches, round_number)
                markup = fixture_round_keyboard(tournament["_id"], rounds, round_number)
                posted = await context.bot.send_message(
                    chat_id=int(tournament["group_id"]),
                    text=text,
                    reply_markup=markup,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                message_ids[str(round_number)] = int(posted.message_id)
        except Exception:
            logger.exception("Fixture data saved, but publishing all round messages failed.")
            await context.application.bot_data["db"].tournaments.update_one(
                {"_id": tournament["_id"]},
                {"$set": {"fixture_round_message_ids": message_ids}},
            )
            context.user_data.pop("fixture_editor", None)
            await query.edit_message_text(
                "⚠️ Fixture draft was saved, but Telegram could not publish every round message. "
                "Check the bot's group send-message permissions. Participants can still use /fixtures to view the saved schedule."
            )
            await query.answer()
            return
        await context.application.bot_data["db"].tournaments.update_one(
            {"_id": tournament["_id"]},
            {"$set": {
                "fixture_round_message_ids": message_ids,
                "fixtures_finalized": True,
            }},
        )
        await refresh_tournament_announcement(context, tournament["_id"])
        context.user_data.pop("fixture_editor", None)
        await query.edit_message_text(
            "✅ <b>FIXTURES FINALIZED & PUBLISHED</b>\n\n"
            f"🏆 {html.escape(tournament['name'])}\n"
            f"📋 {len(rounds)} round message(s) published in the tournament group.\n"
            "All group members can navigate rounds using Previous / Next. "
            "Match results will update these messages automatically.",
            parse_mode=ParseMode.HTML,
        )
        await query.answer("Fixtures published successfully.")
        return
    await query.answer("Unknown fixture editor action.", show_alert=True)


def _recalculate_draft_match_status(match: dict, tournament: dict) -> None:
    p1, p2 = match.get("player1_id"), match.get("player2_id")
    if p1 is None and p2 is not None:
        match["player1_id"], match["player2_id"] = p2, None
        match["player1_name"], match["player2_name"] = match.get("player2_name"), None
        p1, p2 = match.get("player1_id"), match.get("player2_id")
    match["winner_id"] = None
    match.pop("winner_name", None)
    match.pop("completed_at", None)
    if p1 and p2:
        match["status"] = "scheduled"
    elif tournament.get("format") == "knockout" and int(match.get("round", 1)) == 1 and (p1 or p2):
        match["status"] = "bye"
        match["winner_id"] = p1 or p2
    elif tournament.get("format") == "round_robin" and p1 and not p2:
        match["status"] = "bye"
        match["winner_id"] = p1
    else:
        match["status"] = "waiting"


def _validate_fixture_draft(matches: list[dict], tournament: dict) -> str | None:
    if not matches:
        return "The draft has no matches."
    seen_by_round: dict[int, set[int]] = {}
    allowed_players = {int(p["user_id"]) for p in (tournament.get("_editor_participants") or [])}
    for match in matches:
        round_number = int(match.get("round", 1))
        p1, p2 = match.get("player1_id"), match.get("player2_id")
        if p1 is not None and p2 is not None and int(p1) == int(p2):
            return f"{match.get('match_id')}: a player cannot play against themselves."
        used = seen_by_round.setdefault(round_number, set())
        for player_id in (p1, p2):
            if player_id is None:
                continue
            uid = int(player_id)
            if allowed_players and uid not in allowed_players:
                return f"{match.get('match_id')}: selected player is not an active participant."
            if uid in used:
                return f"Round {round_number}: one player is assigned to more than one match."
            used.add(uid)
        if tournament.get("format") == "round_robin" and match.get("status") != "bye":
            if p1 is None or p2 is None:
                return f"{match.get('match_id')}: both players must be selected."
    return None

def _rebuild_knockout_bye_slots(matches: list[dict], tournament: dict) -> None:
    """Recompute unplayed downstream slots after first-round pairing edits."""
    if tournament.get("format") != "knockout":
        return
    by_id = {item.get("match_id"): item for item in matches}
    for item in matches:
        if int(item.get("round", 1)) > 1:
            item["player1_id"] = None
            item["player2_id"] = None
            item["player1_name"] = None
            item["player2_name"] = None
            item["winner_id"] = None
            item["status"] = "waiting"
    for item in matches:
        if item.get("status") != "bye" or not item.get("next_match_id"):
            continue
        target = by_id.get(item["next_match_id"])
        if not target:
            continue
        slot = int(item.get("next_slot", 1))
        target[f"player{slot}_id"] = item.get("winner_id") or item.get("player1_id") or item.get("player2_id")
        target[f"player{slot}_name"] = item.get("player1_name") if item.get("player1_id") else item.get("player2_name")
    for item in matches:
        if int(item.get("round", 1)) > 1 and item.get("player1_id") and item.get("player2_id"):
            item["status"] = "scheduled"
