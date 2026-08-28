#!/usr/bin/env python3
"""PIXELPORT — agent-first pixel collaboration server.

No third-party dependencies.  The server hosts the frontend and exposes a small,
versioned JSON HTTP API for autonomous drawing agents.  Projects are stored in
SQLite with sparse pixel maps, so an empty 1920×1080 canvas remains lightweight.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import struct
import sys
import threading
import zlib
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("PIXELPORT_DATA", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.environ.get("PIXELPORT_DB", DATA_DIR / "pixelport.sqlite3"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "4173"))
MAX_BODY = 8 * 1024 * 1024
MAX_DIMENSION = 1_000_000
MAX_CELLS = 16_000_000
MAX_EXPORT_CELLS = 4_000_000
MAX_FRAMES = 96
MAX_LAYERS = 32
MAX_OPERATIONS = 50_000
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
DB_LOCK = threading.RLock()

DEFAULT_PALETTE = [
    "#081020", "#172a49", "#2c5178", "#4c9fe4", "#7be9eb", "#e8f4ff",
    "#ffd55d", "#ff9668", "#fc7193", "#a685f7", "#7ad58e", "#63779c",
]
DEFAULT_LAYERS = [
    {"name": "Контур + цвет", "visible": True, "opacity": 1.0},
    {"name": "Свет", "visible": True, "opacity": 1.0},
    {"name": "Эффекты", "visible": True, "opacity": 1.0},
]


class APIError(Exception):
    def __init__(self, status: int, message: str, *, code: str = "bad_request", details: Any = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.details = details


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def random_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(9).replace('-', 'a').replace('_', 'b')}"


def clean_string(value: Any, label: str, limit: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise APIError(422, f"{label}: ожидается строка.", code="validation_error")
    result = value.strip()
    if not result and not allow_empty:
        raise APIError(422, f"{label}: значение не может быть пустым.", code="validation_error")
    if len(result) > limit:
        raise APIError(422, f"{label}: максимум {limit} символов.", code="validation_error")
    return result


def positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise APIError(422, f"{label}: ожидается положительное целое число.", code="validation_error")
    try:
        number = int(value)
    except (ValueError, TypeError):
        raise APIError(422, f"{label}: ожидается положительное целое число.", code="validation_error") from None
    if number < 1:
        raise APIError(422, f"{label}: значение должно быть больше нуля.", code="validation_error")
    if number > MAX_DIMENSION:
        raise APIError(422, f"{label}: максимум одного измерения — {MAX_DIMENSION:,} px.", code="validation_error")
    return number


def validate_dimensions(width_value: Any, height_value: Any) -> tuple[int, int]:
    width = positive_int(width_value, "width")
    height = positive_int(height_value, "height")
    if width * height > MAX_CELLS:
        raise APIError(
            422,
            f"Сетка {width}×{height} слишком велика для одного проекта ({MAX_CELLS:,} пикселей максимум).",
            code="canvas_too_large",
        )
    return width, height


def normalize_hex(value: Any, label: str = "color", *, allow_null: bool = False) -> str | None:
    if value is None and allow_null:
        return None
    if not isinstance(value, str) or not HEX.match(value):
        raise APIError(422, f"{label}: используй цвет формата #RRGGBB или null.", code="validation_error")
    return value.lower()


def clamp_opacity(value: Any) -> float:
    try:
        opacity = float(value)
    except (TypeError, ValueError):
        return 1.0
    return max(0.0, min(1.0, opacity))


def bounded_index(value: Any, count: int, default: int = 0) -> int:
    try:
        index = int(value)
    except (TypeError, ValueError):
        index = default
    return max(0, min(max(0, count - 1), index))


def sparse_layer(raw: Any, cells: int) -> dict[str, str]:
    """Read either a legacy array or a sparse object into a compact object."""
    output: dict[str, str] = {}
    if isinstance(raw, list):
        iterator: Iterable[tuple[Any, Any]] = enumerate(raw)
    elif isinstance(raw, dict):
        iterator = raw.items()
    else:
        return output
    for raw_index, raw_color in iterator:
        if raw_color is None:
            continue
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            continue
        if index < 0 or index >= cells:
            continue
        if isinstance(raw_color, str) and HEX.match(raw_color):
            output[str(index)] = raw_color.lower()
    return output


def normalize_project(raw: Any, *, fallback_name: str = "Новая агентная сессия") -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    raw_width = raw.get("width", raw.get("size", 32))
    raw_height = raw.get("height", raw.get("size", 32))
    width, height = validate_dimensions(raw_width, raw_height)
    cells = width * height
    name = raw.get("projectName", raw.get("name", fallback_name))
    if not isinstance(name, str) or not name.strip():
        name = fallback_name
    name = name.strip()[:80]

    palette_input = raw.get("palette")
    if not isinstance(palette_input, list):
        palette_input = DEFAULT_PALETTE
    palette: list[str] = []
    for color in palette_input:
        if isinstance(color, str) and HEX.match(color) and color.lower() not in palette:
            palette.append(color.lower())
        if len(palette) >= 255:
            break
    if not palette:
        palette = list(DEFAULT_PALETTE)

    layers_input = raw.get("layers")
    if not isinstance(layers_input, list) or not layers_input:
        layers_input = DEFAULT_LAYERS
    layers: list[dict[str, Any]] = []
    seen_layer_ids: set[str] = set()
    for index, source in enumerate(layers_input[:MAX_LAYERS]):
        if not isinstance(source, dict):
            source = {}
        layer_id = str(source.get("id") or random_id("layer"))[:80]
        if not SAFE_ID.match(layer_id) or layer_id in seen_layer_ids:
            layer_id = random_id("layer")
        seen_layer_ids.add(layer_id)
        layer_name = source.get("name", f"Слой {index + 1}")
        if not isinstance(layer_name, str) or not layer_name.strip():
            layer_name = f"Слой {index + 1}"
        layers.append({
            "id": layer_id,
            "name": layer_name.strip()[:48],
            "visible": source.get("visible", True) is not False,
            "opacity": clamp_opacity(source.get("opacity", 1)),
        })
    if not layers:
        layers = [{"id": random_id("layer"), **DEFAULT_LAYERS[0]}]

    frames_input = raw.get("frames")
    if not isinstance(frames_input, list) or not frames_input:
        frames_input = [{"name": "Кадр 01", "layers": []}]
    frames: list[dict[str, Any]] = []
    seen_frame_ids: set[str] = set()
    for frame_index, source in enumerate(frames_input[:MAX_FRAMES]):
        if not isinstance(source, dict):
            source = {}
        frame_id = str(source.get("id") or random_id("frame"))[:80]
        if not SAFE_ID.match(frame_id) or frame_id in seen_frame_ids:
            frame_id = random_id("frame")
        seen_frame_ids.add(frame_id)
        frame_name = source.get("name", f"Кадр {frame_index + 1:02d}")
        if not isinstance(frame_name, str) or not frame_name.strip():
            frame_name = f"Кадр {frame_index + 1:02d}"
        source_layers = source.get("layers") if isinstance(source.get("layers"), list) else []
        frames.append({
            "id": frame_id,
            "name": frame_name.strip()[:48],
            "layers": [sparse_layer(source_layers[layer_index] if layer_index < len(source_layers) else {}, cells) for layer_index in range(len(layers))],
        })

    fps_value = raw.get("fps", 8)
    try:
        fps = max(1, min(60, int(fps_value)))
    except (ValueError, TypeError):
        fps = 8
    selected_color = raw.get("selectedColor", palette[0])
    selected_color = selected_color.lower() if isinstance(selected_color, str) and HEX.match(selected_color) else palette[0]

    jobs_input = raw.get("jobs", raw.get("agentTasks", []))
    jobs: list[dict[str, Any]] = []
    if isinstance(jobs_input, list):
        for item in jobs_input[:100]:
            if not isinstance(item, dict):
                continue
            description = item.get("description", item.get("text", ""))
            if not isinstance(description, str) or not description.strip():
                continue
            jobs.append({
                "id": str(item.get("id") or random_id("job"))[:80],
                "description": description.strip()[:600],
                "target": "all" if item.get("target") == "all" else "active",
                "frame": bounded_index(item.get("frame", 0), len(frames)),
                "layer": bounded_index(item.get("layer", 0), len(layers)), 
                "status": item.get("status") if item.get("status") in {"queued", "claimed", "done", "cancelled"} else "queued",
                "agent_id": str(item.get("agent_id", item.get("agentId", "")))[:80] or None,
                "created_at": str(item.get("created_at", utc_now()))[:40],
                "completed_at": str(item.get("completed_at", ""))[:40] or None,
            })

    return {
        "format": "pixelport-agent-project",
        "version": 3,
        "projectName": name,
        "width": width,
        "height": height,
        "palette": palette,
        "selectedColor": selected_color,
        "brushSize": 1,
        "zoom": 1,
        "fps": fps,
        "activeFrame": bounded_index(raw.get("activeFrame", 0), len(frames)),
        "activeLayer": bounded_index(raw.get("activeLayer", 0), len(layers)), 
        "layers": layers,
        "frames": frames,
        "jobs": jobs,
    }


def db_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def initialize_db() -> None:
    with DB_LOCK, db_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                data_json TEXT NOT NULL,
                owner_key TEXT NOT NULL,
                public_read INTEGER NOT NULL DEFAULT 1,
                revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                role TEXT NOT NULL,
                api_key TEXT NOT NULL,
                allowed_layers_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_seen_at TEXT,
                UNIQUE(project_id, name)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                revision INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_project_id ON events(project_id, id);
            CREATE INDEX IF NOT EXISTS agents_project_id ON agents(project_id);
            """
        )


