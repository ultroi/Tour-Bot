"""Telegram entry point for the group-based Tournament Manager Bot.

The bot refuses to process group commands until it is a group administrator
with permission to pin messages. This is the minimum privilege required by
the current tournament announcement workflow.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse
import logging
import sys
import traceback
import asyncio
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

from config import Settings
from database import Database
from handlers import admin, user
from services.tournament_service import TournamentService
from utils import ensure_private_user, ensure_group

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)

GROUP_TYPES = {"group", "supergroup"}

# Do not request every Telegram admin permission. For the features currently
# implemented, administrator status + can_pin_messages are the required rights.
REQUIRED_RIGHTS = {
    "can_pin_messages": "Pin Messages",
}


async def check_bot_group_permissions(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> tuple[bool, list[str]]:
    """Return whether the bot has the required group permissions."""
    bot = await context.bot.get_me()
    member = await context.bot.get_chat_member(chat_id=chat_id, user_id=bot.id)

    if member.status not in {"administrator", "creator"}:
        return False, ["Promote the bot to a group administrator."]

    missing = [
        label
        for attribute, label in REQUIRED_RIGHTS.items()
        if not bool(getattr(member, attribute, False))
    ]
    return not missing, missing


def permission_setup_message(missing: list[str]) -> str:
    details = "\n".join(f"• {item}" for item in missing)
    return (
        "⛔ <b>TOURNAMENT BOT SETUP REQUIRED</b>\n\n"
        "I cannot start tournament operations in this group yet.\n\n"
        "<b>Please ask the group owner/admin to:</b>\n"
        "1. Open Group Settings → Administrators.\n"
        "2. Add/promote this bot as an administrator.\n"
        "3. Enable <b>Pin Messages</b> permission.\n\n"
        f"<b>Missing requirement(s):</b>\n{details}\n\n"
        "I will automatically check again when a command is used. "
        "Tournament commands remain disabled until the required rights are granted."
    )


async def data_storage_middleware(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Automatically store users and groups in the database on every interaction."""
    chat = update.effective_chat
    if not chat:
        return

    try:
        if chat.type == "private":
            await ensure_private_user(update, context)
        elif chat.type in GROUP_TYPES:
            await ensure_group(update, context)
    except Exception:
        logger.exception("Failed to store user/group data in middleware")


