"""Tournament business rules and MongoDB operations."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from database import Database
from services.fixture_service import generate_fixtures

VALID_FORMATS = {"knockout", "round_robin"}
VALID_STATUSES = {"open", "closed", "ongoing", "completed", "cancelled"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TournamentService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create_tournament(
        self,
        name: str,
        tournament_format: str,
        capacity: int,
        manager_id: int,
        manager_name: str,
        group_id: int,
        group_title: str,
        description: str = "",
    ) -> dict[str, Any]:
        normalized_format = tournament_format.strip().lower().replace("-", "_")
        if normalized_format not in VALID_FORMATS:
            raise ValueError("Format must be `knockout` or `round_robin`.")
        if capacity < 0 or capacity > 512 or (capacity != 0 and capacity < 2):
            raise ValueError("Capacity must be 0 (unlimited) or a number between 2 and 512.")
        clean_name = name.strip()
        if not 3 <= len(clean_name) <= 80:
            raise ValueError("Tournament name must be between 3 and 80 characters.")
        clean_description = description.strip()
        if len(clean_description) > 2000:
            raise ValueError("Description must be 2,000 characters or fewer.")

        tournament_id = secrets.token_hex(3).upper()
        document = {
            "_id": tournament_id,
            "name": clean_name,
            "description": clean_description,
            "format": normalized_format,
            "capacity": capacity,
            "registered_count": 0,
            "status": "open",
            # Empty means rules have not been configured; this controls the Rules button.
            "rules": "",
            "group_id": int(group_id),
            "group_title": group_title,
            "manager_id": int(manager_id),
            "manager_name": (manager_name or "Tournament Manager")[:64],
            "tournament_admins": [],
            "tournament_admin_names": {},
            "created_by": int(manager_id),
            "created_at": utc_now(),
            "fixtures_generated": False,
            "is_active": True,
            "last_announcement_message_id": None,
            "announcement_pin_until": None,
            "tournament_message_id": None,
            "tournament_message_chat_id": int(group_id),
            "tournament_message_pinned": False,
        }
        try:
            await self.db.tournaments.insert_one(document)
        except DuplicateKeyError as exc:
            raise ValueError("This group already has an active tournament. Complete it before creating another.") from exc
        return document

    async def get_tournament(self, tournament_id: str) -> dict[str, Any] | None:
        return await self.db.tournaments.find_one({"_id": tournament_id.strip().upper()})

    async def get_group_tournament(self, group_id: int, tournament_id: str | None = None) -> dict[str, Any] | None:
        query: dict[str, Any] = {"group_id": int(group_id)}
        if tournament_id:
            query["_id"] = tournament_id.strip().upper()
        else:
            query["is_active"] = True
        return await self.db.tournaments.find_one(query, sort=[("created_at", -1)])

    async def get_active_tournament(self, group_id: int) -> dict[str, Any] | None:
        return await self.get_group_tournament(group_id)

    async def list_tournaments(self, status: str | None = None, group_id: int | None = None) -> list[dict[str, Any]]:
        query: dict[str, Any] = {}
        if status:
            query["status"] = status
        if group_id is not None:
            query["group_id"] = int(group_id)
        cursor = self.db.tournaments.find(query).sort("created_at", -1).limit(25)
        return await cursor.to_list(length=25)

    async def set_rules(self, tournament_id: str, rules: str) -> bool:
        result = await self.db.tournaments.update_one(
            {"_id": tournament_id.upper()}, {"$set": {"rules": rules.strip()}}
        )
        return result.matched_count > 0

    async def set_description(self, tournament_id: str, description: str) -> bool:
        if len(description.strip()) > 2000:
            raise ValueError("Description must be 2,000 characters or fewer.")
        result = await self.db.tournaments.update_one(
            {"_id": tournament_id.upper()}, {"$set": {"description": description.strip()}}
        )
        return result.matched_count > 0

    async def set_status(self, tournament_id: str, status: str) -> bool:
        normalized = status.strip().lower()
        if normalized not in VALID_STATUSES:
            raise ValueError("Status must be open, closed, ongoing, completed, or cancelled.")
        tournament = await self.get_tournament(tournament_id)
        if not tournament:
            return False
        if normalized in {"open", "closed"} and (tournament.get("fixtures_generated") or tournament.get("status") == "generating"):
            raise ValueError("Registration status cannot be changed after fixture generation has started.")
        if normalized == "ongoing" and not tournament.get("fixtures_generated"):
            raise ValueError("Generate fixtures before marking a tournament as ongoing.")
        update = {"status": normalized, "is_active": normalized not in {"completed", "cancelled"}}
        result = await self.db.tournaments.update_one({"_id": tournament["_id"]}, {"$set": update})
        return result.matched_count > 0

    async def add_tournament_admin(self, tournament_id: str, user_id: int, first_name: str = "Tournament Admin") -> str:
        tournament = await self.get_tournament(tournament_id)
        if not tournament:
            return "not_found"
        if int(user_id) == int(tournament["manager_id"]):
            return "is_manager"
        if int(user_id) in tournament.get("tournament_admins", []):
            return "already_admin"
        await self.db.tournaments.update_one(
            {"_id": tournament["_id"]},
            {
                "$addToSet": {"tournament_admins": int(user_id)},
                "$set": {f"tournament_admin_names.{int(user_id)}": (first_name or "Tournament Admin")[:64]},
            },
        )
        return "added"

    async def remove_tournament_admin(self, tournament_id: str, user_id: int) -> str:
        tournament = await self.get_tournament(tournament_id)
        if not tournament:
            return "not_found"
        result = await self.db.tournaments.update_one(
            {"_id": tournament["_id"]},
            {
                "$pull": {"tournament_admins": int(user_id)},
                "$unset": {f"tournament_admin_names.{int(user_id)}": ""},
            },
        )
        return "removed" if result.modified_count else "not_admin"

    async def register(
        self,
        tournament_id: str,
        group_id: int,
        user_id: int,
        first_name: str,
        username: str | None,
    ) -> str:
        """Register using Telegram's first name; callers must be in the group."""
        tid = tournament_id.upper()
        tournament = await self.db.tournaments.find_one({"_id": tid, "group_id": int(group_id), "is_active": True})
        if not tournament:
            return "not_found"
        if tournament["status"] != "open":
            return "closed"
        if await self.db.registrations.find_one({"tournament_id": tid, "user_id": int(user_id)}):
            return "already_registered"

        reserved = await self.db.tournaments.find_one_and_update(
            {
                "_id": tid,
                "group_id": int(group_id),
                "status": "open",
                "is_active": True,
                # A capacity of zero is the persisted representation of unlimited.
                "$expr": {
                    "$or": [
                        {"$lte": [{"$ifNull": ["$capacity", 0]}, 0]},
                        {"$lt": [{"$ifNull": ["$registered_count", 0]}, "$capacity"]},
                    ]
                },
            },
            {"$inc": {"registered_count": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if not reserved:
            latest = await self.db.tournaments.find_one({"_id": tid, "group_id": int(group_id)})
            if not latest or not latest.get("is_active", False):
                return "not_found"
            if latest["status"] != "open":
                return "closed"
            return "full"

        registration = {
            "tournament_id": tid,
            "group_id": int(group_id),
            "user_id": int(user_id),
            "name": (first_name or "Player").strip()[:50],
            "username": username,
            "registered_at": utc_now(),
        }
        try:
            await self.db.registrations.insert_one(registration)
        except DuplicateKeyError:
            await self.db.tournaments.update_one({"_id": tid}, {"$inc": {"registered_count": -1}})
            return "already_registered"
        except Exception:
            await self.db.tournaments.update_one({"_id": tid}, {"$inc": {"registered_count": -1}})
            raise
        return "registered"

    async def unregister(self, tournament_id: str, group_id: int, user_id: int) -> str:
        tid = tournament_id.upper()
        tournament = await self.db.tournaments.find_one({"_id": tid, "group_id": int(group_id)})
        if not tournament:
            return "not_found"
        if tournament.get("fixtures_generated"):
            return "fixtures_exist"
        result = await self.db.registrations.delete_one(
            {"tournament_id": tid, "group_id": int(group_id), "user_id": int(user_id)}
        )
        if result.deleted_count:
            await self.db.tournaments.update_one({"_id": tid}, {"$inc": {"registered_count": -1}})
            return "unregistered"
        return "not_registered"

    async def get_participants(self, tournament_id: str) -> list[dict[str, Any]]:
        cursor = self.db.registrations.find({
            "tournament_id": tournament_id.upper(),
            "eliminated": {"$ne": True},
            "disqualified": {"$ne": True},
        }).sort("registered_at", ASCENDING)
        registrations = await cursor.to_list(length=512)
        return [
            {"user_id": item["user_id"], "name": item["name"], "username": item.get("username")}
            for item in registrations
        ]

    async def generate(self, tournament_id: str, group_id: int) -> list[dict[str, Any]]:
        tid = tournament_id.upper()
        tournament = await self.db.tournaments.find_one({"_id": tid, "group_id": int(group_id), "is_active": True})
        if not tournament:
            raise LookupError("No active tournament with that ID was found in this group.")
        if tournament.get("fixtures_generated"):
            raise ValueError("Fixtures have already been generated for this tournament.")
        if tournament.get("status") not in {"open", "closed"}:
            raise ValueError("Fixtures can only be generated while registration is open or closed.")

        claim = await self.db.tournaments.update_one(
            {"_id": tid, "group_id": int(group_id), "status": {"$in": ["open", "closed"]}, "fixtures_generated": {"$ne": True}},
            {"$set": {"status": "generating"}},
        )
        if not claim.modified_count:
            raise ValueError("Fixture generation is already running or has already completed.")
        try:
            await self.db.matches.delete_many({"tournament_id": tid})
            participants = await self.get_participants(tid)
            fixtures = generate_fixtures(participants, tid, tournament["format"])
            await self.db.matches.insert_many(fixtures)
            await self.db.tournaments.update_one(
                {"_id": tid, "status": "generating"},
                {"$set": {"fixtures_generated": True, "fixtures_finalized": False, "status": "ongoing", "fixtures_generated_at": utc_now()}, "$unset": {"fixture_round_message_ids": ""}},
            )
            return fixtures
        except Exception:
            await self.db.matches.delete_many({"tournament_id": tid})
            await self.db.tournaments.update_one(
                {"_id": tid, "status": "generating"},
                {"$set": {"status": tournament["status"], "fixtures_generated": False}, "$unset": {"fixtures_generated_at": ""}},
            )
            raise

    async def get_matches(self, tournament_id: str) -> list[dict[str, Any]]:
        cursor = self.db.matches.find({"tournament_id": tournament_id.upper()}).sort([("round", ASCENDING), ("match_no", ASCENDING)])
        return await cursor.to_list(length=2048)

    async def replace_fixture_draft(
        self,
        tournament_id: str,
        group_id: int,
        matches: list[dict[str, Any]],
    ) -> bool:
        """Replace fixtures before the first result is recorded.

        The editor works on a private in-memory draft; this method is the single
        commit point reached only after an admin presses Done.
        """
        tid = tournament_id.upper()
        tournament = await self.db.tournaments.find_one({
            "_id": tid,
            "group_id": int(group_id),
            "fixtures_generated": True,
        })
        if not tournament:
            raise LookupError("Generated fixtures for this tournament were not found.")
        if not matches:
            raise ValueError("The fixture draft is empty.")
        completed = await self.db.matches.count_documents({
            "tournament_id": tid,
            "status": "completed",
        })
        if completed:
            raise ValueError("Fixtures cannot be edited after a match result has been recorded.")

        clean_matches: list[dict[str, Any]] = []
        for source in matches:
            item = dict(source)
            item.pop("_id", None)
            item["tournament_id"] = tid
            clean_matches.append(item)

        previous_matches = await self.get_matches(tid)
        await self.db.matches.delete_many({"tournament_id": tid})
        try:
            await self.db.matches.insert_many(clean_matches)
        except Exception:
            # Restore the previous committed schedule if the draft insert fails.
            await self.db.matches.delete_many({"tournament_id": tid})
            if previous_matches:
                await self.db.matches.insert_many(previous_matches)
            raise
        await self.db.tournaments.update_one(
            {"_id": tid, "group_id": int(group_id)},
            {"$set": {
                "status": "ongoing",
                "is_active": True,
                "fixtures_finalized": True,
                "fixtures_finalized_at": utc_now(),
            }, "$unset": {"fixture_round_message_ids": ""}},
        )
        return True

    async def _save_match_winner(
        self,
        tournament_id: str,
        match: dict[str, Any],
        winner_id: int,
        *,
        eliminated_loser: bool = False,
    ) -> str:
        """Complete a scheduled match and move its winner to the next bracket slot."""
        if match.get("status") != "scheduled":
            return "match_not_ready"
        winner_id = int(winner_id)
        player_ids = {match.get("player1_id"), match.get("player2_id")}
        if winner_id not in player_ids:
            return "winner_not_in_match"

        winner_slot = 1 if winner_id == match.get("player1_id") else 2
        loser_slot = 2 if winner_slot == 1 else 1
        winner_name = match.get(f"player{winner_slot}_name") or "Player"
        update_fields: dict[str, Any] = {
            "status": "completed",
            "winner_id": winner_id,
            "winner_name": winner_name,
            "completed_at": utc_now(),
        }
        if eliminated_loser:
            update_fields["eliminated_id"] = match.get(f"player{loser_slot}_id")
            update_fields["eliminated_name"] = match.get(f"player{loser_slot}_name") or "Player"

        updated = await self.db.matches.update_one(
            {"_id": match["_id"], "status": "scheduled"},
            {"$set": update_fields},
        )
        if not updated.modified_count:
            return "match_not_ready"

        tid = tournament_id.upper()
        next_match_id = match.get("next_match_id")
        if next_match_id:
            next_slot = int(match["next_slot"])
            await self.db.matches.update_one(
                {"tournament_id": tid, "match_id": next_match_id},
                {"$set": {f"player{next_slot}_id": winner_id, f"player{next_slot}_name": winner_name}},
            )
            next_match = await self.db.matches.find_one({"tournament_id": tid, "match_id": next_match_id})
            if next_match and next_match.get("player1_id") and next_match.get("player2_id"):
                await self.db.matches.update_one(
                    {"tournament_id": tid, "match_id": next_match_id, "status": "waiting"},
                    {"$set": {"status": "scheduled"}},
                )
        else:
            tournament = await self.db.tournaments.find_one({"_id": tid}, {"format": 1})
            if tournament and tournament.get("format") == "knockout":
                await self.db.tournaments.update_one(
                    {"_id": tid},
                    {"$set": {"status": "completed", "is_active": False, "winner_id": winner_id, "winner_name": winner_name}},
                )
        return "result_saved"

    async def set_result(self, tournament_id: str, match_id: str, winner_id: int) -> str:
        tid = tournament_id.upper()
        match = await self.db.matches.find_one({"tournament_id": tid, "match_id": match_id.upper()})
        if not match:
            return "match_not_found"
        if match.get("status") != "scheduled":
            return "match_not_ready"
        return await self._save_match_winner(tid, match, winner_id)

    async def eliminate(
        self,
        tournament_id: str,
        loser_id: int,
        match_id: str | None = None,
        eliminated_by: int | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        """Eliminate a knockout loser, advance the opponent, and hide the loser from active rosters.

        Registration records are retained for history; the eliminated flag removes the
        player from active participant lists without erasing their match history. The
        command can also mark a loser after a manager has already saved the match result.
        """
        tid = tournament_id.upper()
        tournament = await self.db.tournaments.find_one({"_id": tid})
        if not tournament:
            return "tournament_not_found", None
        if tournament.get("format") != "knockout":
            return "not_knockout", None

        loser_id = int(loser_id)
        if match_id:
            match = await self.db.matches.find_one(
                {"tournament_id": tid, "match_id": match_id.upper()}
            )
            if not match:
                return "match_not_found", None
        else:
            # Prefer a live scheduled match. If /setresult was already used, fall
            # back to a completed match where this player was recorded as the loser.
            candidates = await self.db.matches.find(
                {
                    "tournament_id": tid,
                    "status": "scheduled",
                    "$or": [{"player1_id": loser_id}, {"player2_id": loser_id}],
                }
            ).to_list(length=2)
            if len(candidates) > 1:
                return "multiple_matches", None
            if candidates:
                match = candidates[0]
            else:
                candidates = await self.db.matches.find(
                    {
                        "tournament_id": tid,
                        "status": "completed",
                        "winner_id": {"$ne": loser_id},
                        "$or": [{"player1_id": loser_id}, {"player2_id": loser_id}],
                    }
                ).to_list(length=2)
                if not candidates:
                    return "match_not_found", None
                if len(candidates) > 1:
                    return "multiple_matches", None
                match = candidates[0]

        player1_id, player2_id = match.get("player1_id"), match.get("player2_id")
        if loser_id not in {player1_id, player2_id}:
            return "loser_not_in_match", None

        registration = await self.db.registrations.find_one(
            {"tournament_id": tid, "user_id": loser_id}
        )
        if not registration:
            return "participant_not_found", None
        if registration.get("eliminated") or registration.get("disqualified"):
            return "already_eliminated", None

        if match.get("status") == "scheduled":
            winner_id = player2_id if loser_id == player1_id else player1_id
            result = await self._save_match_winner(
                tid, match, int(winner_id), eliminated_loser=True
            )
            if result != "result_saved":
                return result, None
        elif match.get("status") == "completed":
            # A normal /setresult may already have advanced the winner. Do not
            # complete or advance this match a second time.
            winner_id = match.get("winner_id")
            if winner_id not in {player1_id, player2_id} or winner_id == loser_id:
                return "match_not_ready", None
            loser_slot = 1 if loser_id == player1_id else 2
            await self.db.matches.update_one(
                {"_id": match["_id"], "status": "completed", "winner_id": winner_id},
                {"$set": {
                    "eliminated_id": loser_id,
                    "eliminated_name": match.get(f"player{loser_slot}_name") or registration.get("name") or "Player",
                }},
            )
        else:
            return "match_not_ready", None

        eliminated_at = utc_now()
        elimination_update = await self.db.registrations.update_one(
            {"tournament_id": tid, "user_id": loser_id, "eliminated": {"$ne": True}},
            {"$set": {
                "eliminated": True,
                "eliminated_at": eliminated_at,
                "eliminated_by": int(eliminated_by) if eliminated_by is not None else None,
            }},
        )
        if elimination_update.modified_count:
            await self.db.tournaments.update_one(
                {"_id": tid, "registered_count": {"$gt": 0}},
                {"$inc": {"registered_count": -1}},
            )

        winner_slot = 1 if winner_id == player1_id else 2
        details = {
            "match_id": match["match_id"],
            "winner_id": int(winner_id),
            "winner_name": match.get(f"player{winner_slot}_name") or "Player",
            "loser_id": loser_id,
            "loser_name": registration.get("name") or "Player",
        }
        return "eliminated", details


    async def disqualify_round_robin(
        self,
        tournament_id: str,
        user_id: int,
        disqualified_by: int | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        """Disqualify a round-robin participant without deleting tournament history.

        Pending fixtures involving the player are cancelled. Completed results are
        retained in match history, but standings are recalculated from active
        participants, so all results involving the disqualified player are excluded.
        """
        tid = tournament_id.upper()
        tournament = await self.db.tournaments.find_one({"_id": tid})
        if not tournament:
            return "tournament_not_found", None
        if tournament.get("format") != "round_robin":
            return "not_round_robin", None
        if not tournament.get("fixtures_generated"):
            return "fixtures_not_generated", None

        player_id = int(user_id)
        registration = await self.db.registrations.find_one(
            {"tournament_id": tid, "user_id": player_id}
        )
        if not registration:
            return "participant_not_found", None
        if registration.get("disqualified") or registration.get("eliminated"):
            return "already_disqualified", None

        disqualified_at = utc_now()
        update = await self.db.registrations.update_one(
            {
                "tournament_id": tid,
                "user_id": player_id,
                "disqualified": {"$ne": True},
                "eliminated": {"$ne": True},
            },
            {"$set": {
                "disqualified": True,
                "disqualified_at": disqualified_at,
                "disqualified_by": int(disqualified_by) if disqualified_by is not None else None,
            }},
        )
        if not update.modified_count:
            return "already_disqualified", None

        # Do not award automatic wins/points to opponents: pending games are void.
        cancelled = await self.db.matches.update_many(
            {
                "tournament_id": tid,
                "status": {"$in": ["scheduled", "bye"]},
                "$or": [{"player1_id": player_id}, {"player2_id": player_id}],
            },
            {"$set": {
                "status": "cancelled",
                "disqualified_id": player_id,
                "disqualified_name": registration.get("name") or "Player",
                "cancelled_reason": "player_disqualified",
                "cancelled_at": disqualified_at,
            }},
        )
        await self.db.tournaments.update_one(
            {"_id": tid, "registered_count": {"$gt": 0}},
            {"$inc": {"registered_count": -1}},
        )
        return "disqualified", {
            "user_id": player_id,
            "name": registration.get("name") or "Player",
            "cancelled_matches": int(cancelled.modified_count),
        }
