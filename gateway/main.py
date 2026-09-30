from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from gemini_webapi import GeminiClient
from gemini_webapi.utils import logger


DATA_DIR = Path(os.getenv("GATEWAY_DATA_DIR", "/data/gateway"))
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "gateway.sqlite3"
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "50")) * 1024 * 1024

GATEWAY_API_KEY = os.getenv("GATEWAY_API_KEY", "").strip()
GEMINI_1PSID = os.getenv("GEMINI_SECURE_1PSID", "").strip()
GEMINI_1PSIDTS = os.getenv("GEMINI_SECURE_1PSIDTS", "").strip()
GEMINI_PROXY = os.getenv("GEMINI_PROXY", "").strip() or None
GEMINI_COOKIE_PATH = os.getenv("GEMINI_COOKIE_PATH", "/data/gemini/cookies")
os.environ.setdefault("GEMINI_COOKIE_PATH", GEMINI_COOKIE_PATH)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class GatewayState:
    def __init__(self) -> None:
        self.client: GeminiClient | None = None
        self.locks: dict[str, asyncio.Lock] = {}
        self.started_at = time.time()

    def lock_for(self, key: str) -> asyncio.Lock:
        if key not in self.locks:
            self.locks[key] = asyncio.Lock()
        return self.locks[key]


state = GatewayState()


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                cid TEXT NOT NULL,
                rid TEXT NOT NULL,
                rcid TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                filename TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                size INTEGER NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )


def get_conversation(conversation_id: str) -> sqlite3.Row | None:
    with db() as connection:
        return connection.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()


def save_conversation(conversation_id: str, metadata: list[str], title: str = "") -> None:
    values = [str(x or "") for x in (metadata + ["", "", ""])[:3]]
    now = time.time()
    with db() as connection:
        connection.execute(
            """
            INSERT INTO conversations(id, cid, rid, rcid, title, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                cid = excluded.cid,
                rid = excluded.rid,
                rcid = excluded.rcid,
                title = CASE WHEN excluded.title <> '' THEN excluded.title ELSE conversations.title END,
                updated_at = excluded.updated_at
            """,
            (conversation_id, values[0], values[1], values[2], title[:200], now, now),
        )


def delete_conversation_record(conversation_id: str) -> None:
    with db() as connection:
        connection.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))


def safe_filename(name: str | None) -> str:
    raw = Path(name or "upload.bin").name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")
    return cleaned[:180] or "upload.bin"


def save_file_record(filename: str, mime_type: str, payload: bytes) -> dict[str, Any]:
    file_id = f"file_{secrets.token_urlsafe(12)}"
    safe_name = safe_filename(filename)
    path = UPLOAD_DIR / f"{file_id}_{safe_name}"
    path.write_bytes(payload)
    now = time.time()
    with db() as connection:
        connection.execute(
            "INSERT INTO files(id, path, filename, mime_type, size, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (file_id, str(path), safe_name, mime_type or "application/octet-stream", len(payload), now),
        )
    return {
        "id": file_id,
        "object": "file",
        "filename": safe_name,
        "bytes": len(payload),
        "created_at": int(now),
        "purpose": "assistants",
    }


def load_file_record(file_id: str) -> tuple[bytes, str]:
    with db() as connection:
        row = connection.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown file id: {file_id}")
    path = Path(row["path"])
    if not path.is_file():
        raise HTTPException(status_code=410, detail=f"File data is missing: {file_id}")
    return path.read_bytes(), row["filename"]


def delete_file_record(file_id: str) -> None:
    with db() as connection:
        row = connection.execute("SELECT path FROM files WHERE id = ?", (file_id,)).fetchone()
        connection.execute("DELETE FROM files WHERE id = ?", (file_id,))
    if row:
        with contextlib.suppress(OSError):
            Path(row["path"]).unlink()


