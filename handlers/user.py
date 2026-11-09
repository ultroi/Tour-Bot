"""Participant commands. Registration is intentionally group-only."""
from __future__ import annotations

import html

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, CallbackQueryHandler
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from utils import (
    ensure_group, ensure_private_user, is_group_chat, match_line,
    reply_html_chunks, tournament_summary, user_link,
)
import logging



logger = logging.getLogger(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await ensure_private_user(update, context)
    except Exception:
        logger.exception("Could not save private user to MongoDB")

    await update.effective_message.reply_text(
        "Welcome to Tournament Manager! 🏆\n\n"
        "Add me to your Telegram group and make me an admin "
        "with permission to pin messages.\n"
        "A group admin can create one tournament in that group. "
        "The creator becomes its tournament manager.\n\n"
        "Use /help to see all commands. "
        "Registration is available only inside the tournament group."
    )





def help_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎮 Player Commands", callback_data="help:player"),
            InlineKeyboardButton("👤 Personal", callback_data="help:personal"),
        ],
        [
            InlineKeyboardButton("🛠 Manager Panel", callback_data="help:manager"),
            InlineKeyboardButton("ℹ️ About Bot", callback_data="help:about"),
        ],
    ])


def category_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🏠 Main Menu", callback_data="help:home"),
        ]
    ])


