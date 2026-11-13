"""Participant commands. Registration is intentionally group-only."""
from __future__ import annotations

import html
import time
from telegram.error import BadRequest
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, CallbackQueryHandler
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from utils import (
    ensure_group,
    ensure_private_user,
    has_custom_rules,
    is_group_chat,
    match_line,
    refresh_tournament_announcement,
    reply_html_chunks,
    tournament_deep_link,
    tournament_summary,
    user_link,
    fixture_round_text,
    fixture_round_keyboard,
)
import logging



logger = logging.getLogger(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start the private bot chat and process tournament deep-link actions."""
    try:
        await ensure_private_user(update, context)
    except Exception:
        logger.exception("Could not save private user to MongoDB")

    message = update.effective_message
    user = update.effective_user
    payload = context.args[0].strip() if context.args else ""
    service = context.application.bot_data["tournament_service"]

    # Deep link launched from the pinned tournament card. Telegram delivers this
    # payload only after the user presses Start in the bot's private chat.
    if payload.startswith("treg_"):
        try:
            _, group_id_text, tournament_id = payload.split("_", 2)
            group_id = int(group_id_text)
            tournament_id = tournament_id.upper()
        except (ValueError, TypeError):
            await message.reply_text("That registration link is invalid. Please use the Register button in the tournament group.")
            return

        tournament = await service.get_tournament(tournament_id)
        if not tournament or int(tournament.get("group_id", 0)) != group_id:
            await message.reply_text("This tournament registration link is no longer valid.")
            return
        if not await _is_tournament_group_member(context, group_id, user.id):
            await message.reply_text(
                "Please join the tournament group first, then tap the Register button again."
            )
            return
        result = await service.register(
            tournament_id, group_id, user.id, user.first_name, user.username
        )
        await message.reply_text(_registration_result_text(result, tournament, user), parse_mode=ParseMode.HTML)
        await refresh_tournament_announcement(context, tournament_id)
        return

    if payload.startswith("trules_"):
        tournament_id = payload.removeprefix("trules_").strip().upper()
        tournament = await service.get_tournament(tournament_id)
        if not tournament:
            await message.reply_text("This tournament could not be found.")
            return
        if not has_custom_rules(tournament):
            await message.reply_text("The tournament manager has not published the rules yet.")
            return
        await _send_tournament_rules(message, tournament)
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📚 View Tournaments", callback_data="help:home"),
            InlineKeyboardButton("🧾 My Registrations", callback_data="help:personal")
        ],
        [
            InlineKeyboardButton("❓ Help & Commands", callback_data="help:home")
        ]
    ])

    await message.reply_text(
        "👋 <b>Welcome to Tournament Manager!</b>\n\n"
        "Your official hub for managing and competing in group tournaments. 🎮\n\n"
        "<b>To Register for a Tournament:</b>\n"
        "1️⃣ Open the tournament group.\n"
        "2️⃣ Tap <b>Register in DM</b> on the pinned tournament post.\n"
        "3️⃣ Press <b>Start</b> here, and I'll handle the rest automatically!\n\n"
        "<i>What would you like to do next? Choose an option below or use the menu.</i>",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
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
        "/generatefixtures — Generate a fixture draft and open the editor.\n"
        "/editfixtures — Edit the unstarted fixture draft.\n"
        "/setresult MATCH_ID WINNER_USER_ID\n"
        "/eleminate — Knockout: eliminate a loser and advance the opponent. Round robin: disqualify a player.\n"
        "/adminparticipants — View active participant IDs.\n\n"
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

    try:
        # Check if the current message is the start message (it won't have the help text)
        if "Welcome to Tournament Manager" in query.message.text:
             await query.edit_message_text(
                text=HELP_TEXTS[section],
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
        else:
            await query.edit_message_text(
                text=HELP_TEXTS[section],
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            raise exc


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
        await update.effective_message.reply_text(
            "Tournament not found. Use /fixtures in the group or /fixtures TOURNAMENT_ID in private."
        )
        return
    service = context.application.bot_data["tournament_service"]
    if tournament.get("fixtures_finalized") is False:
        await update.effective_message.reply_text("🛠 Tournament admins are preparing the fixtures. Please check again shortly.")
        return
    matches = await service.get_matches(tournament["_id"])
    if not matches:
        await update.effective_message.reply_text("Fixtures have not been generated yet.")
        return
    rounds = sorted({int(item.get("round", 1)) for item in matches})
    current = rounds[0]
    await update.effective_message.reply_text(
        fixture_round_text(tournament, matches, current),
        reply_markup=fixture_round_keyboard(tournament["_id"], rounds, current),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def fixture_public_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Public round navigation. Every group member can use it; edits are rate-limited."""
    query = update.callback_query
    if not query or not query.data:
        return
    try:
        _, tournament_id, round_text = query.data.split(":", 2)
        round_number = int(round_text)
    except (ValueError, TypeError):
        await query.answer("Invalid fixture page.", show_alert=True)
        return

    # Per-user navigation cooldown to prevent callback spam in busy groups.
    now = time.monotonic()
    limits = context.application.bot_data.setdefault("fixture_nav_rate_limits", {})
    user_id = int(query.from_user.id)
    previous = float(limits.get(user_id, 0))
    cooldown = 1.25
    if now - previous < cooldown:
        await query.answer("Please wait a moment before changing fixture pages.")
        return
    limits[user_id] = now
    if len(limits) > 5000:
        cutoff = now - 120
        context.application.bot_data["fixture_nav_rate_limits"] = {
            uid: stamp for uid, stamp in limits.items() if stamp >= cutoff
        }

    service = context.application.bot_data["tournament_service"]
    tournament = await service.get_tournament(tournament_id)
    if not tournament or not query.message:
        await query.answer("Tournament or fixture message not found.", show_alert=True)
        return
    if query.message.chat.type != "private" and int(query.message.chat.id) != int(tournament.get("group_id", 0)):
        await query.answer("These fixtures belong to another group.", show_alert=True)
        return
    matches = await service.get_matches(tournament_id)
    rounds = sorted({int(item.get("round", 1)) for item in matches})
    if round_number not in rounds:
        await query.answer("This round is no longer available.", show_alert=True)
        return
    try:
        await query.edit_message_text(
            fixture_round_text(tournament, matches, round_number),
            reply_markup=fixture_round_keyboard(tournament_id, rounds, round_number),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await query.answer()
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            await query.answer()
        else:
            logger.exception("Could not navigate fixture round")
            await query.answer("Could not update this fixture page.", show_alert=True)


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
        status = " — ❌ ELIMINATED" if registration.get("eliminated") else ""
        lines.append(
            f"• {html.escape(title)} — {html.escape(group_title)} — {user_link(user_id, registration['name'])}{status}"
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