def load_project(connection: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not row:
        raise APIError(404, "Проект не найден.", code="not_found")
    return row


def parse_data(row: sqlite3.Row) -> dict[str, Any]:
    try:
        return json.loads(row["data_json"])
    except json.JSONDecodeError as error:
        raise APIError(500, "Проект повреждён в хранилище.", code="storage_error") from error


def project_summary(row: sqlite3.Row, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "revision": row["revision"],
        "public_read": bool(row["public_read"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "width": data["width"],
        "height": data["height"],
        "frames": len(data["frames"]),
        "layers": len(data["layers"]),
        "jobs": len([job for job in data.get("jobs", []) if job.get("status") in {"queued", "claimed"}]),
    }


def event(connection: sqlite3.Connection, project_id: str, revision: int, event_type: str, actor: str, payload: dict[str, Any]) -> int:
    cursor = connection.execute(
        "INSERT INTO events(project_id, revision, event_type, actor, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (project_id, revision, event_type, actor, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), utc_now()),
    )
    return int(cursor.lastrowid)


def save_project(connection: sqlite3.Connection, row: sqlite3.Row, data: dict[str, Any], *, event_type: str, actor: str, payload: dict[str, Any]) -> tuple[int, int]:
    new_revision = int(row["revision"]) + 1
    now = utc_now()
    connection.execute(
        "UPDATE projects SET name = ?, data_json = ?, revision = ?, updated_at = ? WHERE id = ?",
        (data["projectName"], json.dumps(data, ensure_ascii=False, separators=(",", ":")), new_revision, now, row["id"]),
    )
    event_id = event(connection, row["id"], new_revision, event_type, actor, payload)
    return new_revision, event_id


def get_key(handler: "PixelportHandler") -> str | None:
    header = handler.headers.get("X-Agent-Key")
    if header:
        return header.strip()
    auth = handler.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def authenticate(connection: sqlite3.Connection, row: sqlite3.Row, key: str | None, *, owner_only: bool = False) -> dict[str, Any]:
    if not key:
        raise APIError(401, "Для записи передай X-Agent-Key или Authorization: Bearer <key>.", code="missing_key")
    if hmac.compare_digest(key, row["owner_key"]):
        return {"kind": "owner", "id": "owner", "name": "owner", "role": "owner", "allowed_layers": "*"}
    if owner_only:
        raise APIError(403, "Для этой операции нужен ключ владельца проекта.", code="owner_required")
    agent = connection.execute("SELECT * FROM agents WHERE project_id = ? AND api_key = ?", (row["id"], key)).fetchone()
    if not agent:
        raise APIError(403, "Ключ агента не подходит к этому проекту.", code="invalid_key")
    connection.execute("UPDATE agents SET last_seen_at = ? WHERE id = ?", (utc_now(), agent["id"]))
    try:
        allowed_layers = json.loads(agent["allowed_layers_json"])
    except json.JSONDecodeError:
        allowed_layers = []
    return {"kind": "agent", "id": agent["id"], "name": agent["name"], "role": agent["role"], "allowed_layers": allowed_layers}


def resolve_index(value: Any, collection: list[dict[str, Any]], label: str) -> int:
    if isinstance(value, bool):
        raise APIError(422, f"{label}: укажи индекс или id.", code="validation_error")
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        index = int(value)
        if 0 <= index < len(collection):
            return index
    if isinstance(value, str):
        for index, item in enumerate(collection):
            if item.get("id") == value:
                return index
    raise APIError(422, f"{label}: такой кадр или слой не найден.", code="validation_error")


def check_revision(row: sqlite3.Row, body: dict[str, Any]) -> None:
    desired = body.get("if_revision")
    if desired is None:
        return
    try:
        desired_int = int(desired)
    except (ValueError, TypeError):
        raise APIError(422, "if_revision должен быть числом.", code="validation_error") from None
    if desired_int != int(row["revision"]):
        raise APIError(
            409,
            "Версия проекта уже изменилась. Сначала прочитай свежий проект и повтори ход.",
            code="revision_conflict",
            details={"current_revision": int(row["revision"])},
        )


def layer_write_allowed(actor: dict[str, Any], layer: dict[str, Any]) -> bool:
    if actor["kind"] == "owner" or actor["allowed_layers"] == "*":
        return True
    allowed = actor.get("allowed_layers") or []
    return layer.get("id") in allowed


def hex_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def composite_color_maps(data: dict[str, Any], frame_index: int) -> dict[int, str]:
    frame = data["frames"][frame_index]
    pixels: dict[int, str] = {}
    for layer_index, layer in enumerate(data["layers"]):
        if not layer.get("visible", True) or float(layer.get("opacity", 1)) <= 0:
            continue
        layer_pixels = frame["layers"][layer_index]
        for raw_index, color in layer_pixels.items():
            try:
                pixels[int(raw_index)] = color
            except (ValueError, TypeError):
                continue
    return pixels


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def png_bytes(data: dict[str, Any], frame_index: int, scale: int) -> bytes:
    width, height = data["width"], data["height"]
    if width * height > MAX_EXPORT_CELLS:
        raise APIError(413, "Этот холст слишком большой для PNG-экспорта на сервере.", code="export_too_large")
    scale = max(1, min(32, int(scale)))
    output_width, output_height = width * scale, height * scale
    if output_width * output_height > 64_000_000:
        raise APIError(413, "Выбранный масштаб создаёт слишком большой PNG. Уменьши параметр scale.", code="export_too_large")
    final = composite_color_maps(data, frame_index)
    rows = bytearray()
    for y in range(height):
        row = bytearray()
        for x in range(width):
            color = final.get(y * width + x)
            if color:
                r, g, b = hex_rgb(color)
                pixel = bytes((r, g, b, 255))
            else:
                pixel = b"\x00\x00\x00\x00"
            row.extend(pixel * scale)
        for _ in range(scale):
            rows.append(0)  # filter type
            rows.extend(row)
    signature = b"\x89PNG\r\n\x1a\n"
    header = struct.pack(">IIBBBBB", output_width, output_height, 8, 6, 0, 0, 0)
    return signature + png_chunk(b"IHDR", header) + png_chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + png_chunk(b"IEND", b"")


def pack_word(value: int) -> bytes:
    return struct.pack("<H", value)


def gif_lzw(indices: bytes | bytearray, minimum_code_size: int) -> bytes:
    """GIF LZW stream, little-endian packed codes."""
    clear = 1 << minimum_code_size
    end = clear + 1
    code_size = minimum_code_size + 1
    next_code = end + 1
    dictionary: dict[tuple[int, ...], int] = {(value,): value for value in range(clear)}
    output = bytearray()
    accumulator = 0
    bits = 0

    def write(code: int) -> None:
        nonlocal accumulator, bits
        accumulator |= code << bits
        bits += code_size
        while bits >= 8:
            output.append(accumulator & 0xFF)
            accumulator >>= 8
            bits -= 8

    def reset() -> None:
        nonlocal dictionary, code_size, next_code
        dictionary = {(value,): value for value in range(clear)}
        code_size = minimum_code_size + 1
        next_code = end + 1

    write(clear)
    if not indices:
        write(end)
        return bytes(output)
    phrase = (indices[0],)
    for symbol in indices[1:]:
        extended = phrase + (symbol,)
        if extended in dictionary:
            phrase = extended
            continue
        write(dictionary[phrase])
        if next_code < 4096:
            dictionary[extended] = next_code
            next_code += 1
            # GIF decoders adopt the wider code one emitted symbol after the threshold.
            if next_code > (1 << code_size) and code_size < 12:
                code_size += 1
        else:
            write(clear)
            reset()
        phrase = (symbol,)
    write(dictionary[phrase])
    write(end)
    if bits:
        output.append(accumulator & 0xFF)
    return bytes(output)


def gif_sub_blocks(payload: bytes) -> bytes:
    blocks = bytearray()
    for offset in range(0, len(payload), 255):
        part = payload[offset : offset + 255]
        blocks.append(len(part))
        blocks.extend(part)
    blocks.append(0)
    return bytes(blocks)


def gif_bytes(data: dict[str, Any]) -> bytes:
    width, height = data["width"], data["height"]
    if width > 65535 or height > 65535:
        raise APIError(422, "GIF ограничен 65 535 px по ширине и высоте.", code="gif_limit")
    if width * height > MAX_EXPORT_CELLS:
        raise APIError(413, "Этот холст слишком большой для GIF-экспорта на сервере.", code="export_too_large")
    final_frames = [composite_color_maps(data, frame_index) for frame_index in range(len(data["frames"]))]
    colors: list[str] = []
    color_indices: dict[str, int] = {}
    for pixels in final_frames:
        for color in pixels.values():
            if color not in color_indices:
                if len(colors) >= 255:
                    raise APIError(422, "GIF поддерживает до 255 непрозрачных цветов.", code="gif_palette_limit")
                color_indices[color] = len(colors) + 1
                colors.append(color)
    table_size = 2
    while table_size < len(colors) + 1:
        table_size <<= 1
    minimum_code_size = max(2, (table_size - 1).bit_length())
    packed_size = 0x80 | 0x70 | ((table_size.bit_length() - 2) & 0x07)
    payload = bytearray(b"GIF89a")
    payload.extend(pack_word(width))
    payload.extend(pack_word(height))
    payload.extend(bytes((packed_size, 0, 0)))
    payload.extend(b"\x00\x00\x00")  # transparent index 0
    for color in colors:
        payload.extend(bytes(hex_rgb(color)))
    payload.extend(b"\x00\x00\x00" * (table_size - len(colors) - 1))
    payload.extend(b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00")
    delay = max(2, round(100 / max(1, int(data.get("fps", 8)))))
    cell_count = width * height
    for pixels in final_frames:
        indices = bytearray(cell_count)
        for index, color in pixels.items():
            if 0 <= index < cell_count:
                indices[index] = color_indices[color]
        compressed = gif_lzw(indices, minimum_code_size)
        payload.extend(b"\x21\xf9\x04\x09")
        payload.extend(pack_word(delay))
        payload.extend(b"\x00\x00")
        payload.extend(b"\x2c\x00\x00\x00\x00")
        payload.extend(pack_word(width))
        payload.extend(pack_word(height))
        payload.append(0)
        payload.append(minimum_code_size)
        payload.extend(gif_sub_blocks(compressed))
    payload.append(0x3B)
    return bytes(payload)


DOCS = {
    "service": "PIXELPORT Agent API",
    "version": "v1",
    "authentication": "Write endpoints require X-Agent-Key. Project creation returns an owner_key once; the owner can mint per-agent keys.",
    "concurrency": "Send if_revision on writes. A 409 response includes current_revision, so the agent can GET the project, re-plan, and retry.",
    "endpoints": [
        {"method": "POST", "path": "/api/projects", "purpose": "Create a public-read project. Returns project id and owner_key once."},
        {"method": "GET", "path": "/api/projects/{project_id}/manifest", "purpose": "Small agent-readable description of canvas, layers, frames, jobs and API links."},
        {"method": "GET", "path": "/api/projects/{project_id}", "purpose": "Read the full sparse project state."},
        {"method": "POST", "path": "/api/projects/{project_id}/agents", "purpose": "Owner mints a layer-scoped agent key."},
        {"method": "POST", "path": "/api/projects/{project_id}/pixels", "purpose": "Agent writes up to 50,000 explicit pixels per request."},
        {"method": "POST", "path": "/api/projects/{project_id}/jobs", "purpose": "Create, claim, complete or cancel a shared task."},
        {"method": "POST", "path": "/api/projects/{project_id}/frames", "purpose": "Create, duplicate, rename or delete a frame."},
        {"method": "POST", "path": "/api/projects/{project_id}/layers", "purpose": "Owner changes layers; agents can read their assignments."},
        {"method": "GET", "path": "/api/projects/{project_id}/events?after=0", "purpose": "Poll incremental collaboration events."},
        {"method": "GET", "path": "/api/projects/{project_id}/export/png?frame=0&scale=16", "purpose": "Download a transparent rendered PNG."},
        {"method": "GET", "path": "/api/projects/{project_id}/export/gif", "purpose": "Download all frames as a looping GIF."},
    ],
}


class PixelportHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PIXELPORT/1.0"

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write(f"[{utc_now()}] {self.address_string()} {fmt % args}\n")

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Agent-Key, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.handle_api("GET", parsed)
        else:
            if parsed.path == "/":
                self.path = "/index.html"
            super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self.send_json({"ok": False, "error": {"code": "not_found", "message": "API route not found."}}, 404)
            return
        self.handle_api("POST", parsed)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.handle_api("PUT", parsed)
        else:
            self.send_error(404)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.handle_api("DELETE", parsed)
        else:
            self.send_error(404)

    def json_body(self) -> dict[str, Any]:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise APIError(400, "Некорректный Content-Length.", code="invalid_body") from None
        if size < 0 or size > MAX_BODY:
            raise APIError(413, f"JSON-запрос ограничен {MAX_BODY // (1024 * 1024)} MB.", code="body_too_large")
        if size == 0:
            return {}
        raw = self.rfile.read(size)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise APIError(400, "Тело запроса должно быть JSON в UTF-8.", code="invalid_json") from None
        if not isinstance(body, dict):
            raise APIError(400, "Верхний уровень JSON должен быть объектом.", code="invalid_json")
        return body

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_binary(self, payload: bytes, content_type: str, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def handle_api(self, method: str, parsed: Any) -> None:
        try:
            self.route_api(method, parsed)
        except APIError as error:
            response: dict[str, Any] = {"ok": False, "error": {"code": error.code, "message": error.message}}
            if error.details is not None:
                response["error"]["details"] = error.details
            self.send_json(response, error.status)
        except BrokenPipeError:
            return
        except Exception as error:  # do not leak tracebacks to agents, but keep them in the server log
            print(f"Unexpected API error: {error!r}", file=sys.stderr)
            self.send_json({"ok": False, "error": {"code": "internal_error", "message": "Внутренняя ошибка сервера."}}, 500)

    def route_api(self, method: str, parsed: Any) -> None:
        path = parsed.path.rstrip("/") or "/"
        parts = [unquote(part) for part in path.split("/") if part]
        query = parse_qs(parsed.query)
        if path == "/api/health" and method == "GET":
            self.send_json({"ok": True, "service": "pixelport", "time": utc_now(), "storage": str(DB_PATH.name)})
            return
        if path == "/api/docs" and method == "GET":
            self.send_json({"ok": True, "docs": DOCS})
            return
        if path == "/api/projects":
            if method == "GET":
                self.list_projects()
                return
            if method == "POST":
                self.create_project(self.json_body())
                return
        if len(parts) >= 3 and parts[:2] == ["api", "projects"]:
            project_id = parts[2]
            if not SAFE_ID.match(project_id):
                raise APIError(404, "Некорректный id проекта.", code="not_found")
            subpath = parts[3:]
            if not subpath:
                if method == "GET":
                    self.read_project(project_id)
                    return
                if method in {"PUT", "POST"}:
                    self.replace_project(project_id, self.json_body())
                    return
                if method == "DELETE":
                    self.delete_project(project_id)
                    return
            elif subpath == ["manifest"] and method == "GET":
                self.read_manifest(project_id)
                return
            elif subpath == ["agents"]:
                if method == "GET":
                    self.list_agents(project_id)
                    return
                if method == "POST":
                    self.create_agent(project_id, self.json_body())
                    return
            elif subpath == ["pixels"] and method == "POST":
                self.write_pixels(project_id, self.json_body())
                return
            elif subpath == ["frames"] and method == "POST":
                self.frame_operation(project_id, self.json_body())
                return
            elif subpath == ["layers"] and method == "POST":
                self.layer_operation(project_id, self.json_body())
                return
            elif subpath == ["jobs"]:
                if method == "GET":
                    self.list_jobs(project_id)
                    return
                if method == "POST":
                    self.job_operation(project_id, self.json_body())
                    return
            elif subpath == ["events"] and method == "GET":
                self.read_events(project_id, query)
                return
            elif subpath == ["export", "png"] and method == "GET":
                self.export_png(project_id, query)
                return
            elif subpath == ["export", "gif"] and method == "GET":
                self.export_gif(project_id)
                return
        raise APIError(404, "Маршрут API не найден. Открой /api/docs.", code="not_found")

    def list_projects(self) -> None:
        with DB_LOCK, db_connection() as connection:
            rows = connection.execute("SELECT * FROM projects WHERE public_read = 1 ORDER BY updated_at DESC LIMIT 100").fetchall()
            projects = [project_summary(row, parse_data(row)) for row in rows]
        self.send_json({"ok": True, "projects": projects})

    def create_project(self, body: dict[str, Any]) -> None:
        raw_project = body.get("project") if isinstance(body.get("project"), dict) else body
        if isinstance(body.get("name"), str) and "projectName" not in raw_project:
            raw_project = {**raw_project, "projectName": body["name"]}
        data = normalize_project(raw_project, fallback_name="Агентная пиксельная сессия")
        public_read = body.get("public_read", True) is not False
        project_id = random_id("px")
        owner_key = f"px_owner_{secrets.token_urlsafe(28)}"
        now = utc_now()
        with DB_LOCK, db_connection() as connection:
            connection.execute(
                "INSERT INTO projects(id, name, data_json, owner_key, public_read, revision, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                (project_id, data["projectName"], json.dumps(data, ensure_ascii=False, separators=(",", ":")), owner_key, int(public_read), now, now),
            )
            event_id = event(connection, project_id, 1, "project.created", "owner", {"width": data["width"], "height": data["height"], "frames": len(data["frames"]), "layers": len(data["layers"])})
        self.send_json({
            "ok": True,
            "project": {"id": project_id, "name": data["projectName"], "revision": 1, "width": data["width"], "height": data["height"], "public_read": public_read},
            "owner_key": owner_key,
            "event_id": event_id,
            "warning": "Сохрани owner_key сейчас: сервер больше не покажет его в ответах чтения.",
        }, 201)

    def read_project(self, project_id: str) -> None:
        with DB_LOCK, db_connection() as connection:
            row = load_project(connection, project_id)
            if not row["public_read"] and not get_key(self):
                raise APIError(401, "Этот проект не публичный. Передай X-Agent-Key.", code="missing_key")
            if not row["public_read"]:
                authenticate(connection, row, get_key(self))
            data = parse_data(row)
            summary = project_summary(row, data)
        self.send_json({"ok": True, "project": {**summary, "data": data}})

    def read_manifest(self, project_id: str) -> None:
        with DB_LOCK, db_connection() as connection:
            row = load_project(connection, project_id)
            if not row["public_read"] and not get_key(self):
                raise APIError(401, "Этот проект не публичный. Передай X-Agent-Key.", code="missing_key")
            if not row["public_read"]:
                authenticate(connection, row, get_key(self))
            data = parse_data(row)
            layer_info = [{"index": i, "id": layer["id"], "name": layer["name"], "visible": layer["visible"], "opacity": layer["opacity"]} for i, layer in enumerate(data["layers"])]
            frame_info = [{"index": i, "id": frame["id"], "name": frame["name"], "painted_pixels": sum(len(layer) for layer in frame["layers"])} for i, frame in enumerate(data["frames"])]
            agents = connection.execute("SELECT id, name, role, allowed_layers_json, last_seen_at FROM agents WHERE project_id = ? ORDER BY created_at", (project_id,)).fetchall()
            agent_info = []
            for agent in agents:
                try:
                    allowed = json.loads(agent["allowed_layers_json"])
                except json.JSONDecodeError:
                    allowed = []
                agent_info.append({"id": agent["id"], "name": agent["name"], "role": agent["role"], "allowed_layers": allowed, "last_seen_at": agent["last_seen_at"]})
        base = f"/api/projects/{project_id}"
        self.send_json({
            "ok": True,
            "manifest": {
                "protocol": "pixelport-agent/v1",
                "project": project_summary(row, data),
                "canvas": {"width": data["width"], "height": data["height"], "coordinate_system": "origin top-left; x grows right; y grows down; integer pixels only"},
                "palette": data["palette"],
                "layers": layer_info,
                "frames": frame_info,
                "jobs": data.get("jobs", []),
                "registered_agents": agent_info,
                "write_contract": {"endpoint": f"{base}/pixels", "auth_header": "X-Agent-Key", "optimistic_lock": "if_revision", "max_operations": MAX_OPERATIONS},
                "links": {"project": base, "events": f"{base}/events?after=0", "docs": "/api/docs", "png": f"{base}/export/png?frame=0&scale=16", "gif": f"{base}/export/gif"},
                "rule": "Write explicit #RRGGBB pixels only. Do not send generated raster blobs as an agent action.",
            },
        })

    def replace_project(self, project_id: str, body: dict[str, Any]) -> None:
        raw_project = body.get("project") if isinstance(body.get("project"), dict) else body
        with DB_LOCK, db_connection() as connection:
            row = load_project(connection, project_id)
            actor = authenticate(connection, row, get_key(self), owner_only=True)
            check_revision(row, body)
            data = normalize_project(raw_project, fallback_name=parse_data(row)["projectName"])
            revision, event_id = save_project(connection, row, data, event_type="project.replaced", actor=actor["name"], payload={"width": data["width"], "height": data["height"], "frames": len(data["frames"]), "layers": len(data["layers"])})
        self.send_json({"ok": True, "project_id": project_id, "revision": revision, "event_id": event_id})

    def delete_project(self, project_id: str) -> None:
        with DB_LOCK, db_connection() as connection:
            row = load_project(connection, project_id)
            authenticate(connection, row, get_key(self), owner_only=True)
            connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        self.send_json({"ok": True, "deleted": project_id})

    def list_agents(self, project_id: str) -> None:
        with DB_LOCK, db_connection() as connection:
            row = load_project(connection, project_id)
            key = get_key(self)
            actor = authenticate(connection, row, key) if key else None
            rows = connection.execute("SELECT id, name, role, allowed_layers_json, created_at, last_seen_at FROM agents WHERE project_id = ? ORDER BY created_at", (project_id,)).fetchall()
            agents = []
            for agent in rows:
                try:
                    allowed = json.loads(agent["allowed_layers_json"])
                except json.JSONDecodeError:
                    allowed = []
                agents.append({"id": agent["id"], "name": agent["name"], "role": agent["role"], "allowed_layers": allowed, "created_at": agent["created_at"], "last_seen_at": agent["last_seen_at"]})
        self.send_json({"ok": True, "agents": agents, "viewer": actor["kind"] if actor else "public"})

    def create_agent(self, project_id: str, body: dict[str, Any]) -> None:
        name = clean_string(body.get("name"), "name", 48)
        role = clean_string(body.get("role", "pixel artist"), "role", 48)
        with DB_LOCK, db_connection() as connection:
            row = load_project(connection, project_id)
            actor = authenticate(connection, row, get_key(self), owner_only=True)
            data = parse_data(row)
            allowed_raw = body.get("allowed_layers", "*")
            if allowed_raw == "*":
                allowed = "*"
            elif isinstance(allowed_raw, list):
                allowed_ids = []
                for layer_value in allowed_raw:
                    index = resolve_index(layer_value, data["layers"], "allowed_layers")
                    layer_id = data["layers"][index]["id"]
                    if layer_id not in allowed_ids:
                        allowed_ids.append(layer_id)
                allowed = allowed_ids
            else:
                raise APIError(422, "allowed_layers: используй '*' или массив индексов/id слоёв.", code="validation_error")
            agent_id = random_id("agent")
            agent_key = f"px_agent_{secrets.token_urlsafe(28)}"
            try:
                connection.execute(
                    "INSERT INTO agents(id, project_id, name, role, api_key, allowed_layers_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (agent_id, project_id, name, role, agent_key, json.dumps(allowed, ensure_ascii=False), utc_now()),
                )
            except sqlite3.IntegrityError:
                raise APIError(409, "Агент с таким именем уже есть в проекте.", code="agent_exists") from None
            revision, event_id = save_project(connection, row, data, event_type="agent.created", actor=actor["name"], payload={"agent_id": agent_id, "name": name, "role": role, "allowed_layers": allowed})
        self.send_json({
            "ok": True,
            "agent": {"id": agent_id, "name": name, "role": role, "allowed_layers": allowed},
            "agent_key": agent_key,
            "revision": revision,
            "event_id": event_id,
            "warning": "Сохрани agent_key сейчас и передай только конкретному агенту.",
        }, 201)

    def write_pixels(self, project_id: str, body: dict[str, Any]) -> None:
        operations = body.get("operations")
        if not isinstance(operations, list) or not operations:
            raise APIError(422, "operations должен быть непустым массивом пиксельных команд.", code="validation_error")
        if len(operations) > MAX_OPERATIONS:
            raise APIError(413, f"За один ход можно передать не больше {MAX_OPERATIONS:,} пикселей.", code="too_many_operations")
        message = body.get("message", "")
        if message is not None and (not isinstance(message, str) or len(message) > 600):
            raise APIError(422, "message: максимум 600 символов.", code="validation_error")
        with DB_LOCK, db_connection() as connection:
            row = load_project(connection, project_id)
            actor = authenticate(connection, row, get_key(self))
            check_revision(row, body)
            data = parse_data(row)
            changed = 0
            touched: set[tuple[int, int]] = set()
            for operation in operations:
                if not isinstance(operation, dict):
                    raise APIError(422, "Каждая операция должна быть JSON-объектом.", code="validation_error")
                frame_index = resolve_index(operation.get("frame", data.get("activeFrame", 0)), data["frames"], "frame")
                layer_index = resolve_index(operation.get("layer", data.get("activeLayer", 0)), data["layers"], "layer")
                layer_meta = data["layers"][layer_index]
                if not layer_write_allowed(actor, layer_meta):
                    raise APIError(403, f"Агенту «{actor['name']}» не назначен слой «{layer_meta['name']}».", code="layer_forbidden")
                # x/y are zero-based and may legitimately be zero.
                try:
                    x_raw, y_raw = operation.get("x"), operation.get("y")
                    if isinstance(x_raw, bool) or isinstance(y_raw, bool):
                        raise ValueError
                    x, y = int(x_raw), int(y_raw)
                except (TypeError, ValueError):
                    raise APIError(422, "x и y должны быть целыми координатами.", code="validation_error") from None
                if not (0 <= x < data["width"] and 0 <= y < data["height"]):
                    raise APIError(422, f"Координата ({x}, {y}) за пределами {data['width']}×{data['height']}.", code="out_of_bounds")
                if "color" not in operation:
                    raise APIError(422, "Каждая операция должна содержать color (#RRGGBB или null для стирания).", code="validation_error")
                color = normalize_hex(operation.get("color"), allow_null=True)
                pixels = data["frames"][frame_index]["layers"][layer_index]
                key = str(y * data["width"] + x)
                before = pixels.get(key)
                if color is None:
                    if key in pixels:
                        del pixels[key]
                        changed += 1
                elif before != color:
                    pixels[key] = color
                    changed += 1
                touched.add((frame_index, layer_index))
            if not changed:
                self.send_json({"ok": True, "project_id": project_id, "revision": row["revision"], "changes": 0, "event_id": None})
                return
            revision, event_id = save_project(connection, row, data, event_type="pixels.written", actor=actor["name"], payload={"changes": changed, "operations": len(operations), "touched": [{"frame": f, "layer": l} for f, l in sorted(touched)], "message": message or None})
        self.send_json({"ok": True, "project_id": project_id, "revision": revision, "changes": changed, "event_id": event_id})

    def frame_operation(self, project_id: str, body: dict[str, Any]) -> None:
        action = body.get("action")
        if action not in {"create", "duplicate", "delete", "rename"}:
            raise APIError(422, "action: create, duplicate, delete или rename.", code="validation_error")
        with DB_LOCK, db_connection() as connection:
            row = load_project(connection, project_id)
            actor = authenticate(connection, row, get_key(self))
            check_revision(row, body)
            data = parse_data(row)
            if action in {"delete", "rename"} and actor["kind"] != "owner":
                raise APIError(403, "Удалять и переименовывать кадры может только владелец.", code="owner_required")
            frames = data["frames"]
            if action == "create":
                if len(frames) >= MAX_FRAMES:
                    raise APIError(422, f"В проекте максимум {MAX_FRAMES} кадров.", code="frame_limit")
                index = body.get("index", len(frames))
                try:
                    index = max(0, min(len(frames), int(index)))
                except (TypeError, ValueError):
                    raise APIError(422, "index должен быть числом.", code="validation_error") from None
                name = str(body.get("name") or f"Кадр {len(frames) + 1:02d}").strip()[:48] or f"Кадр {len(frames) + 1:02d}"
                frame = {"id": random_id("frame"), "name": name, "layers": [{} for _ in data["layers"]]}
                frames.insert(index, frame)
                result = {"index": index, "frame": {"id": frame["id"], "name": frame["name"]}}
            elif action == "duplicate":
                if len(frames) >= MAX_FRAMES:
                    raise APIError(422, f"В проекте максимум {MAX_FRAMES} кадров.", code="frame_limit")
                source_index = resolve_index(body.get("source", data.get("activeFrame", 0)), frames, "source")
                source = frames[source_index]
                frame = json.loads(json.dumps(source))
                frame["id"] = random_id("frame")
                frame["name"] = str(body.get("name") or f"{source['name']} copy").strip()[:48]
                index = source_index + 1
                frames.insert(index, frame)
                result = {"index": index, "frame": {"id": frame["id"], "name": frame["name"]}, "source": source_index}
            elif action == "delete":
                if len(frames) <= 1:
                    raise APIError(422, "В проекте должен остаться хотя бы один кадр.", code="frame_limit")
                index = resolve_index(body.get("frame", data.get("activeFrame", 0)), frames, "frame")
                deleted = frames.pop(index)
                data["activeFrame"] = min(data.get("activeFrame", 0), len(frames) - 1)
                result = {"deleted": {"index": index, "id": deleted["id"], "name": deleted["name"]}}
            else:
                index = resolve_index(body.get("frame", data.get("activeFrame", 0)), frames, "frame")
                name = clean_string(body.get("name"), "name", 48)
                frames[index]["name"] = name
                result = {"index": index, "frame": {"id": frames[index]["id"], "name": name}}
            revision, event_id = save_project(connection, row, data, event_type=f"frame.{action}", actor=actor["name"], payload=result)
        self.send_json({"ok": True, "revision": revision, "event_id": event_id, "result": result})

    def layer_operation(self, project_id: str, body: dict[str, Any]) -> None:
        action = body.get("action")
        if action not in {"create", "delete", "update"}:
            raise APIError(422, "action: create, delete или update.", code="validation_error")
        with DB_LOCK, db_connection() as connection:
            row = load_project(connection, project_id)
            actor = authenticate(connection, row, get_key(self), owner_only=True)
            check_revision(row, body)
            data = parse_data(row)
            layers = data["layers"]
            if action == "create":
                if len(layers) >= MAX_LAYERS:
                    raise APIError(422, f"В проекте максимум {MAX_LAYERS} слоя.", code="layer_limit")
                name = str(body.get("name") or f"Слой {len(layers) + 1}").strip()[:48] or f"Слой {len(layers) + 1}"
                layer = {"id": random_id("layer"), "name": name, "visible": True, "opacity": 1.0}
                layers.append(layer)
                for frame in data["frames"]:
                    frame["layers"].append({})
                result = {"index": len(layers) - 1, "layer": layer}
            elif action == "delete":
                if len(layers) <= 1:
                    raise APIError(422, "В проекте должен остаться хотя бы один слой.", code="layer_limit")
                index = resolve_index(body.get("layer", data.get("activeLayer", 0)), layers, "layer")
                deleted = layers.pop(index)
                for frame in data["frames"]:
                    frame["layers"].pop(index)
                data["activeLayer"] = min(data.get("activeLayer", 0), len(layers) - 1)
                result = {"deleted": {"index": index, **deleted}}
            else:
                index = resolve_index(body.get("layer", data.get("activeLayer", 0)), layers, "layer")
                layer = layers[index]
                if "name" in body:
                    layer["name"] = clean_string(body["name"], "name", 48)
                if "visible" in body:
                    layer["visible"] = body["visible"] is not False
                if "opacity" in body:
                    layer["opacity"] = clamp_opacity(body["opacity"])
                result = {"index": index, "layer": layer}
            revision, event_id = save_project(connection, row, data, event_type=f"layer.{action}", actor=actor["name"], payload=result)
        self.send_json({"ok": True, "revision": revision, "event_id": event_id, "result": result})

    def list_jobs(self, project_id: str) -> None:
        with DB_LOCK, db_connection() as connection:
            row = load_project(connection, project_id)
            if not row["public_read"]:
                authenticate(connection, row, get_key(self))
            data = parse_data(row)
        self.send_json({"ok": True, "project_id": project_id, "revision": row["revision"], "jobs": data.get("jobs", [])})

    def job_operation(self, project_id: str, body: dict[str, Any]) -> None:
        action = body.get("action")
        if action not in {"create", "claim", "complete", "cancel"}:
            raise APIError(422, "action: create, claim, complete или cancel.", code="validation_error")
        with DB_LOCK, db_connection() as connection:
            row = load_project(connection, project_id)
            actor = authenticate(connection, row, get_key(self))
            check_revision(row, body)
            data = parse_data(row)
            jobs = data.setdefault("jobs", [])
            if action == "create":
                if actor["kind"] != "owner":
                    raise APIError(403, "Создавать задания может владелец проекта.", code="owner_required")
                description = clean_string(body.get("description"), "description", 600)
                target = "all" if body.get("target") == "all" else "active"
                frame = resolve_index(body.get("frame", data.get("activeFrame", 0)), data["frames"], "frame")
                layer = resolve_index(body.get("layer", data.get("activeLayer", 0)), data["layers"], "layer")
                job = {"id": random_id("job"), "description": description, "target": target, "frame": frame, "layer": layer, "status": "queued", "agent_id": None, "created_at": utc_now(), "completed_at": None}
                jobs.append(job)
                result = {"job": job}
            else:
                job_id = body.get("job_id")
                if not isinstance(job_id, str):
                    raise APIError(422, "job_id обязателен.", code="validation_error")
                job = next((item for item in jobs if item.get("id") == job_id), None)
                if not job:
                    raise APIError(404, "Задание не найдено.", code="not_found")
                if action == "claim":
                    if job["status"] != "queued":
                        raise APIError(409, "Можно взять только задание со статусом queued.", code="job_not_available")
                    job["status"] = "claimed"; job["agent_id"] = actor["id"]
                elif action == "complete":
                    if actor["kind"] != "owner" and job.get("agent_id") != actor["id"]:
                        raise APIError(403, "Завершить задание может взявший его агент.", code="job_forbidden")
                    job["status"] = "done"; job["completed_at"] = utc_now()
                else:
                    if actor["kind"] != "owner":
                        raise APIError(403, "Отменять задания может владелец.", code="owner_required")
                    job["status"] = "cancelled"
                result = {"job": job}
            revision, event_id = save_project(connection, row, data, event_type=f"job.{action}", actor=actor["name"], payload=result)
        self.send_json({"ok": True, "revision": revision, "event_id": event_id, "result": result})

    def read_events(self, project_id: str, query: dict[str, list[str]]) -> None:
        after_value = query.get("after", ["0"])[0]
        try:
            after = max(0, int(after_value))
        except (TypeError, ValueError):
            raise APIError(422, "after должен быть числом.", code="validation_error") from None
        with DB_LOCK, db_connection() as connection:
            row = load_project(connection, project_id)
            if not row["public_read"]:
                authenticate(connection, row, get_key(self))
            rows = connection.execute("SELECT * FROM events WHERE project_id = ? AND id > ? ORDER BY id ASC LIMIT 100", (project_id, after)).fetchall()
            events = []
            for event_row in rows:
                events.append({"id": event_row["id"], "revision": event_row["revision"], "type": event_row["event_type"], "actor": event_row["actor"], "payload": json.loads(event_row["payload_json"]), "created_at": event_row["created_at"]})
        self.send_json({"ok": True, "project_id": project_id, "events": events})

    def export_png(self, project_id: str, query: dict[str, list[str]]) -> None:
        with DB_LOCK, db_connection() as connection:
            row = load_project(connection, project_id)
            if not row["public_read"]:
                authenticate(connection, row, get_key(self))
            data = parse_data(row)
        try:
            frame = int(query.get("frame", [data.get("activeFrame", 0)])[0])
            scale = int(query.get("scale", [16])[0])
        except (TypeError, ValueError):
            raise APIError(422, "frame и scale должны быть числами.", code="validation_error") from None
        if not 0 <= frame < len(data["frames"]):
            raise APIError(422, "Такого кадра нет.", code="validation_error")
        binary = png_bytes(data, frame, scale)
        name = re.sub(r"[^A-Za-z0-9_-]+", "-", data["projectName"])[:40] or "pixelport"
        self.send_binary(binary, "image/png", f"{name}-frame-{frame + 1}.png")

    def export_gif(self, project_id: str) -> None:
        with DB_LOCK, db_connection() as connection:
            row = load_project(connection, project_id)
            if not row["public_read"]:
                authenticate(connection, row, get_key(self))
            data = parse_data(row)
        binary = gif_bytes(data)
        name = re.sub(r"[^A-Za-z0-9_-]+", "-", data["projectName"])[:40] or "pixelport"
        self.send_binary(binary, "image/gif", f"{name}-loop.gif")


def main() -> None:
    initialize_db()
    mimetypes.add_type("application/javascript", ".js")
    server = ThreadingHTTPServer((HOST, PORT), PixelportHandler)
    print(f"PIXELPORT agent server listening on http://{HOST}:{PORT}")
    print(f"Database: {DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping PIXELPORT.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
