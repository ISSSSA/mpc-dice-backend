from __future__ import annotations

from enum import Enum, auto
from uuid import uuid4
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import json
from fastapi import Query
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import hashlib
import uuid
import asyncio


# ====== MPC Commitment Round ======

@dataclass
class RollRound:
    """
    Представляет один раунд совместной генерации случайного числа (MPC).
    Сервер координирует обмен коммитментами и раскрытиями, но не знает результат до reveal.
    """
    id: str
    n: int  # диапазон [0, n-1]
    participants: List[str]  # user_id участников
    commits: Dict[str, str] = field(default_factory=dict)  # user_id -> commitment c_j
    reveals: Dict[str, int] = field(default_factory=dict)  # user_id -> revealed value a_j
    phase: str = "COMMIT"  # COMMIT -> REVEAL -> DONE


# ====== Domain Models ======

class PlayerGameStatus(Enum):
    """Статус игрока в игре (на сервере отслеживается только pass/bust, не счетчик)"""
    ACTIVE = auto()
    PASSED = auto()
    BUSTED = auto()


@dataclass
class User:
    id: str
    name: str


@dataclass
class PlayerInGame:
    """
    Игрок в игре.
    ВАЖНО: счетчик x_i хранится ТОЛЬКО на клиенте, сервер не знает его значения!
    """
    user: User
    status: PlayerGameStatus = PlayerGameStatus.ACTIVE


class GamePhase(Enum):
    WAITING_FOR_PLAYERS = auto()
    IN_PROGRESS = auto()
    FINISHED = auto()


@dataclass
class GameState:
    """Состояние игры на сервере (публичная информация)"""
    id: str
    n: int  # размер кубика
    m: int  # порог проигрыша
    players: List[PlayerInGame] = field(default_factory=list)
    phase: GamePhase = GamePhase.WAITING_FOR_PLAYERS
    current_turn_index: int = 0

    def current_player(self) -> Optional[PlayerInGame]:
        """Возвращает игрока, чей сейчас ход"""
        if not self.players or self.current_turn_index >= len(self.players):
            return None
        return self.players[self.current_turn_index]

    def advance_turn(self) -> None:
        """Переход к следующему активному игроку"""
        if not self.players:
            return

        # Ищем следующего активного игрока
        for _ in range(len(self.players)):
            self.current_turn_index = (self.current_turn_index + 1) % len(self.players)
            current = self.current_player()
            if current and current.status == PlayerGameStatus.ACTIVE:
                return

        # Если не нашли активных игроков, игра окончена
        self.phase = GamePhase.FINISHED

    def mark_pass(self, user_id: str) -> None:
        """Помечает игрока как спасовавшего"""
        for p in self.players:
            if p.user.id == user_id:
                p.status = PlayerGameStatus.PASSED
                return

    def mark_busted(self, user_id: str) -> None:
        """Помечает игрока как проигравшего (вылетевшего)"""
        for p in self.players:
            if p.user.id == user_id:
                p.status = PlayerGameStatus.BUSTED
                return

    def is_game_over(self) -> bool:
        """Игра окончена, если активных игроков <= 1"""
        active_count = sum(1 for p in self.players if p.status == PlayerGameStatus.ACTIVE)
        return active_count <= 1

    def get_winner(self) -> Optional[str]:
        """
        Возвращает ID победителя.
        Победитель = последний активный или последний спасовавший игрок.
        Сервер не знает счетчики x_i, поэтому клиенты сами определяют победителя.
        """
        active = [p for p in self.players if p.status == PlayerGameStatus.ACTIVE]
        if len(active) == 1:
            return active[0].user.id

        passed = [p for p in self.players if p.status == PlayerGameStatus.PASSED]
        if passed:
            # Если все активные вылетели, побеждает последний спасовавший
            # (логика может быть доработана на клиенте)
            return passed[-1].user.id

        return None


@dataclass
class Room:
    """Комната с игроками и игрой"""
    id: str
    name: str
    users: Dict[str, User] = field(default_factory=dict)
    game: Optional[GameState] = None
    is_active: bool = False
    websockets: Dict[str, WebSocket] = field(default_factory=dict)

    # MPC state
    current_round: Optional[RollRound] = None
    round_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# ====== Stores ======

class InMemoryUserStore:
    def __init__(self) -> None:
        self._users_by_id: Dict[str, User] = {}
        self._users_by_name: Dict[str, User] = {}

    def create_user(self, name: str) -> User:
        if name in self._users_by_name:
            raise ValueError("User with this name already exists")
        user_id = str(uuid4())
        user = User(id=user_id, name=name)
        self._users_by_id[user_id] = user
        self._users_by_name[name] = user
        return user

    def get_user(self, user_id: str) -> Optional[User]:
        return self._users_by_id.get(user_id)

    def delete_user(self, user_id: str) -> None:
        user = self._users_by_id.pop(user_id, None)
        if user:
            self._users_by_name.pop(user.name, None)

    def all_users(self) -> List[User]:
        return list(self._users_by_id.values())


