import hashlib
import json
from typing import List
from uuid import uuid4

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from core import (
    GamePhase,
    PlayerGameStatus,
    RollRound,
    Room,
    RoundHistory,
    room_store,
    user_store,
)

ws_router = APIRouter()


async def broadcast_to_room(room: Room, message: dict) -> None:
    text = json.dumps(message)
    dead: List[str] = []
    for uid, ws in room.websockets.items():
        try:
            await ws.send_text(text)
        except Exception:
            dead.append(uid)

    for uid in dead:
        room.websockets.pop(uid, None)


@ws_router.websocket("/ws/rooms/{room_id}")
async def ws_room(websocket: WebSocket, room_id: str, user_id: str = Query(...)) -> None:
    room = room_store.get_room(room_id)
    if room is None:
        await websocket.close(code=1008, reason="Room not found")
        return

    user = user_store.get_user(user_id)
    if user is None:
        await websocket.close(code=1008, reason="User not found")
        return

    await websocket.accept()
    room.websockets[user_id] = websocket

    print(f"User {user.name} connected to room {room.name}")

    await websocket.send_json({
        "type": "room_state",
        "room": {
            "id": room.id,
            "name": room.name,
            "users": [{"id": u.id, "name": u.name} for u in room.users.values()],
        },
        "game": {
            "id": room.game.id,
            "phase": room.game.phase.name,
            "players": [
                {
                    "id": p.user.id,
                    "name": p.user.name,
                    "status": p.status.name
                }
                for p in room.game.players
            ],
            "current_player_id": room.game.current_player().user.id if room.game.current_player() else None,
            "n": room.game.n,
            "m": room.game.m,
        } if room.game else None
    })

    await broadcast_to_room(room, {
        "type": "player_joined",
        "user_id": user_id,
        "name": user.name
    })

    try:
        async for data in websocket.iter_json():
            msg_type = data.get("type")
            print(f"Message from {user.name}: {msg_type}")

            if msg_type == "roll_request":
                game = room.game
                if not game or game.phase != GamePhase.IN_PROGRESS:
                    await websocket.send_json({"type": "error", "reason": "No active game"})
                    continue

                current = game.current_player()
                if not current or current.user.id != user_id:
                    await websocket.send_json({"type": "error", "reason": "Not your turn"})
                    continue

                if current.status != PlayerGameStatus.ACTIVE:
                    await websocket.send_json({"type": "error", "reason": "You are not active"})
                    continue

                async with room.round_lock:
                    round_id = str(uuid4())
                    participants = [p.user.id for p in game.players if p.status == PlayerGameStatus.ACTIVE]

                    roll_round = RollRound(
                        id=round_id,
                        n=game.n,
                        participants=participants
                    )
                    room.current_round = roll_round

                    print(f"🎲 Starting MPC round {round_id} with participants: {participants}")

                    await broadcast_to_room(room, {
                        "type": "commit_phase_start",
                        "round_id": round_id,
                        "n": game.n,
                        "participants": participants
                    })

            elif msg_type == "commit":
                round_id = data.get("round_id")
                commit = data.get("c")

                if not room.current_round or room.current_round.id != round_id:
                    await websocket.send_json({"type": "error", "reason": "Invalid round"})
                    continue

                if room.current_round.phase != "COMMIT":
                    await websocket.send_json({"type": "error", "reason": "Not in commit phase"})
                    continue

                room.current_round.commits[user_id] = commit
                print(f"Commit from {user.name}: {commit[:16]}...")

                if len(room.current_round.commits) == len(room.current_round.participants):
                    room.current_round.phase = "REVEAL"

                    await broadcast_to_room(room, {
                        "type": "reveal_phase_start",
                        "round_id": round_id
                    })

            elif msg_type == "reveal":
                round_id = data.get("round_id")
                a = data.get("a")
                salt = data.get("salt")

                if not room.current_round or room.current_round.id != round_id:
                    await websocket.send_json({"type": "error", "reason": "Invalid round"})
                    continue

                if room.current_round.phase != "REVEAL":
                    await websocket.send_json({"type": "error", "reason": "Not in reveal phase"})
                    continue

                expected_commit = hashlib.sha256(f"{a}|{salt}".encode("utf-8")).hexdigest()
                actual_commit = room.current_round.commits.get(user_id)

                if expected_commit != actual_commit:
                    await websocket.send_json({
                        "type": "error",
                        "reason": "Commitment verification failed",
                        "detail": f"Expected {expected_commit[:16]}..., got {actual_commit[:16] if actual_commit else 'None'}..."
                    })
                    continue

                room.current_round.reveals[user_id] = a
                print(f"Reveal from {user.name}: a={a}")

                if len(room.current_round.reveals) == len(room.current_round.participants):
                    result = sum(room.current_round.reveals.values()) % room.current_round.n
                    print(f"Roll result: {result}")

                    room.current_round.phase = "DONE"

                    room.current_round.salts[user_id] = salt  # Сохраняем salt
                    print(f" Reveal from {user.name}: a={a}")

                    if len(room.current_round.reveals) == len(room.current_round.participants):
                        result = sum(room.current_round.reveals.values()) % room.current_round.n
                        print(f"Roll result: {result}")
                        room.current_round.phase = "DONE"

                        game = room.game
                        if game:
                            current_player = game.current_player()
                            if current_player:
                                current_ws = room.websockets.get(current_player.user.id)
                                if current_ws:
                                    await current_ws.send_json({
                                        "type": "roll_computed",
                                        "round_id": round_id,
                                        "result": result
                                    })
                                    print(f"Sent result {result} ONLY to {current_player.user.name}")

                        if game and current_player:
                            history = RoundHistory(
                                round_id=room.current_round.id,
                                player_id=current_player.user.id,
                                n=room.current_round.n,
                                commits=dict(room.current_round.commits),
                                reveals=dict(room.current_round.reveals),
                                salts=dict(room.current_round.salts),
                                result=result
                            )
                            room.rounds_history.append(history)
                            print(f"Saved round history (total: {len(room.rounds_history)})")

                        room.current_round = None

                    room.current_round = None

            elif msg_type == "roll_success":
                game = room.game
                if not game:
                    await websocket.send_json({"type": "error", "reason": "No active game"})
                    continue

                current = game.current_player()
                if not current or current.user.id != user_id:
                    await websocket.send_json({"type": "error", "reason": "Not your turn"})
                    continue

                print(f"{user.name} successfully rolled and didn't bust")

                game.advance_turn()

                if game.is_game_over():
                    winner_id = game.get_winner()
                    game.phase = GamePhase.FINISHED
                    print(f"Game over! Winner: {winner_id}")

                    await broadcast_to_room(room, {
                        "type": "game_finished",
                        "winner_id": winner_id
                    })
                else:
                    next_player = game.current_player()
                    if next_player:
                        print(f" Next turn: {next_player.user.name}")
                        await broadcast_to_room(room, {
                            "type": "next_turn",
                            "user_id": next_player.user.id,
                            "player_name": next_player.user.name
                        })

            elif msg_type == "declare_busted":
                game = room.game
                if not game:
                    await websocket.send_json({"type": "error", "reason": "No active game"})
                    continue

                current = game.current_player()
                if not current or current.user.id != user_id:
                    await websocket.send_json({"type": "error", "reason": "Not your turn"})
                    continue

                game.mark_busted(user_id)
                print(f"{user.name} busted!")

                await broadcast_to_room(room, {
                    "type": "player_busted",
                    "user_id": user_id
                })

                game.advance_turn()

                if game.is_game_over():
                    winner_id = game.get_winner()
                    game.phase = GamePhase.FINISHED
                    print(f"Game over! Winner: {winner_id}")

                    await broadcast_to_room(room, {
                        "type": "game_finished",
                        "winner_id": winner_id
                    })
                else:
                    next_player = game.current_player()
                    if next_player:
                        print(f"Next turn: {next_player.user.name}")
                        await broadcast_to_room(room, {
                            "type": "next_turn",
                            "user_id": next_player.user.id,
                            "player_name": next_player.user.name
                        })

            elif msg_type == "pass":
                game = room.game
                if not game:
                    await websocket.send_json({"type": "error", "reason": "No active game"})
                    continue

                current = game.current_player()
                if not current or current.user.id != user_id:
                    await websocket.send_json({"type": "error", "reason": "Not your turn"})
                    continue

                game.mark_pass(user_id)
                print(f" {user.name} passed")

                await broadcast_to_room(room, {
                    "type": "player_passed",
                    "user_id": user_id
                })

                game.advance_turn()

                if game.is_game_over():
                    winner_id = game.get_winner()
                    game.phase = GamePhase.FINISHED
                    print(f"🏁 Game over! Winner: {winner_id}")

                    await broadcast_to_room(room, {
                        "type": "game_finished",
                        "winner_id": winner_id
                    })
                else:
                    next_player = game.current_player()
                    if next_player:
                        print(f" Next turn: {next_player.user.name}")
                        await broadcast_to_room(room, {
                            "type": "next_turn",
                            "user_id": next_player.user.id,
                            "player_name": next_player.user.name
                        })

            elif msg_type == "request_final_verification":
                game = room.game
                if not game or game.phase != GamePhase.FINISHED:
                    await websocket.send_json({"type": "error", "reason": "Game not finished"})
                    continue

                print(f" {user.name} requested final verification")
                await broadcast_to_room(room, {
                    "type": "final_verification_start",
                    "rounds_count": len(room.rounds_history)
                })

            elif msg_type == "final_reveal":
                game = room.game
                if not game:
                    await websocket.send_json({"type": "error", "reason": "No game"})
                    continue

                counter = data.get("counter")
                rounds_data = data.get("rounds_data", [])

                print(f" Final reveal from {user.name}: counter={counter}, rounds={len(rounds_data)}")

                verified = True
                expected_counter = 0
                verification_errors = []
                player_rounds = {r.get("round_id"): r for r in rounds_data}

                for history in room.rounds_history:
                    if history.player_id == user_id:
                        player_round = player_rounds.get(history.round_id)

                        if not player_round:
                            verified = False
                            verification_errors.append(f"Missing round {history.round_id}")
                            continue

                        a = player_round.get("a")
                        salt = player_round.get("salt")

                        expected_commit = hashlib.sha256(f"{a}|{salt}".encode("utf-8")).hexdigest()
                        actual_commit = history.commits.get(user_id)

                        if expected_commit != actual_commit:
                            verified = False
                            verification_errors.append(f"Commitment mismatch in round {history.round_id}")
                            continue

                        if history.reveals.get(user_id) != a:
                            verified = False
                            verification_errors.append(f"Reveal mismatch in round {history.round_id}")
                            continue

                        expected_counter += history.result

                if expected_counter != counter:
                    verified = False
                    verification_errors.append(f"Counter mismatch: expected {expected_counter}, got {counter}")

                if counter > game.m:
                    verified = False
                    verification_errors.append(f"Counter {counter} exceeds threshold {game.m}")

                if verified:
                    print(f" {user.name} verification PASSED: counter={counter}")
                else:
                    print(f" {user.name} verification FAILED: {verification_errors}")

                await broadcast_to_room(room, {
                    "type": "player_verification_result",
                    "user_id": user_id,
                    "player_name": user.name,
                    "verified": verified,
                    "counter": counter if verified else None,
                    "errors": verification_errors if not verified else []
                })

            else:
                print(f" Unknown message type: {msg_type}")
                await websocket.send_json({
                    "type": "error",
                    "reason": f"unknown_message_type",
                    "detail": f"Message type '{msg_type}' is not recognized"
                })

    except WebSocketDisconnect:
        print(f" User {user.name} disconnected")
    except Exception as e:
        print(f" WebSocket error for {user.name}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        room.websockets.pop(user_id, None)
        await broadcast_to_room(room, {
            "type": "player_left",
            "user_id": user_id
        })