def require_api_key(authorization: str | None, x_api_key: str | None) -> None:
    if not GATEWAY_API_KEY:
        raise HTTPException(status_code=503, detail="GATEWAY_API_KEY is not configured")
    candidate = x_api_key or ""
    if not candidate and authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            candidate = value.strip()
    if not candidate or not secrets.compare_digest(candidate, GATEWAY_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")


def auth_dependency(authorization: str | None = None, x_api_key: str | None = None) -> None:
    require_api_key(authorization, x_api_key)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    messages: list[dict[str, Any]] = Field(default_factory=list)
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    conversation_id: str | None = None
    gem: str | None = None
    temporary: bool = False
    deep_research: bool = False
    extended_thinking: bool = False
    reasoning_effort: str | None = None
    extra_body: dict[str, Any] | None = None


class NativeGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    prompt: str
    model: str | None = None
    conversation_id: str | None = None
    gem: str | None = None
    temporary: bool = False
    deep_research: bool = False
    extended_thinking: bool = False
    file_ids: list[str] = Field(default_factory=list)


class ImageGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    prompt: str
    model: str | None = None
    n: int = 1


def extract_text_and_files(
    messages: list[dict[str, Any]], use_history: bool
) -> tuple[str, list[tuple[bytes, str]]]:
    selected = messages[-1:] if use_history else messages
    if not selected:
        raise HTTPException(status_code=400, detail="messages must not be empty")

    blocks: list[str] = []
    files: list[tuple[bytes, str]] = []
    for message in selected:
        role = str(message.get("role", "user"))
        content = message.get("content", "")
        parts = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
        chunks: list[str] = []

        for part in parts:
            if not isinstance(part, dict):
                chunks.append(str(part))
                continue

            part_type = str(part.get("type", "text"))
            if part_type in {"text", "input_text"}:
                chunks.append(str(part.get("text", part.get("content", ""))))
                continue

            if part_type in {"image_url", "input_image"}:
                image_url = part.get("image_url", part.get("url", ""))
                if isinstance(image_url, dict):
                    image_url = image_url.get("url", "")
                image_url = str(image_url)
                match = re.match(r"^data:([^;]+);base64,(.+)$", image_url, re.DOTALL)
                if not match:
                    raise HTTPException(
                        status_code=400,
                        detail="Only data: image URLs are accepted directly; upload remote files to /v1/files first.",
                    )
                try:
                    payload = base64.b64decode(match.group(2), validate=True)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail="Invalid base64 image data") from exc
                ext = {
                    "image/jpeg": ".jpg",
                    "image/webp": ".webp",
                    "image/gif": ".gif",
                }.get(match.group(1).lower(), ".png")
                files.append((payload, f"input_{len(files) + 1}{ext}"))
                chunks.append("[image attached]")
                continue

            if part_type in {"file", "input_file"}:
                file_id = part.get("file_id")
                if isinstance(file_id, dict):
                    file_id = file_id.get("id")
                if not file_id:
                    raise HTTPException(status_code=400, detail="file_id is required for file parts")
                payload, filename = load_file_record(str(file_id))
                files.append((payload, filename))
                chunks.append(f"[file attached: {filename}]")
                continue

            chunks.append(str(part.get("text", "")))

        text = "\n".join(chunk for chunk in chunks if chunk.strip()).strip()
        if text:
            blocks.append(f"[{role}]\n{text}")

    prompt = "\n\n".join(blocks).strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="No textual content was provided")
    return prompt, files


def model_items() -> list[dict[str, Any]]:
    if state.client is None:
        return []
    return [
        {
            "id": model.model_name,
            "object": "model",
            "owned_by": "google",
            "display_name": model.display_name,
            "description": model.description,
            "model_id": model.model_id,
            "available": model.is_available,
            "aliases": model.aliases,
        }
        for model in (state.client.list_models() or [])
    ]


