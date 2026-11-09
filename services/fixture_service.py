"""Pure fixture-generation helpers (no Telegram or database dependencies)."""
from __future__ import annotations
from typing import Any


def _round_robin(participants: list[dict[str, Any]], tournament_id: str) -> list[dict[str, Any]]:
    pool: list[dict[str, Any] | None] = list(participants)
    if len(pool) % 2:
        pool.append(None)
    half = len(pool) // 2
    fixtures: list[dict[str, Any]] = []
    for round_index in range(len(pool) - 1):
        match_number = 0
        for index in range(half):
            left, right = pool[index], pool[-(index + 1)]
            if left is None or right is None:
                bye_player = left or right
                if bye_player:
                    fixtures.append({
                        "_id": f"{tournament_id}:R{round_index + 1}-BYE{match_number + 1}",
                        "tournament_id": tournament_id,
                        "match_id": f"R{round_index + 1}-BYE{match_number + 1}",
                        "round": round_index + 1,
                        "match_no": match_number + 1,
                        "player1_id": bye_player["user_id"],
                        "player2_id": None,
                        "player1_name": bye_player["name"],
                        "player2_name": None,
                        "status": "bye",
                        "winner_id": bye_player["user_id"],
                    })
                continue
            match_number += 1
            fixtures.append({
                "_id": f"{tournament_id}:R{round_index + 1}-M{match_number}",
                "tournament_id": tournament_id,
                "match_id": f"R{round_index + 1}-M{match_number}",
                "round": round_index + 1,
                "match_no": match_number,
                "player1_id": left["user_id"],
                "player2_id": right["user_id"],
                "player1_name": left["name"],
                "player2_name": right["name"],
                "status": "scheduled",
                "winner_id": None,
            })
        pool = [pool[0], pool[-1], *pool[1:-1]]
    return fixtures


def _knockout(participants: list[dict[str, Any]], tournament_id: str) -> list[dict[str, Any]]:
    count = len(participants)
    bracket_size = 1
    while bracket_size < count:
        bracket_size *= 2
    bye_count = bracket_size - count
    first_round_pairs: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] = []
    cursor = 0
    for _ in range(bye_count):
        first_round_pairs.append((participants[cursor], None))
        cursor += 1
    while cursor < count:
        first_round_pairs.append((participants[cursor], participants[cursor + 1]))
        cursor += 2

    matches: list[dict[str, Any]] = []
    final_round = bracket_size.bit_length() - 1
    for round_number in range(1, final_round + 1):
        for match_number in range(1, bracket_size // (2**round_number) + 1):
            match_id = f"R{round_number}-M{match_number}"
            match: dict[str, Any] = {
                "_id": f"{tournament_id}:{match_id}", "tournament_id": tournament_id,
                "match_id": match_id, "round": round_number, "match_no": match_number,
                "player1_id": None, "player2_id": None, "player1_name": None,
                "player2_name": None, "status": "waiting", "winner_id": None,
                "next_match_id": None, "next_slot": None,
            }
            if round_number < final_round:
                match["next_match_id"] = f"R{round_number + 1}-M{(match_number + 1) // 2}"
                match["next_slot"] = 1 if match_number % 2 else 2
            matches.append(match)

    match_by_id = {item["match_id"]: item for item in matches}
    first_round = [item for item in matches if item["round"] == 1]
    for match, (player1, player2) in zip(first_round, first_round_pairs, strict=True):
        for slot, player in ((1, player1), (2, player2)):
            if player:
                match[f"player{slot}_id"] = player["user_id"]
                match[f"player{slot}_name"] = player["name"]
        if player1 and player2:
            match["status"] = "scheduled"
        else:
            bye_player = player1 or player2
            if bye_player:
                match["status"] = "bye"
                match["winner_id"] = bye_player["user_id"]
                if match["next_match_id"]:
                    target = match_by_id[match["next_match_id"]]
                    slot = int(match["next_slot"])
                    target[f"player{slot}_id"] = bye_player["user_id"]
                    target[f"player{slot}_name"] = bye_player["name"]
    for match in matches:
        if match["round"] > 1 and match["player1_id"] and match["player2_id"]:
            match["status"] = "scheduled"
    return matches


def generate_fixtures(participants: list[dict[str, Any]], tournament_id: str, tournament_format: str) -> list[dict[str, Any]]:
    if len(participants) < 2:
        raise ValueError("At least two registered participants are required.")
    if tournament_format == "round_robin":
        return _round_robin(participants, tournament_id)
    if tournament_format == "knockout":
        return _knockout(participants, tournament_id)
    raise ValueError(f"Unsupported tournament format: {tournament_format}")
