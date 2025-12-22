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
import hashlib, uuid, asyncio

@dataclass
class RollRound:
    id: str
    n: int
    participants: List[str]
    commits: Dict[str, str] = field(default_factory=dict)   # user_id -> c
    reveals: Dict[str, int] = field(default_factory=dict)   # user_id -> a
    phase: str = "COMMIT"                                   # COMMIT/REVEAL/DONE

# ====== Доменные модели ======

class PlayerGameStatus(Enum):
    ACTIVE = auto()
    PASSED = auto()
    BUSTED = auto()


@dataclass
class User:
    id: str
    name: str

@dataclass
class Game:
    id: str
    n: int
    m: int
    phase: str  # например "IN_PROGRESS"
    # + players/current_player_id и т.д.

@dataclass
class PlayerInGame:
    user: User
    status: PlayerGameStatus = PlayerGameStatus.ACTIVE
    # приватный счётчик x_i игрок ведёт у себя на клиенте


class GamePhase(Enum):
    WAITING_FOR_PLAYERS = auto()
    IN_PROGRESS = auto()
    FINISHED = auto()


@dataclass
class GameState:
    id: str
    n: int
    m: int
    players: List[PlayerInGame] = field(default_factory=list)
    phase: GamePhase = GamePhase.WAITING_FOR_PLAYERS
    current_turn_index: int = 0  # индекс в списке players

    def current_player(self) -> Optional[PlayerInGame]:
        if not self.players:
            return None
        if self.current_turn_index < 0 or self.current_turn_index >= len(self.players):
            return None
        return self.players[self.current_turn_index]

    def advance_turn(self) -> None:
        if not self.players:
            return
        self.current_turn_index = (self.current_turn_index + 1) % len(self.players)

    def mark_pass(self, user_id: str) -> None:
        for p in self.players:
            if p.user.id == user_id:
                p.status = PlayerGameStatus.PASSED

    def mark_busted(self, user_id: str) -> None:
        for p in self.players:
            if p.user.id == user_id:
                p.status = PlayerGameStatus.BUSTED

    def is_everyone_done(self) -> bool:
        return all(p.status != PlayerGameStatus.ACTIVE for p in self.players)


@dataclass
class Room:
    id: str
    name: str

    # Пользователи в комнате (то, что ты уже используешь)
    users: Dict[str, User] = field(default_factory=dict)

    # Текущая игра (или None, если игра ещё не создана)
    game: Optional[Game] = None
    is_active: bool = False

    # WebSocket соединения: user_id -> WebSocket
    websockets: Dict[str, WebSocket] = field(default_factory=dict)

    # --- MPC state ---
    current_round: Optional[RollRound] = None

    # Lock для защиты current_round/commits/reveals от гонок
    round_lock: asyncio.Lock = field(default_factory=asyncio.Lock)



# ====== Stores (как раньше, только Room расширен) ======

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
        if user is None:
            return
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

        if not room.users:
            raise HTTPException(status_code=400, detail="Cannot start game without players")

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

app = FastAPI(title="MPC Dice Game Backend (ephemeral users)")

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


@app.post("/users", response_model=UserResponse)
def register_user(payload: UserCreateRequest) -> UserResponse:
    user = user_service.register(name=payload.name)
    return UserResponse(id=user.id, name=user.name)


@app.post("/rooms", response_model=RoomResponse)
def create_room(payload: RoomCreateRequest) -> RoomResponse:
    room = room_service.create_room(name=payload.name)

    return RoomResponse(
        id=room.id,
        name=room.name,
        user_count=len(room.users),
        is_active=room.is_active,
    )

@app.get("/rooms", response_model=list[RoomResponse])
def list_rooms() -> list[RoomResponse]:
    """
    Вернуть список всех комнат.
    Используется фронтендом для отображения лобби.
    """
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
    room = room_service.join_room(room_id=room_id, user_id=payload.user_id)
    return RoomResponse(
        id=room.id,
        name=room.name,
        user_count=len(room.users),
        is_active=room.is_active,
    )


