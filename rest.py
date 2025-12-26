from typing import List

from fastapi import APIRouter

from core import room_service, room_store, user_service
from schemas import (
    GamePublicState,
    JoinRoomRequest,
    RoomCreateRequest,
    RoomResponse,
    StartGameRequest,
    UserCreateRequest,
    UserResponse,
)

router = APIRouter()


@router.post("/users", response_model=UserResponse)
def register_user(payload: UserCreateRequest) -> UserResponse:
    user = user_service.register(name=payload.name)
    return UserResponse(id=user.id, name=user.name)


@router.post("/rooms", response_model=RoomResponse)
def create_room(payload: RoomCreateRequest) -> RoomResponse:
    room = room_service.create_room(name=payload.name)
    return RoomResponse(
        id=room.id,
        name=room.name,
        user_count=len(room.users),
        is_active=room.is_active,
    )


@router.get("/rooms", response_model=List[RoomResponse])
def list_rooms() -> List[RoomResponse]:
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


@router.post("/rooms/{room_id}/join", response_model=RoomResponse)
def join_room(room_id: str, payload: JoinRoomRequest) -> RoomResponse:
    room = room_service.join_room(room_id=room_id, user_id=payload.user_id)
    return RoomResponse(
        id=room.id,
        name=room.name,
        user_count=len(room.users),
        is_active=room.is_active,
    )


@router.post("/rooms/{room_id}/start", response_model=GamePublicState)
def start_game(room_id: str, payload: StartGameRequest) -> GamePublicState:
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