HELP_TEXTS = {
    "home": (
        "🏆 <b>TOURNAMENT MANAGER</b>\n"
        "<i>Your Official Tournament Hub</i>\n\n"
        "Welcome, Gamer! 🎮\n\n"
        "Manage tournaments, register for competitions, "
        "track fixtures and follow your championship journey.\n\n"
        "Select a category below to explore available commands.\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🎮 <b>Play.</b> Compete. <b>Win.</b> 🏆"
    ),

    "player": (
        "🎮 <b>PLAYER COMMANDS</b>\n"
        "<i>Use these commands inside the tournament group.</i>\n\n"
        "🏆 <b>/tournament</b>\n"
        "View current tournament details.\n\n"
        "📝 <b>/register</b>\n"
        "Register automatically using your Telegram first name.\n\n"
        "❌ <b>/unregister</b>\n"
        "Withdraw before fixtures are generated.\n\n"
        "📜 <b>/rules</b>\n"
        "Read tournament rules.\n\n"
        "⚔️ <b>/fixtures</b>\n"
        "View matches, schedules and results.\n\n"
        "👥 <b>/participants</b>\n"
        "View registered players.\n\n"
        "📊 <b>/standings</b>\n"
        "View the round-robin leaderboard.\n\n"
        "📚 <b>/tournaments</b>\n"
        "View tournament history for this group."
    ),

    "personal": (
        "👤 <b>PERSONAL COMMANDS</b>\n"
        "<i>Your personal account tools.</i>\n\n"
        "🧾 <b>/myreg</b>\n"
        "View your tournament registrations.\n\n"
        "🆔 <b>/myid</b>\n"
        "Get your Telegram user ID.\n\n"
        "❓ <b>/help</b>\n"
        "Open this help menu."
    ),

    "manager": (
        "🛠 <b>TOURNAMENT MANAGEMENT PANEL</b>\n"
        "<i>Commands for Tournament Managers and Tournament Admins.</i>\n\n"
        "🏆 <b>TOURNAMENT CREATION</b>\n"
        "/newtournament Name | format | capacity | description\n"
        "Create a tournament. Group admins only.\n\n"
        "👮 <b>ADMIN MANAGEMENT</b>\n"
        "/addtouradmin — Reply to a member or provide USER_ID.\n"
        "/rmtouradmin — Remove a tournament admin.\n"
        "Only the Tournament Manager can manage tournament admins.\n\n"
        "⚙️ <b>TOURNAMENT SETTINGS</b>\n"
        "/setdescription Description...\n"
        "/setrules Rules...\n"
        "/close — Close registration.\n"
        "/open — Reopen registration.\n"
        "/complete — Complete tournament.\n\n"
        "⚔️ <b>MATCH MANAGEMENT</b>\n"
        "/generatefixtures — Generate match schedule.\n"
        "/setresult MATCH_ID WINNER_USER_ID\n"
        "/adminparticipants — View participant IDs.\n\n"
        "📢 <b>ANNOUNCEMENTS</b>\n"
        "/announce Message...\n"
        "/unpinannouncement — Unpin latest announcement.\n\n"
        "📊 <b>ADMIN OVERVIEW</b>\n"
        "/admintournaments — View tournament history."
    ),

    "about": (
        "ℹ️ <b>ABOUT TOURNAMENT MANAGER</b>\n\n"
        "🎮 Support for Knockout and Round Robin.\n"
        "👥 Automatic player registration.\n"
        "⚔️ Automatic fixture generation.\n"
        "📊 Match results and leaderboards.\n"
        "📢 Group announcements and pin management.\n"
        "👮 Separate Tournament Manager and Admin roles.\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📌 <b>IMPORTANT</b>\n\n"
        "• Tournament creation is restricted to group admins.\n"
        "• Only one active tournament is allowed per group.\n"
        "• Registration must be done inside the group.\n"
        "• The bot needs permission to pin messages.\n"
        "• Announcements automatically unpin after the configured hours.\n\n"
        "🎯 <i>Compete fairly. Play responsibly. Win big!</i>"
    ),
}


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> None:
    await update.effective_message.reply_text(
        HELP_TEXTS["home"],
        reply_markup=help_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def help_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()

    section = query.data.split(":", 1)[1]

    if section not in HELP_TEXTS:
        return

    keyboard = (
        help_keyboard()
        if section == "home"
        else category_keyboard()
    )

    await query.edit_message_text(
        text=HELP_TEXTS[section],
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


async def _resolve_tournament(update: Update, context: ContextTypes.DEFAULT_TYPE, allow_completed: bool = True) -> dict | None:
    service = context.application.bot_data["tournament_service"]
    parts = (update.effective_message.text or "").split(maxsplit=1)
    tournament_id = parts[1].strip() if len(parts) == 2 else None
    if is_group_chat(update):
        await ensure_group(update, context)
        tournament = await service.get_group_tournament(update.effective_chat.id, tournament_id)
        if not tournament and not tournament_id and allow_completed:
            history = await service.list_tournaments(group_id=update.effective_chat.id)
            tournament = history[0] if history else None
        return tournament
    if tournament_id:
        return await service.get_tournament(tournament_id)
    await update.effective_message.reply_text("Use this command in the tournament group, or provide a tournament ID in a private chat.")
    return None


async def list_tournaments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    service = context.application.bot_data["tournament_service"]
    if is_group_chat(update):
        await ensure_group(update, context)
        tournaments = await service.list_tournaments(group_id=update.effective_chat.id)
        title = "🏆 <b>TOURNAMENTS IN THIS GROUP</b>"
    else:
        tournaments = await service.list_tournaments()
        title = "🏆 <b>RECENT TOURNAMENTS</b>"
    if not tournaments:
        await update.effective_message.reply_text("There are no tournaments to show.")
        return
    lines = [title]
    for item in tournaments:
        lines.extend(["", tournament_summary(item, include_description=False)])
    await reply_html_chunks(update.effective_message, lines)


async def view_tournament(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tournament = await _resolve_tournament(update, context)
    if not tournament:
        await update.effective_message.reply_text("Tournament not found in this group.")
        return
    manager = user_link(tournament.get("manager_id"), tournament.get("manager_name", "Tournament Manager"))
    admins = tournament.get("tournament_admins", [])
    admin_names = tournament.get("tournament_admin_names", {})
    admin_lines = [
        user_link(user_id, admin_names.get(str(user_id), f"Admin {i}"))
        for i, user_id in enumerate(admins, start=1)
    ]
    lines = [tournament_summary(tournament), f"Manager: {manager}", "Tournament admins:"]
    lines.extend(admin_lines if admin_lines else ["None"])
    await reply_html_chunks(update.effective_message, lines)


async def register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_group_chat(update):
        await update.effective_message.reply_text("⛔ Registration is group-only. Open the tournament group and send /register there.")
        return
    if not await ensure_group(update, context):
        return
    if len((update.effective_message.text or "").strip().split(maxsplit=1)) > 1:
        await update.effective_message.reply_text("Just send /register. Your Telegram first name is used automatically.")
        return
    service = context.application.bot_data["tournament_service"]
    tournament = await service.get_active_tournament(update.effective_chat.id)
    if not tournament:
        await update.effective_message.reply_text("There is no active tournament open for registration in this group.")
        return
    user = update.effective_user
    result = await service.register(
        tournament["_id"], update.effective_chat.id, user.id, user.first_name, user.username
    )
    name_link = user_link(user.id, user.first_name)
    messages = {
        "registered": f"✅ {name_link}, you are registered for <b>{html.escape(tournament['name'])}</b>.",
        "already_registered": f"{name_link}, you are already registered for this tournament.",
        "not_found": "Tournament not found in this group.",
        "closed": "Registration is closed for this tournament.",
        "full": "Sorry, this tournament has reached its participant limit.",
    }
    await update.effective_message.reply_text(messages[result], parse_mode=ParseMode.HTML)


async def unregister(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_group_chat(update):
        await update.effective_message.reply_text("Use /unregister in the tournament group.")
        return
    await ensure_group(update, context)
    tournament = await context.application.bot_data["tournament_service"].get_active_tournament(update.effective_chat.id)
    if not tournament:
        await update.effective_message.reply_text("There is no active tournament in this group.")
        return
    result = await context.application.bot_data["tournament_service"].unregister(
        tournament["_id"], update.effective_chat.id, update.effective_user.id
    )
    messages = {
        "unregistered": "Your registration has been cancelled.",
        "not_registered": "You are not registered for this tournament.",
        "not_found": "Tournament not found.",
        "fixtures_exist": "You cannot withdraw after fixtures have been generated. Contact the tournament manager.",
    }
    await update.effective_message.reply_text(messages[result])


async def view_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tournament = await _resolve_tournament(update, context)
    if not tournament:
        await update.effective_message.reply_text("Tournament not found. Use /rules in the group or /rules TOURNAMENT_ID in private.")
        return
    await update.effective_message.reply_text(
        f"📜 RULES — {tournament['name']}\n\n{tournament.get('rules', 'No rules added yet.') }"
    )


async def view_fixtures(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tournament = await _resolve_tournament(update, context)
    if not tournament:
        await update.effective_message.reply_text("Tournament not found. Use /fixtures in the group or /fixtures TOURNAMENT_ID in private.")
        return
    matches = await context.application.bot_data["tournament_service"].get_matches(tournament["_id"])
    if not matches:
        await update.effective_message.reply_text("Fixtures have not been generated yet.")
        return
    grouped: dict[int, list[str]] = {}
    for match in matches:
        grouped.setdefault(match["round"], []).append(match_line(match))
    lines = [f"📅 <b>FIXTURES — {html.escape(tournament['name'])}</b>"]
    for round_number, round_matches in grouped.items():
        lines.append(f"\n<b>ROUND {round_number}</b>")
        lines.extend(round_matches)
    await reply_html_chunks(update.effective_message, lines)


async def my_registrations(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    db = context.application.bot_data["db"]
    registrations = await db.registrations.find({"user_id": user_id}).sort("registered_at", -1).to_list(length=50)
    if not registrations:
        await update.effective_message.reply_text("You have no tournament registrations yet.")
        return
    lines = ["🧾 <b>YOUR REGISTRATIONS</b>"]
    for registration in registrations:
        tournament = await db.tournaments.find_one({"_id": registration["tournament_id"]})
        title = tournament["name"] if tournament else "Deleted tournament"
        group_title = tournament.get("group_title", "Group") if tournament else "Group"
        lines.append(
            f"• {html.escape(title)} — {html.escape(group_title)} — {user_link(user_id, registration['name'])}"
        )
    await reply_html_chunks(update.effective_message, lines)


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(f"Your Telegram user ID: <code>{update.effective_user.id}</code>", parse_mode=ParseMode.HTML)


async def list_participants(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tournament = await _resolve_tournament(update, context)
    if not tournament:
        await update.effective_message.reply_text("Tournament not found.")
        return
    participants = await context.application.bot_data["tournament_service"].get_participants(tournament["_id"])
    if not participants:
        await update.effective_message.reply_text("No one has registered yet.")
        return
    lines = [f"👥 <b>PARTICIPANTS — {html.escape(tournament['name'])}</b>"]
    for number, participant in enumerate(participants, start=1):
        lines.append(f"{number}. {user_link(participant['user_id'], participant['name'])}")
    await reply_html_chunks(update.effective_message, lines)


async def standings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tournament = await _resolve_tournament(update, context)
    if not tournament:
        await update.effective_message.reply_text("Tournament not found.")
        return
    if tournament["format"] != "round_robin":
        await update.effective_message.reply_text("Standings are available for round-robin tournaments. Use /fixtures for knockout brackets.")
        return
    participants = await context.application.bot_data["tournament_service"].get_participants(tournament["_id"])
    table = {
        p["user_id"]: {"name": p["name"], "played": 0, "wins": 0, "losses": 0, "points": 0}
        for p in participants
    }
    matches = await context.application.bot_data["tournament_service"].get_matches(tournament["_id"])
    for match in matches:
        if match.get("status") != "completed" or not match.get("player1_id") or not match.get("player2_id"):
            continue
        first_id, second_id, winner_id = match["player1_id"], match["player2_id"], match.get("winner_id")
        if first_id not in table or second_id not in table or winner_id not in {first_id, second_id}:
            continue
        loser_id = second_id if winner_id == first_id else first_id
        table[first_id]["played"] += 1
        table[second_id]["played"] += 1
        table[winner_id]["wins"] += 1
        table[winner_id]["points"] += 3
        table[loser_id]["losses"] += 1
    ranked = sorted(table.items(), key=lambda item: (-item[1]["points"], -item[1]["wins"], item[1]["name"].casefold()))
    if not ranked:
        await update.effective_message.reply_text("No participants are registered yet.")
        return
    lines = [f"📊 <b>STANDINGS — {html.escape(tournament['name'])}</b>", "Rank | Player | P | W | L | Pts"]
    for rank, (user_id, row) in enumerate(ranked, start=1):
        lines.append(f"{rank}. {user_link(user_id, row['name'])} | {row['played']} | {row['wins']} | {row['losses']} | {row['points']}")
    if not any(row["played"] for _, row in ranked):
        lines.append("\nNo match results have been recorded yet. Each win is worth 3 points.")
    await reply_html_chunks(update.effective_message, lines)

from telegram import Update
from telegram.ext import ContextTypes

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show real-time statistics for the bot."""
    db = context.application.bot_data["db"]
    
    # Query MongoDB for total document counts
    total_users = await db.users.count_documents({})
    total_groups = await db.groups.count_documents({})
    total_tournaments = await db.tournaments.count_documents({})
    
    stats_message = (
        "📊 <b>Bot Real-Time Statistics</b>\n\n"
        f"👤 <b>Total Users:</b> {total_users}\n"
        f"👥 <b>Total Groups:</b> {total_groups}\n"
        f"🏆 <b>Total Tournaments:</b> {total_tournaments}"
    )
    
    await update.effective_message.reply_text(stats_message, parse_mode="HTML")