@app.post("/rooms/{room_id}/start", response_model=GamePublicState)
def start_game(room_id: str, payload: StartGameRequest) -> GamePublicState:
    game = room_service.start_game(room_id=room_id, n=payload.n, m=payload.m)
    current = game.current_player()
    return GamePublicState(
        id=game.id,
        phase=game.phase.name,
        players=[p.user.id for p in game.players],
        current_player_id=current.user.id if current else None,
    )

async def broadcast_to_room(room: Room, message: dict) -> None:
    text = json.dumps(message)
    dead: list[str] = []
    for uid, ws in room.websockets.items():
        try:
            await ws.send_text(text)
        except Exception:
            dead.append(uid)
    for uid in dead:
        room.websockets.pop(uid, None)



@app.websocket("/ws/rooms/{room_id}")
async def ws_room(websocket: WebSocket, room_id: str, user_id: str = Query(...)) -> None:
    """
    Подключение к комнате по WebSocket.
    user_id — это id, полученный при /users (регистрация по имени).
    """
    room = room_store.get_room(room_id)
    if room is None:
        await websocket.close(code=4404)
        return

    user = user_store.get_user(user_id)
    if user is None:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    await websocket.send_text(json.dumps({
        "type": "room_state",
        "room": {
            "id": room.id,
            "name": room.name,
            "users": [{"id": u.id, "name": u.name} for u in room.users.values()],
            "is_active": room.is_active,
        },
        "game": None if room.game is None else {
            "id": room.game.id,
            "phase": room.game.phase.name,
            "players": [p.user.id for p in room.game.players],
            "current_player_id": room.game.current_player().user.id if room.game.current_player() else None,
            "n": room.game.n,
            "m": room.game.m,
        }
    }))

    room.websockets[user_id] = websocket

    # уведомляем остальных
    await broadcast_to_room(room, {
        "type": "player_joined",
        "user_id": user_id,
        "name": user.name,
    })

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({"type": "error", "reason": "invalid_json"}))
                continue

            msg_type = msg.get("type")

            # запрос на "roll" (дальше клиент уже делает MPC: commit/reveal по WS или отдельно)
            if msg_type == "roll_request":
                await handle_roll_request(room=room, user_id=user_id)

            elif msg_type == "commit":
                await handle_commit(room=room, user_id=user_id, round_id=msg.get("round_id"), c=msg.get("c"))

            elif msg_type == "reveal":
                await handle_reveal(room=room, user_id=user_id, round_id=msg.get("round_id"),
                                    a=msg.get("a"), salt=msg.get("salt"))

            elif msg_type == "roll_result":
                busted = bool(msg.get("busted", False))
                await handle_roll_result(room=room, user_id=user_id, busted=busted)

            elif msg_type == "pass":
                await handle_pass(room=room, user_id=user_id)

            else:
                await websocket.send_text(json.dumps({"type": "error", "reason": "unknown_type"}))

    except WebSocketDisconnect:
        room.websockets.pop(user_id, None)
        await broadcast_to_room(room, {
            "type": "player_left",
            "user_id": user_id,
        })

async def handle_roll_request(room: Room, user_id: str) -> None:
    game = room.game
    if game is None or game.phase != GamePhase.IN_PROGRESS:
        return

    current = game.current_player()
    if current is None or current.user.id != user_id:
        # не его ход
        ws = room.websockets.get(user_id)
        if ws:
            await ws.send_text(json.dumps({"type": "error", "reason": "not_your_turn"}))
        return

    # уведомляем всех, что игрок начинает ход (клиенты сами запустят MPC commit/reveal)
    await broadcast_to_room(room, {
        "type": "turn_started",
        "user_id": user_id,
        "n": game.n,
        "m": game.m,
    })

    # дальше фронтенд делает MPC и ПОСЛЕ этого шлёт серверу результат:
    # например, msg type="roll_result" с флагом busted.
    # Здесь ставим заглушку.