def serialize_output(output: Any) -> dict[str, Any]:
    return {
        "metadata": output.metadata,
        "text": output.text,
        "thoughts": output.thoughts,
        "images": [
            {
                "type": type(item).__name__,
                "url": item.url,
                "title": item.title,
                "alt": getattr(item, "alt", ""),
            }
            for item in output.images
        ],
        "videos": [
            {
                "type": type(item).__name__,
                "url": item.url,
                "title": item.title,
                "thumbnail": getattr(item, "thumbnail", ""),
            }
            for item in output.videos
        ],
        "media": [
            {
                "type": type(item).__name__,
                "url": getattr(item, "url", ""),
                "mp3_url": getattr(item, "mp3_url", ""),
                "title": item.title,
            }
            for item in output.media
        ],
        "citations": [
            {
                "id": item.id,
                "title": item.title,
                "url": item.url,
                "favicon": item.favicon,
            }
            for item in output.citations
        ],
        "deep_research_plan": output.deep_research_plan.model_dump() if output.deep_research_plan else None,
        "deep_research_document": output.deep_research_document.model_dump()
        if output.deep_research_document
        else None,
    }


async def create_or_get_session(
    conversation_id: str | None, model: str | None, gem: str | None
) -> tuple[Any, str | None]:
    if state.client is None:
        raise HTTPException(status_code=503, detail="Gemini client is not initialized")
    if conversation_id:
        row = get_conversation(conversation_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Unknown conversation_id")
        metadata = [row["cid"], row["rid"], row["rcid"]]
        return state.client.start_chat(metadata=metadata, model=model, gem=gem), conversation_id
    return state.client.start_chat(model=model, gem=gem), None


async def generate_native(
    prompt: str,
    model: str | None,
    conversation_id: str | None,
    gem: str | None,
    temporary: bool,
    deep_research: bool,
    extended_thinking: bool,
    files: list[tuple[bytes, str]],
) -> tuple[Any, str | None]:
    session, resolved_id = await create_or_get_session(conversation_id, model, gem)
    async with state.lock_for(resolved_id or "__new__"):
        output = await session.send_message(
            prompt,
            files=[payload for payload, _ in files] or None,
            temporary=temporary,
            deep_research=deep_research,
            extended_thinking=extended_thinking,
        )

    new_id = resolved_id
    if not temporary and output.metadata and output.metadata[0]:
        new_id = resolved_id or f"conv_{uuid4().hex}"
        save_conversation(new_id, output.metadata, title=prompt[:120].replace("\n", " "))
    return output, new_id


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    logger.info("Gateway data directory: {}", DATA_DIR)
    logger.info("Gemini cookie cache: {}", GEMINI_COOKIE_PATH)

    if not GATEWAY_API_KEY:
        logger.warning("GATEWAY_API_KEY is not configured")

    client: GeminiClient | None = None
    if GEMINI_1PSID or env_bool("ALLOW_GUEST_SESSION", False):
        client = GeminiClient(
            secure_1psid=GEMINI_1PSID or None,
            secure_1psidts=GEMINI_1PSIDTS or None,
            proxy=GEMINI_PROXY,
        )
        try:
            await client.init(
                timeout=float(os.getenv("GEMINI_TIMEOUT", "450")),
                auto_close=False,
                auto_refresh=env_bool("GEMINI_AUTO_REFRESH", True),
                refresh_interval=float(os.getenv("GEMINI_REFRESH_INTERVAL", "600")),
                watchdog_timeout=float(os.getenv("GEMINI_WATCHDOG_TIMEOUT", "120")),
                verbose=env_bool("GEMINI_VERBOSE", False),
            )
            state.client = client
            logger.success("Gemini Web session initialized")
        except Exception as exc:
            logger.exception("Gemini initialization failed: {}", exc)
            await client.close()
    else:
        logger.warning("Gemini Web credentials are not configured")

    try:
        yield
    finally:
        if state.client is not None:
            await state.client.close()
            state.client = None


app = FastAPI(
    title="Gemini Web Gateway",
    version="2026.09",
    description="Railway-ready HTTP gateway for the Gemini Web client.",
    lifespan=lifespan,
)

allowed_origins = [x.strip() for x in os.getenv("CORS_ORIGINS", "*").split(",") if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "gemini-web-gateway",
        "uptime_seconds": int(time.time() - state.started_at),
        "gemini_initialized": state.client is not None,
    }


@app.get("/ready")
async def ready() -> JSONResponse:
    if state.client is None:
        return JSONResponse(
            status_code=503,
            content={"ready": False, "reason": "gemini_not_initialized"},
        )
    return JSONResponse(
        status_code=200,
        content={
            "ready": True,
            "account_status": getattr(state.client.account_status, "name", "UNKNOWN"),
            "model_count": len(state.client.list_models() or []),
        },
    )


@app.get("/v1/models", dependencies=[Depends(auth_dependency)])
async def list_models() -> dict[str, Any]:
    return {"object": "list", "data": model_items()}


@app.get("/api/models", dependencies=[Depends(auth_dependency)])
async def api_models() -> dict[str, Any]:
    return {"models": model_items()}


@app.get("/api/account", dependencies=[Depends(auth_dependency)])
async def account() -> dict[str, Any]:
    client = state.client
    if client is None:
        raise HTTPException(status_code=503, detail="Gemini client is not initialized")
    status = client.account_status
    return {
        "status": getattr(status, "name", "UNKNOWN"),
        "description": getattr(status, "description", ""),
        "usage_info": client.usage_info,
        "quotas": client.quotas,
        "abuse_status": client.abuse_status,
    }


@app.get("/api/conversations", dependencies=[Depends(auth_dependency)])
async def conversations() -> dict[str, Any]:
    with db() as connection:
        rows = connection.execute(
            "SELECT id, cid, title, created_at, updated_at FROM conversations ORDER BY updated_at DESC"
        ).fetchall()
    return {"data": [dict(row) for row in rows]}


@app.get("/api/conversations/{conversation_id}", dependencies=[Depends(auth_dependency)])
async def conversation(conversation_id: str) -> dict[str, Any]:
    row = get_conversation(conversation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown conversation_id")
    if state.client is None:
        raise HTTPException(status_code=503, detail="Gemini client is not initialized")
    history = await state.client.read_chat(row["cid"], limit=100)
    return {
        "conversation_id": conversation_id,
        "metadata": [row["cid"], row["rid"], row["rcid"]],
        "turns": [{"role": turn.role, "text": turn.text} for turn in (history.turns if history else [])],
    }


@app.delete("/api/conversations/{conversation_id}", dependencies=[Depends(auth_dependency)])
async def delete_conversation(conversation_id: str) -> dict[str, Any]:
    row = get_conversation(conversation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown conversation_id")
    if state.client is None:
        raise HTTPException(status_code=503, detail="Gemini client is not initialized")
    await state.client.delete_chat(row["cid"])
    delete_conversation_record(conversation_id)
    state.locks.pop(conversation_id, None)
    return {"deleted": True, "conversation_id": conversation_id}


@app.post("/v1/files", dependencies=[Depends(auth_dependency)])
@app.post("/api/files", dependencies=[Depends(auth_dependency)])
async def upload_file(file: UploadFile = File(...)) -> dict[str, Any]:
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds MAX_UPLOAD_MB={MAX_UPLOAD_BYTES // 1024 // 1024}",
        )
    return save_file_record(
        file.filename or "upload.bin",
        file.content_type or "application/octet-stream",
        data,
    )


@app.delete("/v1/files/{file_id}", dependencies=[Depends(auth_dependency)])
@app.delete("/api/files/{file_id}", dependencies=[Depends(auth_dependency)])
async def delete_file(file_id: str) -> dict[str, Any]:
    delete_file_record(file_id)
    return {"deleted": True, "id": file_id}


@app.get("/v1/files/{file_id}", dependencies=[Depends(auth_dependency)])
@app.get("/api/files/{file_id}", dependencies=[Depends(auth_dependency)])
async def get_file(file_id: str) -> dict[str, Any]:
    with db() as connection:
        row = connection.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown file id")
    return dict(row)


@app.post("/api/generate", dependencies=[Depends(auth_dependency)])
async def native_generate(request: NativeGenerateRequest) -> dict[str, Any]:
    files = [load_file_record(file_id) for file_id in request.file_ids]
    output, conversation_id = await generate_native(
        request.prompt,
        request.model,
        request.conversation_id,
        request.gem,
        request.temporary,
        request.deep_research,
        request.extended_thinking,
        files,
    )
    return {"conversation_id": conversation_id, "result": serialize_output(output)}


@app.post("/api/research", dependencies=[Depends(auth_dependency)])
async def native_research(request: NativeGenerateRequest) -> dict[str, Any]:
    output, conversation_id = await generate_native(
        request.prompt,
        request.model,
        request.conversation_id,
        request.gem,
        request.temporary,
        True,
        request.extended_thinking,
        [],
    )
    return {"conversation_id": conversation_id, "result": serialize_output(output)}


@app.post("/v1/chat/completions", dependencies=[Depends(auth_dependency)])
async def chat_completions(request: ChatRequest) -> Any:
    prompt, files = extract_text_and_files(request.messages, use_history=bool(request.conversation_id))
    extra = request.extra_body or {}
    extended_thinking = request.extended_thinking or request.reasoning_effort in {"high", "xhigh"}
    if "extended_thinking" in extra:
        extended_thinking = bool(extra["extended_thinking"])
    deep_research = request.deep_research or bool(extra.get("deep_research", False))

    if request.stream:
        async def event_stream():
            session, resolved_id = await create_or_get_session(
                request.conversation_id, request.model, request.gem
            )
            lock = state.lock_for(resolved_id or "__new_stream")
            local_id = resolved_id or f"conv_{uuid4().hex}"
            async with lock:
                full_text = ""
                full_thoughts = ""
                last_output: Any = None
                try:
                    async for output in session.send_message_stream(
                        prompt,
                        files=[payload for payload, _ in files] or None,
                        temporary=request.temporary,
                        deep_research=deep_research,
                        extended_thinking=extended_thinking,
                    ):
                        last_output = output
                        text_delta = output.text_delta or ""
                        thought_delta = output.thoughts_delta or ""
                        full_text += text_delta
                        full_thoughts += thought_delta
                        delta: dict[str, Any] = {"role": "assistant"}
                        if text_delta:
                            delta["content"] = text_delta
                        if thought_delta:
                            delta["reasoning_content"] = thought_delta
                        payload = {
                            "id": f"chatcmpl_{secrets.token_hex(12)}",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": request.model or "gemini-web-default",
                            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

                    if (
                        last_output is not None
                        and not request.temporary
                        and last_output.metadata
                        and last_output.metadata[0]
                    ):
                        save_conversation(local_id, last_output.metadata, title=prompt[:120].replace("\n", " "))

                    final = {
                        "id": f"chatcmpl_{secrets.token_hex(12)}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": request.model or "gemini-web-default",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "conversation_id": local_id,
                        "x_gemini": {"thoughts": full_thoughts or None, "text": full_text},
                    }
                    yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                except Exception as exc:
                    logger.exception("Streaming generation failed: {}", exc)
                    error = {"error": {"message": str(exc), "type": "gemini_web_error"}}
                    yield f"data: {json.dumps(error, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    output, conversation_id = await generate_native(
        prompt,
        request.model,
        request.conversation_id,
        request.gem,
        request.temporary,
        deep_research,
        extended_thinking,
        files,
    )
    return {
        **{
            "id": f"chatcmpl_{secrets.token_hex(12)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": request.model or "gemini-web-default",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": output.text or "",
                        "reasoning_content": output.thoughts,
                    },
                    "finish_reason": "stop",
                }
            ],
        },
        "x_gemini": serialize_output(output),
        "conversation_id": conversation_id,
    }


@app.post("/v1/images/generations", dependencies=[Depends(auth_dependency)])
async def image_generations(request: ImageGenerationRequest) -> dict[str, Any]:
    output, _ = await generate_native(
        request.prompt, request.model, None, None, True, False, False, []
    )
    return {
        "created": int(time.time()),
        "data": [
            {"url": image.url, "revised_prompt": request.prompt}
            for image in output.images[: max(1, request.n)]
        ],
    }


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {"service": "gemini-web-gateway", "docs": "/docs", "health": "/health"}