async def group_permission_guard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Stop every group command/callback until required bot rights are granted."""
    chat = update.effective_chat
    if not chat or chat.type not in GROUP_TYPES:
        return

    message = update.effective_message
    callback = update.callback_query

    # Gate commands and inline-button callbacks, not ordinary group conversation.
    is_command = bool(
        message
        and message.text
        and message.text.lstrip().startswith("/")
    )
    is_group_callback = bool(callback and callback.message)
    if not (is_command or is_group_callback):
        return

    try:
        ready, missing = await check_bot_group_permissions(context, chat.id)
    except Exception:
        logger.exception("Could not verify bot permissions in group %s", chat.id)
        missing = [
            "Could not verify the bot's administrator permissions. "
            "Please make the bot a group administrator and try again."
        ]
        ready = False

    if ready:
        return

    notice = permission_setup_message(missing)
    try:
        if callback:
            await callback.answer(
                "Bot setup incomplete. Ask a group admin to grant the required rights.",
                show_alert=True,
            )
            if callback.message:
                await callback.message.reply_text(notice, parse_mode="HTML")
        elif message:
            await message.reply_text(notice, parse_mode="HTML")
    except Exception:
        logger.exception("Could not send permission setup notice in group %s", chat.id)

    # Prevent all later command/callback handlers from running.
    raise ApplicationHandlerStop


async def on_bot_membership_change(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Notify the group when the bot is added or its admin rights change."""
    change = update.my_chat_member
    if not change:
        return

    chat = change.chat
    if chat.type not in GROUP_TYPES:
        return

    new_status = change.new_chat_member.status
    if new_status in {"left", "kicked"}:
        return

    try:
        ready, missing = await check_bot_group_permissions(context, chat.id)
    except Exception:
        logger.exception("Could not inspect bot membership in group %s", chat.id)
        ready = False
        missing = ["Unable to verify permissions. Please try again."]

    if ready:
        text = (
            "✅ <b>TOURNAMENT MANAGER IS READY</b>\n\n"
            "Thanks! I have the required group administrator access and "
            "<b>Pin Messages</b> permission.\n\n"
            "You can now use /help to explore tournament commands."
        )
    else:
        text = permission_setup_message(missing)

    try:
        await context.bot.send_message(
            chat_id=chat.id,
            text=text,
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Could not send setup status to group %s", chat.id)


async def post_init(application: Application) -> None:
    print("[BOOT] Connecting to MongoDB...", flush=True)

    db: Database = application.bot_data["db"]
    await db.connect()

    print("[BOOT] MongoDB connected!", flush=True)

    bot = await application.bot.get_me()

    print(
        f"[BOOT] Telegram connected: @{bot.username}",
        flush=True
    )
    logger.info("Tournament Manager Bot started as @%s", bot.username)

    if application.job_queue:
        # Due times are stored in MongoDB, so pending unpins survive restarts.
        application.job_queue.run_repeating(
            admin.process_due_unpins,
            interval=60,
            first=60,
            name="process-due-announcement-unpins",
        )
    else:
        logger.warning(
            "JobQueue is unavailable; automatic announcement unpinning is disabled"
        )


async def post_shutdown(application: Application) -> None:
    db: Database | None = application.bot_data.get("db")
    if db:
        await db.close()
        logger.info("MongoDB connection closed")


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    error = context.error
    if error:
        logger.error(
            "Unhandled Telegram update error",
            exc_info=(type(error), error, error.__traceback__),
        )
    else:
        logger.error("Unhandled Telegram update error without exception details")

    # Send every unhandled error to the configured owner/admin Telegram account.
    try:
        error_text = (
            "".join(traceback.format_exception(type(error), error, error.__traceback__))
            if error
            else "No exception object was provided by python-telegram-bot."
        )

        update_details = []
        if isinstance(update, Update):
            update_details.append(f"Update ID: {update.update_id}")
            if update.effective_chat:
                update_details.append(
                    f"Chat: {update.effective_chat.title or update.effective_chat.full_name or 'Private'} "
                    f"(ID: {update.effective_chat.id}, type: {update.effective_chat.type})"
                )
            if update.effective_user:
                update_details.append(
                    f"User: {update.effective_user.full_name} "
                    f"(ID: {update.effective_user.id}, username: "
                    f"@{update.effective_user.username or 'N/A'})"
                )
            if update.effective_message and update.effective_message.text:
                update_details.append(
                    f"Message: {update.effective_message.text[:500]}"
                )

        report = (
            "🚨 TOURNAMENT BOT ERROR\n\n"
            + ("\n".join(update_details) + "\n\n" if update_details else "")
            + f"Error: {type(error).__name__ if error else 'UnknownError'}\n\n"
            + "Traceback:\n"
            + error_text
        )

        chunks = [report[i:i + 3900] for i in range(0, len(report), 3900)]
        for chunk in chunks:
            await context.bot.send_message(
                chat_id=6439532660,
                text=chunk,
            )
    except Exception:
        logger.exception("Could not DM the error report to owner ID 6439532660")

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Something went wrong. Please try again or contact the tournament manager."
            )
        except Exception:
            logger.exception("Could not send error message to user")


def build_application(settings: Settings, db: Database) -> Application:
    application = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.bot_data["settings"] = settings
    application.bot_data["db"] = db
    application.bot_data["tournament_service"] = TournamentService(db)

    # Automatically store users and groups in the database on every interaction.
    application.add_handler(
        TypeHandler(Update, data_storage_middleware),
        group=-3,
    )

    # Check membership/rights changes as soon as Telegram reports them.
    application.add_handler(
        ChatMemberHandler(
            on_bot_membership_change,
            ChatMemberHandler.MY_CHAT_MEMBER,
        ),
        group=-2,
    )

    # This guard runs before all normal command and callback handlers.
    application.add_handler(
        TypeHandler(Update, group_permission_guard),
        group=-1,
    )

    # Participant commands.
    application.add_handler(CommandHandler("start", user.start))
    application.add_handler(CommandHandler("stats", user.stats))
    application.add_handler(CommandHandler("help", user.help_command))
    application.add_handler(CommandHandler("tournaments", user.list_tournaments))
    application.add_handler(CommandHandler("tournament", user.view_tournament))
    application.add_handler(CommandHandler("register", user.register))
    application.add_handler(CommandHandler("unregister", user.unregister))
    application.add_handler(CommandHandler("rules", user.view_rules))
    application.add_handler(CommandHandler("fixtures", user.view_fixtures))
    application.add_handler(CommandHandler("myreg", user.my_registrations))
    application.add_handler(CommandHandler("participants", user.list_participants))
    application.add_handler(CommandHandler("standings", user.standings))
    application.add_handler(CommandHandler("myid", user.my_id))

    # Admin/manager commands; role checks remain inside their handlers.
    application.add_handler(CommandHandler("newtournament", admin.new_tournament))
    application.add_handler(CommandHandler("addtouradmin", admin.add_tournament_admin))
    application.add_handler(CommandHandler("rmtouradmin", admin.remove_tournament_admin))
    application.add_handler(CommandHandler("setdescription", admin.set_description))
    application.add_handler(CommandHandler("setrules", admin.set_rules))
    application.add_handler(CommandHandler("open", admin.set_tournament_status))
    application.add_handler(CommandHandler("close", admin.set_tournament_status))
    application.add_handler(CommandHandler("complete", admin.set_tournament_status))
    application.add_handler(CommandHandler("generatefixtures", admin.generate_fixtures))
    application.add_handler(CommandHandler("setresult", admin.set_result))
    application.add_handler(CommandHandler("announce", admin.announce))
    application.add_handler(CommandHandler("announceall", admin.announce))
    application.add_handler(
        CommandHandler("unpinannouncement", admin.unpin_announcement)
    )
    application.add_handler(CommandHandler("admintournaments", admin.admin_tournaments))
    application.add_handler(
        CommandHandler("adminparticipants", admin.admin_participants)
    )

    application.add_handler(
        CallbackQueryHandler(user.help_callback, pattern=r"^help:")
    )

    application.add_error_handler(error_handler)
    return application