async def handle_pass(room: Room, user_id: str) -> None:
    game = room.game
    if game is None or game.phase != GamePhase.IN_PROGRESS:
        return

    game.mark_pass(user_id=user_id)
    await broadcast_to_room(room, {
        "type": "player_passed",
        "user_id": user_id,
    })

    if game.is_everyone_done():
        game.phase = GamePhase.FINISHED
        room.is_active = False
        await broadcast_to_room(room, {
            "type": "game_finished",
            # кто победил — решается отдельно, т.к. счётчики приватные
        })
    else:
        game.advance_turn()
        current = game.current_player()
        await broadcast_to_room(room, {
            "type": "next_turn",
            "user_id": current.user.id if current else None,
        })

async def handle_roll_request(room: Room, user_id: str) -> None:
    game = room.game
    if game is None or game.phase != GamePhase.IN_PROGRESS:
        return

    current = game.current_player()
    if current is None or current.user.id != user_id:
        ws = room.websockets.get(user_id)
        if ws:
            await ws.send_text(json.dumps({"type": "error", "reason": "not_your_turn"}))
        return

    participants = [p.user.id for p in game.players]  # можно сузить до "активных", если есть статусы

    async with room.round_lock:
        if getattr(room, "current_round", None) and room.current_round.phase != "DONE":
            return  # уже идёт раунд
        room.current_round = RollRound(
            id=str(uuid.uuid4()),
            n=game.n,
            participants=participants,
        )

    await broadcast_to_room(room, {
        "type": "commit_phase_start",
        "round_id": room.current_round.id,
        "n": room.current_round.n,
        "participants": participants,
        "turn_user_id": user_id,
    })


async def handle_commit(room: Room, user_id: str, round_id: str, c: str) -> None:
    rr: RollRound = getattr(room, "current_round", None)
    if not rr or rr.id != round_id or rr.phase != "COMMIT":
        return
    if user_id not in rr.participants or not c:
        return

    rr.commits[user_id] = c

    if len(rr.commits) == len(rr.participants):
        rr.phase = "REVEAL"
        await broadcast_to_room(room, {
            "type": "reveal_phase_start",
            "round_id": rr.id,
        })


async def handle_reveal(room: Room, user_id: str, round_id: str, a, salt: str) -> None:
    rr: RollRound = getattr(room, "current_round", None)
    if not rr or rr.id != round_id or rr.phase != "REVEAL":
        return
    if user_id not in rr.participants or salt is None:
        return

    try:
        a_int = int(a)
    except Exception:
        return

    # проверка диапазона a_j ∈ {0..n-1} [file:417]
    if a_int < 0 or a_int >= rr.n:
        return

    # проверка коммитмента c_j = H(a_j || salt_j) [file:417]
    expected = hashlib.sha256(f"{a_int}|{salt}".encode("utf-8")).hexdigest()
    if rr.commits.get(user_id) != expected:
        # читерство / рассинхрон: можно завершить игру или пометить игрока
        ws = room.websockets.get(user_id)
        if ws:
            await ws.send_text(json.dumps({"type": "error", "reason": "commit_mismatch"}))
        return

    rr.reveals[user_id] = a_int

    if len(rr.reveals) == len(rr.participants):
        r = sum(rr.reveals.values()) % rr.n  # r = Σ a_j mod n [file:417]
        rr.phase = "DONE"

        await broadcast_to_room(room, {
            "type": "roll_computed",
            "round_id": rr.id,
            "r": r,
            "contributions": [{"user_id": uid, "a": rr.reveals[uid]} for uid in rr.participants],
        })

        # можно очистить, чтобы следующий ход мог стартовать новый раунд
        room.current_round = None

