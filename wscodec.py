"""Minimal WebSocket codec (RFC 6455 subset) shared by pool and edge.

Supports binary frames both directions, server-side handshake accept,
client-side masked frames. No extensions, no fragmentation output.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os

WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def accept_key(key: str) -> str:
    return base64.b64encode(
        hashlib.sha1((key.strip() + WS_MAGIC).encode()).digest()
    ).decode()


def server_handshake_response(key: str) -> bytes:
    return (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: " + accept_key(key).encode() + b"\r\n\r\n"
    )


def client_handshake_request(host: str, path: str, key: str) -> bytes:
    return (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode("latin1")


def new_key() -> str:
    return base64.b64encode(os.urandom(16)).decode()


def encode_frame(payload: bytes, opcode: int = 0x2, mask: bool = False) -> bytes:
    fin = 0x80 | (opcode & 0x0F)
    header = bytes([fin])
    length = len(payload)
    mask_bit = 0x80 if mask else 0
    if length < 126:
        header += bytes([mask_bit | length])
    elif length < 65536:
        header += bytes([mask_bit | 126]) + length.to_bytes(2, "big")
    else:
        header += bytes([mask_bit | 127]) + length.to_bytes(8, "big")
    if mask:
        mask_key = os.urandom(4)
        header += mask_key
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return header + payload


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes] | None:
    try:
        hdr = await reader.readexactly(2)
    except (asyncio.IncompleteReadError, ConnectionError):
        return None
    opcode = hdr[0] & 0x0F
    masked = bool(hdr[1] & 0x80)
    length = hdr[1] & 0x7F
    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), "big")
    elif length == 127:
        length = int.from_bytes(await reader.readexactly(8), "big")
    mask_key = await reader.readexactly(4) if masked else b""
    payload = await reader.readexactly(length) if length else b""
    if masked:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    if opcode == 0x8:
        return None
    return opcode, payload
