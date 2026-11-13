"""Participant commands. Registration is intentionally group-only."""
from __future__ import annotations

import html
import time
from pymongo import message
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
            InlineKeyboardButton("❓ Help & Commands", callback_data="help:home")
        ]
    ])

    await message.reply_text(
        "👋 <b>Welcome to Tournament Manager!</b>\n\n"
        "Your official hub for managing and competing in group tournaments. 🎮",
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
        "🏆 <b>TOURNAMENT MANAGER</b>\\n"
        "<i>Your complete tournament hub</i>\\n\\n"
        "Register for competitions, check fixtures and results, "
        "read tournament rules, or manage your own tournaments.\\n\\n"
        "Choose a section below to see the commands available to you."
    ),

    "player": (
        "🎮 <b>PLAYER COMMANDS</b>\\n"
        "<i>Use these commands in the tournament group unless stated otherwise.</i>\\n\\n"
        "🏆 <code>/tournament</code> — View the active tournament details.\\n\\n"
        "📝 <code>/register</code> — Register for the active tournament using your Telegram first name. "
        "Registration must be open.\\n\\n"
        "❌ <code>/unregister</code> — Cancel your registration before fixtures are generated. "
        "After fixtures are generated, contact the tournament team.\\n\\n"
        "📜 <code>/rules</code> — Read the tournament's published rules.\\n\\n"
        "⚔️ <code>/fixtures</code> — View the fixture schedule, rounds and match results.\\n\\n"
        "📊 <code>/standings</code> — View the leaderboard for Round Robin tournaments only.\\n\\n"
        "📚 <code>/tournaments</code> — Browse tournament history for the group."
    ),

    "personal": (
        "👤 <b>PERSONAL & ACCOUNT COMMANDS</b>\\n"
        "<i>These commands can be used in your private chat with the bot.</i>\\n\\n"
        "🧾 <code>/mystatus</code> — View your tournament registrations, player status and upcoming matches.\\n\\n"
        "🆔 <code>/myid</code> — Display your Telegram User ID. Share it with a tournament admin if needed.\\n\\n"
        "❓ <code>/help</code> — Open this help menu.\\n"
        "🚀 <code>/start</code> — Start the bot and open tournament links."
    ),

    "manager": (
        "🛠 <b>TOURNAMENT MANAGEMENT</b>\\n"
        "<i>For the tournament manager and appointed tournament admins.</i>\\n"
        "Commands must be used in the tournament group.\\n\\n"

        "🏆 <b>1. CREATE A TOURNAMENT</b>\\n"
        "<code>/newtournament Name | Format | Capacity | Description</code>\\n"
        "Create the group's tournament. Only a Telegram group admin can create it.\\n"
        "• Format: <code>knockout</code> or <code>round_robin</code>\\n"
        "• Capacity: optional; use <code>0</code> or leave blank for unlimited.\\n"
        "• Description: optional.\\n"
        "<b>Example:</b>\\n"
        "<code>/newtournament Weekend Cup | knockout | 16 | Fun event</code>\\n"
        "Only one active tournament is allowed per group. New tournaments start with registration OFF.\\n\\n"

        "👮 <b>2. ADMIN & PLAYER MANAGEMENT</b>\\n"
        "<code>/addtouradmin USER_ID</code> — Appoint a tournament admin; alternatively reply to their message with <code>/addtouradmin</code>.\\n"
        "<code>/rmtouradmin USER_ID</code> — Remove tournament-admin access; reply method is also supported. Only the manager can appoint or remove tournament admins.\\n"
        "<code>/addplayer USER_ID [Name]</code> — Manually register a player by ID, or reply to their message with <code>/addplayer</code>. Registration must be open.\\n"
        "<code>/rmplayer USER_ID</code> — Completely remove a registered player; alternatively reply to their message with <code>/rmplayer</code>. If fixtures exist but no match result has been recorded, the schedule is rebuilt. Removal is blocked once results exist.\\n"
        "<code>/participants</code> — View active participants and their Telegram User IDs.\\n\\n"

        "⚙️ <b>3. TOURNAMENT SETTINGS</b>\\n"
        "<code>/setdescription New description...</code> — Change the tournament description and refresh its pinned announcement.\\n"
        "<code>/setrules Rules...</code> — Set or update the rules and refresh the pinned announcement's Rules button.\\n"
        "<code>/open</code> — Open registration before fixture generation.\\n"
        "<code>/close</code> — Close registration before fixture generation.\\n"
        "<code>/complete</code> — Mark the tournament as completed.\\n\\n"

        "⚔️ <b>4. FIXTURES & MATCH RESULTS</b>\\n"
        "<code>/generatefixtures</code> — Generate the schedule and open the private fixture editor. Press Done to publish the final draft.\\n"
        "<code>/editfixtures</code> — Re-open the fixture editor before any match result has been recorded.\\n"
        "<code>/setresult MATCH_ID WINNER_ID</code> — Record a match winner.\\n"
        "<code>/setresult MATCH_ID DRAW</code> — Record a draw in a Round Robin match.\\n"
        "<code>/eleminate LOSER_ID</code> — Eliminate a Knockout player and advance their opponent; in Round Robin, disqualify a player. Reply to the player's message with <code>/eleminate</code> is also supported.\\n\\n"

        "📢 <b>5. ANNOUNCEMENTS</b>\\n"
        "<code>/announce Your message here</code> — Send an announcement to the group.\\n"
        "<code>/unpinannouncement</code> — Unpin the latest bot announcement.\\n\\n"

        "📊 <b>6. ADMIN OVERVIEW</b>\\n"
        "<code>/admintournaments</code> — View tournament history and administration details."
    ),

    "about": (
        "ℹ️ <b>ABOUT TOURNAMENT MANAGER</b>\\n\\n"
        "Tournament Manager helps Telegram groups run competitions with registration, "
        "player administration, fixtures, match results, standings and pinned announcements.\\n\\n"
        "🎮 <b>SUPPORTED FORMATS</b>\\n"
        "• <b>Knockout:</b> Players are eliminated after losing; winners advance through the bracket.\\n"
        "• <b>Round Robin:</b> Players compete against each other and earn points on the standings table.\\n\\n"
        "👮 <b>ACCESS LEVELS</b>\\n"
        "• Telegram group admins can create a tournament.\\n"
        "• The tournament manager can appoint or remove tournament admins.\\n"
        "• The manager and appointed tournament admins can use tournament-management commands.\\n\\n"
        "📌 <b>GROUP SETUP</b>\\n"
        "The bot must be a Telegram group administrator with permission to pin messages. "
        "Only one active tournament is allowed in each group.\\n\\n"
        "Use <code>/help</code> to explore commands."
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
    
    # Safely acknowledge the button press
    try:
        await query.answer()
    except BadRequest as exc:
        if "is too old" not in str(exc).lower() and "is invalid" not in str(exc).lower():
            raise  # Rethrow if it's a different BadRequest

    section = query.data.split(":", 1)[1]

    if section not in HELP_TEXTS:
        return

    keyboard = (
        help_keyboard()
        if section == "home"
        else category_keyboard()
    )

    try:
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


async def start_menu_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Route actions from the private /start menu."""
    query = update.callback_query
    if not query:
        return
        
    # Safely acknowledge the button press
    try:
        await query.answer()
    except BadRequest as exc:
        if "is too old" not in str(exc).lower() and "is invalid" not in str(exc).lower():
            raise

    action = query.data.split(":", 1)[1]
    if action == "tournaments":
        await list_tournaments(update, context)
    elif action == "mystatus":
        await my_status(update, context)


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

    # 1. Pehle tournament fetch karo (taaki hume tournament_id mil sake)
    service = context.application.bot_data["tournament_service"]
    tournament = await service.get_active_tournament(update.effective_chat.id)
    if not tournament:
        await update.effective_message.reply_text("There is no active tournament open for registration in this group.")
        return

    # 2. Database se check karo ki player ne DM me bot start kiya hai ya nahi
    db = context.application.bot_data["db"]
    user = update.effective_user
    user_doc = await db.users.find_one({"_id": user.id})
    
    if not user_doc:
        # Agar start nahi kiya hai, toh link banao aur button me bhej do
        bot_username = context.bot.username
        tournament_id = tournament["_id"]
        chat_id = update.effective_chat.id
        deep_link = f"https://t.me/{bot_username}?start=treg_{chat_id}_{tournament_id}"
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Start Bot", url=deep_link)]
        ])
        await update.effective_message.reply_text(
            "⚠️ Please start the bot in DM first to complete your registration!", 
            reply_markup=keyboard
        )
        return

    # 3. Agar pehle se start kiya hua hai, toh seedha register kar do
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
    
    # Registration ke baad Pinned Announcement ko update karna mat bhoolna
    if result == "registered":
        await refresh_tournament_announcement(context, tournament["_id"])


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


async def my_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View user's tournament registrations, current status, and upcoming matches."""
    user_id = update.effective_user.id
    db = context.application.bot_data["db"]
    service = context.application.bot_data["tournament_service"]
    
    # Retrieve all registrations for the user
    registrations = await db.registrations.find({"user_id": user_id}).sort("registered_at", -1).to_list(length=50)
    
    if not registrations:
        await update.effective_message.reply_text("You have no tournament registrations yet.")
        return

    lines = ["🧾 <b>YOUR TOURNAMENT STATUS</b>\n"]
    
    for registration in registrations:
        tournament = await db.tournaments.find_one({"_id": registration["tournament_id"]})
        if not tournament:
            lines.append("• Deleted tournament\n")
            continue
            
        title = html.escape(tournament["name"])
        group_title = html.escape(tournament.get("group_title", "Group"))
        
        # Determine player status
        if registration.get("disqualified"):
             status_text = "🚫 <b>DISQUALIFIED</b>"
        elif registration.get("eliminated"):
             status_text = "❌ <b>ELIMINATED</b>"
        elif tournament.get("status") == "completed":
             if tournament.get("winner_id") == user_id:
                  status_text = "🏆 <b>CHAMPION</b>"
             else:
                  status_text = "🏁 <b>FINISHED</b>"
        else:
             status_text = "🟢 <b>ACTIVE</b>"
             
        lines.append(f"🏆 <b>{title}</b> ({group_title})")
        lines.append(f"👤 Player: {user_link(user_id, registration['name'])}")
        lines.append(f"📊 Status: {status_text}")
        
        # If the tournament is active and fixtures exist, find the upcoming match
        if tournament.get("is_active") and tournament.get("fixtures_finalized"):
            # Find the user's scheduled or waiting matches
            matches = await db.matches.find({
                "tournament_id": tournament["_id"],
                "status": {"$in": ["scheduled", "waiting", "bye"]},
                "$or": [{"player1_id": user_id}, {"player2_id": user_id}]
            }).sort("round", 1).to_list(length=1)
            
            if matches:
                match = matches[0]
                match_id = html.escape(match["match_id"])
                round_num = match.get("round", "?")
                
                if match.get("status") == "bye":
                     lines.append(f"⚔️ Next Match: <code>{match_id}</code> (Round {round_num}) — 🟡 <b>BYE (Advances automatically)</b>")
                elif match.get("status") == "waiting":
                     lines.append(f"⚔️ Next Match: <code>{match_id}</code> (Round {round_num}) — ⏳ <b>Waiting for opponent...</b>")
                else: # scheduled
                     opponent_id = match.get("player2_id") if match.get("player1_id") == user_id else match.get("player1_id")
                     opponent_name = match.get("player2_name") if match.get("player1_id") == user_id else match.get("player1_name")
                     opponent = user_link(opponent_id, opponent_name) if opponent_id else "TBD"
                     lines.append(f"⚔️ Next Match: <code>{match_id}</code> (Round {round_num}) vs {opponent}")
            elif not registration.get("eliminated") and not registration.get("disqualified"):
                lines.append("⚔️ Matches: No upcoming matches found.")
        elif not tournament.get("fixtures_generated"):
             lines.append("⚔️ Matches: 🛠 Fixtures are not generated yet.")
             
        lines.append("")

    await reply_html_chunks(update.effective_message, lines)


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(f"Your Telegram user ID: <code>{update.effective_user.id}</code>", parse_mode=ParseMode.HTML)


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
        if first_id not in table or second_id not in table:
            continue
        table[first_id]["played"] += 1
        table[second_id]["played"] += 1
        if match.get("is_draw"):
            table[first_id]["points"] += 1
            table[second_id]["points"] += 1
            continue
        if winner_id not in {first_id, second_id}:
            continue
        loser_id = second_id if winner_id == first_id else first_id
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
