"""Formatting and Telegram helpers."""
from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes


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
    lines = [
        f"🏆 <b>{name}</b>",
        f"ID: <code>{html.escape(tournament['_id'])}</code>",
        f"Group: {html.escape(tournament.get('group_title', 'Unknown group'))}",
        f"Format: {html.escape(tournament['format'].replace('_', ' ').title())}",
        f"Status: {html.escape(tournament['status'].replace('_', ' ').title())}",
        f"Registered: {tournament.get('registered_count', 0)}/{tournament['capacity']}",
    ]
    if include_description and tournament.get("description"):
        description = tournament["description"].strip()
        if len(description) > 400:
            description = description[:397] + "..."
        lines.extend(["", "<b>Description</b>", html.escape(description)])
    return "\n".join(lines)


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
        return f"<code>{match_id}</code>: {first} vs {second} — Winner: {winner} ✅"
    if status == "waiting":
        return f"<code>{match_id}</code>: {first} vs {second} — Waiting for previous results"
    return f"<code>{match_id}</code>: {first} vs {second} — Scheduled"
