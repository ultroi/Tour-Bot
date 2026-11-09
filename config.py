"""Environment-backed configuration for the group tournament bot."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    mongodb_uri: str
    database_name: str
    admin_ids: frozenset[int]
    announcement_pin_hours: int = 24

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("BOT_TOKEN", "").strip()
        mongo_uri = os.getenv("MONGODB_URI", "").strip()
        db_name = os.getenv("DATABASE_NAME", "tournament_manager").strip() or "tournament_manager"
        raw_admins = os.getenv("ADMIN_IDS", "").strip()
        try:
            pin_hours = int(os.getenv("ANNOUNCEMENT_PIN_HOURS", "24").strip())
        except ValueError as exc:
            raise RuntimeError("ANNOUNCEMENT_PIN_HOURS must be an integer.") from exc

        if not token:
            raise RuntimeError("BOT_TOKEN is missing. Add it to your .env file.")
        if not mongo_uri:
            raise RuntimeError("MONGODB_URI is missing. Add it to your .env file.")
        if pin_hours < 0 or pin_hours > 8760:
            raise RuntimeError("ANNOUNCEMENT_PIN_HOURS must be between 0 and 8760 (0 disables auto-unpin).")

        admin_ids: set[int] = set()
        if raw_admins:
            for value in raw_admins.split(","):
                value = value.strip()
                if value:
                    try:
                        admin_ids.add(int(value))
                    except ValueError as exc:
                        raise RuntimeError(f"Invalid Telegram admin ID: {value!r}") from exc

        # ADMIN_IDS is optional: ordinary Telegram group admins can create tournaments.
        # IDs here are bot-wide owners who can also create/manage tournaments.
        return cls(
            bot_token=token,
            mongodb_uri=mongo_uri,
            database_name=db_name,
            admin_ids=frozenset(admin_ids),
            announcement_pin_hours=pin_hours,
        )