def main() -> None:
    """Start the bot behind StackHost's HTTPS reverse proxy using webhooks.

    Required environment variables:
      WEBHOOK_URL: Public HTTPS base URL, without the webhook path.
      WEBHOOK_SECRET: Secret accepted by Telegram for authenticating webhook calls.

    Optional environment variables:
      WEBHOOK_PATH: One URL path segment (default: telegram-webhook).
      PORT: Internal port supplied by the hosting platform (default: 8080).
    """
    # --- FIX FOR PYTHON 3.12+ & WINDOWS ---
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    # --------------------------------------

    print("[BOOT] Tournament bot process started (webhook mode)", flush=True)

    try:
        print("[BOOT] Loading configuration...", flush=True)
        settings = Settings.from_env()
        print("[BOOT] Configuration loaded", flush=True)

        import os
        from urllib.parse import urlparse

        webhook_base = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
        webhook_secret = os.getenv("WEBHOOK_SECRET", "").strip()
        webhook_path = os.getenv("WEBHOOK_PATH", "telegram-webhook").strip().strip("/")

        if not webhook_base:
            raise RuntimeError(
                "WEBHOOK_URL is missing. Set it to the public HTTPS base URL "
                "of this StackHost app (for example, https://your-app.example.com)."
            )
        parsed_url = urlparse(webhook_base)
        if parsed_url.scheme != "https" or not parsed_url.netloc or parsed_url.path:
            raise RuntimeError(
                "WEBHOOK_URL must be an HTTPS base URL with no path, "
                "for example https://your-app.example.com."
            )
        if not webhook_secret:
            raise RuntimeError(
                "WEBHOOK_SECRET is missing. Set a random secret containing "
                "1-256 letters, digits, underscores, or hyphens."
            )
        if not (1 <= len(webhook_secret) <= 256) or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for char in webhook_secret
        ):
            raise RuntimeError(
                "WEBHOOK_SECRET must be 1-256 characters using only letters, "
                "digits, underscores, and hyphens."
            )
        if not webhook_path or "/" in webhook_path:
            raise RuntimeError(
                "WEBHOOK_PATH must be a single non-empty path segment, "
                "for example telegram-webhook."
            )

        try:
            port = int(os.getenv("PORT", "8080"))
        except ValueError as exc:
            raise RuntimeError("PORT must be a valid integer.") from exc
        if not (1 <= port <= 65535):
            raise RuntimeError("PORT must be between 1 and 65535.")

        webhook_url = f"{webhook_base}/{webhook_path}"

        print("[BOOT] Initializing database...", flush=True)
        db = Database(settings.mongodb_uri, settings.database_name)

        print("[BOOT] Building Telegram application...", flush=True)
        application = build_application(settings, db)

        print(
            f"[BOOT] Starting webhook listener on 0.0.0.0:{port}; "
            f"public webhook: {webhook_url}",
            flush=True,
        )

        # StackHost terminates HTTPS at its reverse proxy and forwards requests
        # to the internal PORT. Telegram connects to the public HTTPS URL above.
        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=webhook_path,
            webhook_url=webhook_url,
            secret_token=webhook_secret,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
            bootstrap_retries=3,
        )

    except Exception:
        traceback.print_exc(file=sys.stdout)
        raise


if __name__ == "__main__":
    main()