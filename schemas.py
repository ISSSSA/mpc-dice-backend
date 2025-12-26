from typing import List, Optional

from pydantic import BaseModel, Field


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
