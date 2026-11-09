"""MongoDB connection and collection/index setup."""
from __future__ import annotations

from pymongo import ASCENDING
from pymongo import AsyncMongoClient


class Database:
    def __init__(self, uri: str, database_name: str) -> None:
        self.client = AsyncMongoClient(uri, serverSelectionTimeoutMS=10_000)
        self.db = self.client[database_name]
        self.users = self.db["users"]
        self.groups = self.db["groups"]
        self.tournaments = self.db["tournaments"]
        self.registrations = self.db["registrations"]
        self.matches = self.db["matches"]

    async def connect(self) -> None:
        await self.client.admin.command("ping")
        await self.users.create_index([("_id", ASCENDING)])
        await self.groups.create_index([("_id", ASCENDING)])
        await self.tournaments.create_index([("status", ASCENDING), ("created_at", ASCENDING)])
        # This partial unique index guarantees that a group can have only one
        # active tournament, including when two admins issue create commands together.
        await self.tournaments.create_index(
            [("group_id", ASCENDING)],
            unique=True,
            partialFilterExpression={"is_active": True},
            name="one_active_tournament_per_group",
        )
        await self.tournaments.create_index([("group_id", ASCENDING), ("created_at", ASCENDING)])
        await self.registrations.create_index(
            [("tournament_id", ASCENDING), ("user_id", ASCENDING)], unique=True
        )
        await self.registrations.create_index([("tournament_id", ASCENDING), ("registered_at", ASCENDING)])
        await self.matches.create_index(
            [("tournament_id", ASCENDING), ("match_id", ASCENDING)], unique=True
        )
        await self.matches.create_index([("tournament_id", ASCENDING), ("round", ASCENDING)])

    async def close(self) -> None:
        await self.client.close()