class InMemoryRoomStore:
    def __init__(self) -> None:
        self._rooms: Dict[str, Room] = {}

    def create_room(self, name: str) -> Room:
        room_id = str(uuid4())
        room = Room(id=room_id, name=name)
        self._rooms[room_id] = room
        return room

    def get_room(self, room_id: str) -> Optional[Room]:
        return self._rooms.get(room_id)

    def all_rooms(self) -> List[Room]:
        return list(self._rooms.values())

    def delete_room(self, room_id: str) -> None:
        self._rooms.pop(room_id, None)


# ====== Services ======

class UserService:
    def __init__(self, user_store: InMemoryUserStore) -> None:
        self._user_store = user_store

    def register(self, name: str) -> User:
        try:
            return self._user_store.create_user(name=name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def remove(self, user_id: str) -> None:
        self._user_store.delete_user(user_id)

    def get(self, user_id: str) -> User:
        user = self._user_store.get_user(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        return user

    def list_users(self) -> List[User]:
        return self._user_store.all_users()


class RoomService:
    def __init__(self, room_store: InMemoryRoomStore, user_store: InMemoryUserStore) -> None:
        self._room_store = room_store
        self._user_store = user_store

    def create_room(self, name: str) -> Room:
        return self._room_store.create_room(name=name)

    def list_rooms(self) -> List[Room]:
        return self._room_store.all_rooms()

    def join_room(self, room_id: str, user_id: str) -> Room:
        room = self._room_store.get_room(room_id)
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found")

        user = self._user_store.get_user(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        room.users[user_id] = user
        return room

    def start_game(self, room_id: str, n: int, m: int) -> GameState:
        room = self._room_store.get_room(room_id)
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found")

        if len(room.users) < 2:
            raise HTTPException(status_code=400, detail="Need at least 2 players to start")

        if room.game is not None and room.game.phase == GamePhase.IN_PROGRESS:
            raise HTTPException(status_code=400, detail="Game already in progress")

        game_id = str(uuid4())
        players = [PlayerInGame(user=u) for u in room.users.values()]
        game = GameState(id=game_id, n=n, m=m, players=players, phase=GamePhase.IN_PROGRESS)
        room.game = game
        room.is_active = True
        return game


# ====== DTO / Schemas ======

class UserCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=32)


class UserResponse(BaseModel):
    id: str
    name: str


class RoomCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class RoomResponse(BaseModel):
    id: str
    name: str
    user_count: int
    is_active: bool


class JoinRoomRequest(BaseModel):
    user_id: str


class StartGameRequest(BaseModel):
    n: int = Field(ge=2, le=256)
    m: int = Field(ge=1)


class GamePublicState(BaseModel):
    id: str
    phase: str
    players: List[str]
    current_player_id: Optional[str]
    n: int
    m: int


# ====== FastAPI App ======

app = FastAPI(title="MPC Dice Game Backend (Non-Trusted Server)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

user_store = InMemoryUserStore()
room_store = InMemoryRoomStore()
user_service = UserService(user_store=user_store)
room_service = RoomService(room_store=room_store, user_store=user_store)


# ====== REST Endpoints ======

@app.post("/users", response_model=UserResponse)
def register_user(payload: UserCreateRequest) -> UserResponse:
    """Регистрация пользователя (эфемерного)"""
    user = user_service.register(name=payload.name)
    return UserResponse(id=user.id, name=user.name)


@app.post("/rooms", response_model=RoomResponse)
def create_room(payload: RoomCreateRequest) -> RoomResponse:
    """Создание комнаты"""
    room = room_service.create_room(name=payload.name)
    return RoomResponse(
        id=room.id,
        name=room.name,
        user_count=len(room.users),
        is_active=room.is_active,
    )


@app.get("/rooms", response_model=List[RoomResponse])
def list_rooms() -> List[RoomResponse]:
    """Список всех комнат"""
    rooms = room_store.all_rooms()
    return [
        RoomResponse(
            id=r.id,
            name=r.name,
            user_count=len(r.users),
            is_active=r.is_active,
        )
        for r in rooms
    ]


@app.post("/rooms/{room_id}/join", response_model=RoomResponse)
def join_room(room_id: str, payload: JoinRoomRequest) -> RoomResponse:
    """Присоединение к комнате"""
    room = room_service.join_room(room_id=room_id, user_id=payload.user_id)
    return RoomResponse(
        id=room.id,
        name=room.name,
        user_count=len(room.users),
        is_active=room.is_active,
    )


@app.post("/rooms/{room_id}/start", response_model=GamePublicState)
def start_game(room_id: str, payload: StartGameRequest) -> GamePublicState:
    """Старт игры"""
    game = room_service.start_game(room_id=room_id, n=payload.n, m=payload.m)
    current = game.current_player()
    return GamePublicState(
        id=game.id,
        phase=game.phase.name,
        players=[p.user.id for p in game.players],
        current_player_id=current.user.id if current else None,
        n=game.n,
        m=game.m,
    )


# ====== WebSocket Helpers ======

async def broadcast_to_room(room: Room, message: dict) -> None:
    """Рассылка сообщения всем в комнате"""
    text = json.dumps(message)
    dead: List[str] = []
    for uid, ws in room.websockets.items():
        try:
            await ws.send_text(text)
        except Exception:
            dead.append(uid)

    # Удаляем мертвые соединения
    for uid in dead:
        room.websockets.pop(uid, None)


@app.websocket("/ws/rooms/{room_id}")
async def ws_room(websocket: WebSocket, room_id: str, user_id: str = Query(...)) -> None:
    """
    WebSocket подключение к комнате.
    Сервер координирует MPC протокол, но не вычисляет результаты ходов.
    """
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

    print(f"✅ User {user.name} connected to room {room.name}")

    # Отправляем текущее состояние комнаты новому подключению
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

    # Уведомляем других о новом игроке
    await broadcast_to_room(room, {
        "type": "player_joined",
        "user_id": user_id,
        "name": user.name
    })

    try:
        async for data in websocket.iter_json():
            msg_type = data.get("type")
            print(f"📩 Message from {user.name}: {msg_type}")

            # ===== ROLL REQUEST =====
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

                # Запускаем MPC раунд для броска
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

                    # Отправляем commit_phase_start всем активным игрокам
                    await broadcast_to_room(room, {
                        "type": "commit_phase_start",
                        "round_id": round_id,
                        "n": game.n,
                        "participants": participants
                    })

            # ===== COMMIT =====
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
                print(f"🔒 Commit from {user.name}: {commit[:16]}...")

                # Проверяем, все ли отправили коммитменты
                if len(room.current_round.commits) == len(room.current_round.participants):
                    room.current_round.phase = "REVEAL"
                    print(f"🔓 All commits received, starting reveal phase")

                    await broadcast_to_room(room, {
                        "type": "reveal_phase_start",
                        "round_id": round_id
                    })

            # ===== REVEAL =====
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

                # Проверяем коммитмент
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
                print(f"🔓 Reveal from {user.name}: a={a}")

                # Проверяем, все ли раскрыли значения
                if len(room.current_round.reveals) == len(room.current_round.participants):
                    # Вычисляем результат
                    result = sum(room.current_round.reveals.values()) % room.current_round.n
                    print(f"🎲 Roll result: {result}")

                    room.current_round.phase = "DONE"

                    # Отправляем результат броска ВСЕМ игрокам
                    await broadcast_to_room(room, {
                        "type": "roll_computed",
                        "round_id": round_id,
                        "result": result
                    })

                    # Очищаем раунд
                    room.current_round = None

            # ===== ROLL SUCCESS =====
            elif msg_type == "roll_success":
                game = room.game
                if not game:
                    await websocket.send_json({"type": "error", "reason": "No active game"})
                    continue

                current = game.current_player()
                if not current or current.user.id != user_id:
                    await websocket.send_json({"type": "error", "reason": "Not your turn"})
                    continue

                print(f"✅ {user.name} successfully rolled and didn't bust")

                # Передаем ход следующему игроку
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
                        print(f"➡️ Next turn: {next_player.user.name}")
                        await broadcast_to_room(room, {
                            "type": "next_turn",
                            "user_id": next_player.user.id,
                            "player_name": next_player.user.name
                        })

            # ===== DECLARE BUSTED =====
            elif msg_type == "declare_busted":
                game = room.game
                if not game:
                    await websocket.send_json({"type": "error", "reason": "No active game"})
                    continue

                current = game.current_player()
                if not current or current.user.id != user_id:
                    await websocket.send_json({"type": "error", "reason": "Not your turn"})
                    continue

                # Помечаем игрока как проигравшего
                game.mark_busted(user_id)
                print(f"💥 {user.name} busted!")

                await broadcast_to_room(room, {
                    "type": "player_busted",
                    "user_id": user_id
                })

                # Передаем ход следующему игроку
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
                        print(f"➡️ Next turn: {next_player.user.name}")
                        await broadcast_to_room(room, {
                            "type": "next_turn",
                            "user_id": next_player.user.id,
                            "player_name": next_player.user.name
                        })

            # ===== PASS =====
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
                print(f"🛑 {user.name} passed")

                await broadcast_to_room(room, {
                    "type": "player_passed",
                    "user_id": user_id
                })

                # Передаем ход следующему игроку
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
                        print(f"➡️ Next turn: {next_player.user.name}")
                        await broadcast_to_room(room, {
                            "type": "next_turn",
                            "user_id": next_player.user.id,
                            "player_name": next_player.user.name
                        })

            # ===== UNKNOWN MESSAGE TYPE =====
            else:
                print(f"⚠️ Unknown message type: {msg_type}")
                await websocket.send_json({
                    "type": "error",
                    "reason": f"unknown_message_type",
                    "detail": f"Message type '{msg_type}' is not recognized"
                })

    except WebSocketDisconnect:
        print(f"👋 User {user.name} disconnected")
    except Exception as e:
        print(f"❌ WebSocket error for {user.name}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        room.websockets.pop(user_id, None)
        await broadcast_to_room(room, {
            "type": "player_left",
            "user_id": user_id
        })
