from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

MAX_CACHED_MESSAGES = 50
ROOM_CODE_BYTES = 3
STATIC_DIR = Path(__file__).resolve().parent / "static"


@dataclass
class Room:
    code: str
    connections: dict[WebSocket, str] = field(default_factory=dict)
    messages: list[dict] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def usernames(self) -> list[str]:
        return sorted(set(self.connections.values()))

    @property
    def user_count(self) -> int:
        return len(self.connections)

    def cache_message(self, message: dict) -> None:
        self.messages.append(message)
        if len(self.messages) > MAX_CACHED_MESSAGES:
            self.messages = self.messages[-MAX_CACHED_MESSAGES:]


class RoomManager:
    def __init__(self) -> None:
        self.rooms: dict[str, Room] = {}

    def create_room(self) -> Room:
        code = self._generate_unique_code()
        room = Room(code=code)
        self.rooms[code] = room
        return room

    def get_room(self, code: str) -> Room | None:
        return self.rooms.get(code.upper())

    def delete_room(self, code: str) -> None:
        self.rooms.pop(code, None)

    def add_connection(self, room: Room, ws: WebSocket, username: str) -> None:
        room.connections[ws] = username

    def remove_connection(self, room: Room, ws: WebSocket) -> str | None:
        return room.connections.pop(ws, None)

    def cleanup_if_empty(self, room: Room) -> bool:
        if not room.connections:
            self.delete_room(room.code)
            return True
        return False

    async def broadcast(self, room: Room, message: dict) -> None:
        payload = json.dumps(message)
        stale: list[WebSocket] = []
        for ws in list(room.connections):
            try:
                await ws.send_text(payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            room.connections.pop(ws, None)

    async def send_personal(self, ws: WebSocket, message: dict) -> None:
        await ws.send_text(json.dumps(message))

    def _generate_unique_code(self) -> str:
        for _ in range(100):
            code = secrets.token_hex(ROOM_CODE_BYTES).upper()
            if code not in self.rooms:
                return code
        raise RuntimeError("Unable to generate a unique room code after 100 attempts")


class CreateRoomRequest(BaseModel):
    username: str


class CreateRoomResponse(BaseModel):
    code: str


class RoomStatusResponse(BaseModel):
    exists: bool
    users: int = 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _system_message(content: str) -> dict:
    return {"type": "system", "content": content, "timestamp": _now_iso()}


def _chat_message(username: str, content: str) -> dict:
    return {
        "type": "chat",
        "username": username,
        "content": content,
        "timestamp": _now_iso(),
    }


def _user_list_message(users: list[str]) -> dict:
    return {"type": "user_list", "users": users, "timestamp": _now_iso()}


app = FastAPI(title="VibeChat", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

manager = RoomManager()


@app.post("/api/rooms", response_model=CreateRoomResponse, status_code=201)
async def create_room(body: CreateRoomRequest) -> CreateRoomResponse:
    if not body.username or not body.username.strip():
        raise HTTPException(status_code=400, detail="Username must not be empty.")
    room = manager.create_room()
    return CreateRoomResponse(code=room.code)


@app.get("/api/rooms/{code}", response_model=RoomStatusResponse)
async def get_room_status(code: str) -> RoomStatusResponse:
    room = manager.get_room(code)
    if room is None:
        return RoomStatusResponse(exists=False, users=0)
    return RoomStatusResponse(exists=True, users=room.user_count)


@app.websocket("/ws/{room_code}")
async def websocket_endpoint(websocket: WebSocket, room_code: str, username: str = "Anonymous"):
    room_code = room_code.upper()
    room = manager.get_room(room_code)

    if room is None:
        await websocket.close(code=4004, reason="Room not found")
        return

    await websocket.accept()

    manager.add_connection(room, websocket, username)

    try:
        join_msg = _system_message(f"{username} joined the room")
        room.cache_message(join_msg)
        await manager.broadcast(room, join_msg)

        for msg in room.messages:
            await manager.send_personal(websocket, msg)

        await manager.broadcast(room, _user_list_message(room.usernames))

        while True:
            data = await websocket.receive_text()
            try:
                payload = json.loads(data)
                content = payload.get("content", "")
            except (json.JSONDecodeError, AttributeError):
                content = data

            if not content:
                continue

            chat_msg = _chat_message(username, content)
            room.cache_message(chat_msg)
            await manager.broadcast(room, chat_msg)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        manager.remove_connection(room, websocket)

        if room.connections:
            leave_msg = _system_message(f"{username} left the room")
            room.cache_message(leave_msg)
            await manager.broadcast(room, leave_msg)
            await manager.broadcast(room, _user_list_message(room.usernames))

        manager.cleanup_if_empty(room)


if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
