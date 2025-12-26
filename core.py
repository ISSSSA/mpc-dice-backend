from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional
from uuid import uuid4

from fastapi import HTTPException, WebSocket



@dataclass
class RollRound:
    id: str
    n: int
    participants: List[str]
    commits: Dict[str, str] = field(default_factory=dict)
    reveals: Dict[str, int] = field(default_factory=dict)
    phase: str = "COMMIT"
    salts: Dict[str, str] = field(default_factory=dict)


@dataclass
class RoundHistory:
    round_id: str
    player_id: str
    n: int
    commits: Dict[str, str]
    reveals: Dict[str, int]
    salts: Dict[str, str]
    result: int


class PlayerGameStatus(Enum):
    ACTIVE = auto()
    PASSED = auto()
    BUSTED = auto()


@dataclass
class User:
    id: str
    name: str


@dataclass
class PlayerInGame:
    user: User
    status: PlayerGameStatus = PlayerGameStatus.ACTIVE


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
    current_turn_index: int = 0

    def current_player(self) -> Optional[PlayerInGame]:
        if not self.players or self.current_turn_index >= len(self.players):
            return None
        return self.players[self.current_turn_index]

    def advance_turn(self) -> None:
        if not self.players:
            return

        for _ in range(len(self.players)):
            self.current_turn_index = (self.current_turn_index + 1) % len(self.players)
            current = self.current_player()
            if current and current.status == PlayerGameStatus.ACTIVE:
                return

        self.phase = GamePhase.FINISHED

    def mark_pass(self, user_id: str) -> None:
        for p in self.players:
            if p.user.id == user_id:
                p.status = PlayerGameStatus.PASSED
                return

    def mark_busted(self, user_id: str) -> None:
        for p in self.players:
            if p.user.id == user_id:
                p.status = PlayerGameStatus.BUSTED
                return

    def is_game_over(self) -> bool:
        active_count = sum(1 for p in self.players if p.status == PlayerGameStatus.ACTIVE)
        return active_count <= 1

    def get_winner(self) -> Optional[str]:
        active = [p for p in self.players if p.status == PlayerGameStatus.ACTIVE]
        if len(active) == 1:
            return active[0].user.id

        passed = [p for p in self.players if p.status == PlayerGameStatus.PASSED]
        if passed:
            return passed[-1].user.id

        return None


@dataclass
class Room:
    id: str
    name: str
    users: Dict[str, User] = field(default_factory=dict)
    game: Optional[GameState] = None
    is_active: bool = False
    websockets: Dict[str, WebSocket] = field(default_factory=dict)
    rounds_history: List[RoundHistory] = field(default_factory=list)
    current_round: Optional[RollRound] = None
    round_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

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

user_store = InMemoryUserStore()
room_store = InMemoryRoomStore()
user_service = UserService(user_store=user_store)
room_service = RoomService(room_store=room_store, user_store=user_store)
