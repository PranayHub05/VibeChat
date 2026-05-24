"""
VibeChat — Real-time chat application backend.

A FastAPI server providing:
  • In-memory room management with auto-cleanup
  • REST endpoints for room creation and validation
  • WebSocket endpoint for real-time messaging
  • Static file serving for the frontend
"""

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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_CACHED_MESSAGES = 50
ROOM_CODE_BYTES = 3  # 3 hex bytes → 6 hex characters
STATIC_DIR = Path(__file__).resolve().parent / "static"

# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


@dataclass
class Room:
    """Represents a single chat room."""

    code: str
    connections: dict[WebSocket, str] = field(default_factory=dict)
    messages: list[dict] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # -- helpers -------------------------------------------------------------

    @property
    def usernames(self) -> list[str]:
        """Return a sorted list of connected usernames."""
        return sorted(set(self.connections.values()))

    @property
    def user_count(self) -> int:
        return len(self.connections)

    def cache_message(self, message: dict) -> None:
        """Append a message to the history, keeping only the last N entries."""
        self.messages.append(message)
        if len(self.messages) > MAX_CACHED_MESSAGES:
            self.messages = self.messages[-MAX_CACHED_MESSAGES:]


class RoomManager:
    """Manages all active chat rooms in memory."""

    def __init__(self) -> None:
        self.rooms: dict[str, Room] = {}

    # -- room lifecycle ------------------------------------------------------

    def create_room(self) -> Room:
        """Create a new room with a unique 6-character uppercase code."""
        code = self._generate_unique_code()
        room = Room(code=code)
        self.rooms[code] = room
        return room

    def get_room(self, code: str) -> Room | None:
        """Return the room for *code*, or ``None`` if it doesn't exist."""
        return self.rooms.get(code.upper())

    def delete_room(self, code: str) -> None:
        """Remove a room from the manager."""
        self.rooms.pop(code, None)

    # -- connection management -----------------------------------------------

    def add_connection(self, room: Room, ws: WebSocket, username: str) -> None:
        """Register a WebSocket connection in the given room."""
        room.connections[ws] = username

    def remove_connection(self, room: Room, ws: WebSocket) -> str | None:
        """Remove a WebSocket from the room and return the username."""
        return room.connections.pop(ws, None)

    def cleanup_if_empty(self, room: Room) -> bool:
        """Delete the room if no connections remain. Returns ``True`` if deleted."""
        if not room.connections:
            self.delete_room(room.code)
            return True
        return False

    # -- broadcasting --------------------------------------------------------

    async def broadcast(self, room: Room, message: dict) -> None:
        """Send a JSON message to every connection in the room."""
        payload = json.dumps(message)
        stale: list[WebSocket] = []
        for ws in list(room.connections):
            try:
                await ws.send_text(payload)
            except Exception:
                stale.append(ws)
        # Remove any broken connections discovered during broadcast
        for ws in stale:
            room.connections.pop(ws, None)

    async def send_personal(self, ws: WebSocket, message: dict) -> None:
        """Send a JSON message to a single WebSocket connection."""
        await ws.send_text(json.dumps(message))

    # -- internal ------------------------------------------------------------

    def _generate_unique_code(self) -> str:
        """Generate a 6-char uppercase alphanumeric code, retrying on collision."""
        for _ in range(100):
            code = secrets.token_hex(ROOM_CODE_BYTES).upper()
            if code not in self.rooms:
                return code
        raise RuntimeError("Unable to generate a unique room code after 100 attempts")


# ---------------------------------------------------------------------------
# Pydantic request / response schemas
# ---------------------------------------------------------------------------


class CreateRoomRequest(BaseModel):
    username: str


class CreateRoomResponse(BaseModel):
    code: str


class RoomStatusResponse(BaseModel):
    exists: bool
    users: int = 0


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
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


# ---------------------------------------------------------------------------
# Application setup
# ---------------------------------------------------------------------------

app = FastAPI(title="VibeChat", version="1.0.0")

# CORS — allow all origins for development convenience
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

manager = RoomManager()

# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


@app.post("/api/rooms", response_model=CreateRoomResponse, status_code=201)
async def create_room(body: CreateRoomRequest) -> CreateRoomResponse:
    """Create a new chat room and return its unique code."""
    if not body.username or not body.username.strip():
        raise HTTPException(status_code=400, detail="Username must not be empty.")
    room = manager.create_room()
    return CreateRoomResponse(code=room.code)


@app.get("/api/rooms/{code}", response_model=RoomStatusResponse)
async def get_room_status(code: str) -> RoomStatusResponse:
    """Check whether a room exists and how many users are connected."""
    room = manager.get_room(code)
    if room is None:
        return RoomStatusResponse(exists=False, users=0)
    return RoomStatusResponse(exists=True, users=room.user_count)


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


@app.websocket("/ws/{room_code}")
async def websocket_endpoint(websocket: WebSocket, room_code: str, username: str = "Anonymous"):
    """Handle a WebSocket connection for real-time chat in a room."""
    room_code = room_code.upper()
    room = manager.get_room(room_code)

    if room is None:
        await websocket.close(code=4004, reason="Room not found")
        return

    await websocket.accept()

    # Register the connection
    manager.add_connection(room, websocket, username)

    try:
        # 1. Broadcast "user joined" system message
        join_msg = _system_message(f"{username} joined the room")
        room.cache_message(join_msg)
        await manager.broadcast(room, join_msg)

        # 2. Send cached message history to the newcomer
        for msg in room.messages:
            await manager.send_personal(websocket, msg)

        # 3. Broadcast updated user list
        await manager.broadcast(room, _user_list_message(room.usernames))

        # 4. Listen for incoming messages
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
        # Catch-all so a single misbehaving client doesn't crash the server
        pass
    finally:
        # Clean up regardless of how the connection ended
        manager.remove_connection(room, websocket)

        # Broadcast departure & updated user list (only if room still has users)
        if room.connections:
            leave_msg = _system_message(f"{username} left the room")
            room.cache_message(leave_msg)
            await manager.broadcast(room, leave_msg)
            await manager.broadcast(room, _user_list_message(room.usernames))

        # Auto-delete the room when the last user leaves
        manager.cleanup_if_empty(room)


# ---------------------------------------------------------------------------
# Static files — serve the frontend
# ---------------------------------------------------------------------------
# Mounted AFTER all API/WS routes so they take priority.
# html=True serves index.html for "/" and allows style.css, app.js at root.

if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
