import asyncio
import json
import mimetypes
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote_from_bytes

import agent
import storage
import event_bus
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool


ROOT = Path(__file__).resolve().parent
TERMINAL_JOB_STATUSES = {"completed", "cancelled", "failed"}


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode()


def conversation_title(messages):
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()[:120]
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"].strip():
                    return part["text"].strip()[:120]
            if content:
                return "Csatolt fájl"
    return "Beszélgetés"


@asynccontextmanager
async def lifespan(application):
    await run_in_threadpool(storage.open_pool)
    try:
        await run_in_threadpool(agent.resume_pending_jobs)
        yield
    finally:
        await run_in_threadpool(storage.close_pool)


app = FastAPI(lifespan=lifespan)


async def _stream_job_events(job_id, after):
    subscription = event_bus.subscribe(job_id)
    last_event_id = after
    try:
        while True:
            events = await run_in_threadpool(storage.list_events, job_id, last_event_id)
            for event in events:
                event_id = int(event["event_id"])
                payload = json_bytes(event["payload"]).decode("utf-8")
                yield f"id: {event_id}\ndata: {payload}\n\n"
                last_event_id = event_id
            current = await run_in_threadpool(storage.get_job, job_id)
            if current is None or current["status"] in TERMINAL_JOB_STATUSES:
                return
            try:
                signal = await asyncio.wait_for(subscription.queue.get(), timeout=15)
                if signal.event_id > 0 and signal.event_id <= last_event_id:
                    continue
            except asyncio.TimeoutError:
                yield 'data: {"type":"keep-alive"}\n\n'
    finally:
        event_bus.unsubscribe(subscription)


def _event_stream_response(job_id, after, headers=None):
    response_headers = {
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
        "X-Content-Type-Options": "nosniff",
    }
    if headers:
        response_headers.update(headers)
    return StreamingResponse(
        _stream_job_events(job_id, after),
        media_type="text/event-stream",
        headers=response_headers,
    )


@app.get("/api/health")
async def health():
    return JSONResponse({"status": "ok"})


@app.get("/api/conversations")
async def conversations(request: Request):
    ids = [
        value
        for item in request.query_params.getlist("ids")
        for value in item.split(",")
        if value
    ]
    result = await run_in_threadpool(storage.list_conversations, ids)
    return JSONResponse({"conversations": result})


@app.get("/api/chat/jobs/{job_id}/events")
async def get_job_events(job_id: str, request: Request):
    try:
        after_id = max(0, int(request.query_params.get("after", "0")))
    except ValueError:
        return JSONResponse({"error": "Érvénytelen eseményazonosító"}, status_code=400)
    job = await run_in_threadpool(storage.get_job, job_id)
    if not job:
        return JSONResponse({"error": "A Work-feladat nem található"}, status_code=404)
    return _event_stream_response(job_id, after_id)


@app.post("/api/chat/jobs/{job_id}/stop")
async def stop_job(job_id: str):
    stopped = await run_in_threadpool(storage.request_cancel, job_id)
    return JSONResponse({"stopped": stopped}, status_code=200 if stopped else 409)


@app.post("/api/chat/jobs/{job_id}/redirect")
async def redirect_job(job_id: str, request: Request):
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        return JSONResponse({"error": "Érvénytelen JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "A JSON object is required"}, status_code=400)
    message = body.get("message", body.get("content"))
    valid_text = isinstance(message, str) and bool(message.strip())
    valid_parts = isinstance(message, list) and any(
        isinstance(part, dict)
        and (
            (part.get("type") == "text" and isinstance(part.get("text"), str) and part["text"].strip())
            or part.get("type") in ("image_url", "input_image")
            or "image_url" in part
        )
        for part in message
    )
    if not valid_text and not valid_parts:
        return JSONResponse({"error": "Az új irány nem lehet üres"}, status_code=400)
    redirect_content = message.strip() if valid_text else message
    conversation_id = await run_in_threadpool(
        storage.add_redirect,
        job_id,
        redirect_content,
    )
    if not conversation_id:
        return JSONResponse({"error": "A Work-feladat már nem fut"}, status_code=409)
    return JSONResponse({"accepted": True}, status_code=202)


@app.post("/api/chat")
async def start_chat(request: Request):
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        return JSONResponse(
            {"error": {"message": "Érvénytelen JSON"}},
            status_code=400,
        )
    if not isinstance(body, dict):
        return JSONResponse(
            {"error": {"message": "A JSON object is required"}},
            status_code=400,
        )
    supplied_id = body.get("conversation_id")
    conversation_id = (
        supplied_id
        if isinstance(supplied_id, str) and supplied_id.strip()
        else str(uuid.uuid4())
    )
    supplied_messages = body.get("messages")
    if not isinstance(supplied_messages, list):
        return JSONResponse(
            {"error": {"message": "A messages mezőnek tömbnek kell lennie"}},
            status_code=400,
        )
    messages = []
    for item in supplied_messages:
        if not isinstance(item, dict) or item.get("role") not in ("user", "assistant"):
            continue
        content = item.get("content")
        if isinstance(content, (str, list)):
            messages.append({"role": item["role"], "content": content})
    if not messages or not any(message["role"] == "user" for message in messages):
        return JSONResponse(
            {"error": {"message": "A beszélgetéshez legalább egy felhasználói üzenet szükséges"}},
            status_code=400,
        )
    title = conversation_title(messages)
    await run_in_threadpool(
        storage.upsert_conversation,
        conversation_id,
        messages,
        title,
    )
    job_id = str(uuid.uuid4())
    agent_messages = [{"role": "system", "content": agent.SYSTEM_MESSAGE}]
    agent_messages.extend(messages)
    await run_in_threadpool(
        storage.create_job,
        job_id,
        conversation_id,
        agent_messages,
    )
    event_id = await run_in_threadpool(
        storage.append_event,
        job_id,
        {
            "type": "meta",
            "conversation_id": conversation_id,
            "job_id": job_id,
            "work_task": True,
        },
    )
    event_bus.publish(job_id, event_id)
    agent.launch_job(job_id)
    return _event_stream_response(
        job_id,
        0,
        {"X-Chat-Job-Id": job_id},
    )


@app.get("/api/artifacts/{artifact_id}")
async def serve_artifact(artifact_id: str):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", artifact_id):
        return JSONResponse({"error": "A fájl nem található"}, status_code=404)
    artifact = await run_in_threadpool(storage.get_artifact, artifact_id)
    if not artifact:
        return JSONResponse({"error": "A fájl nem található"}, status_code=404)
    if artifact["content_type"] == "application/vnd.instavm.site-link+json":
        try:
            site = json.loads(bytes(artifact["content"]).decode())
            url = site["url"]
        except (ValueError, KeyError, UnicodeDecodeError):
            return JSONResponse({"error": "A Site-hivatkozás sérült"}, status_code=500)
        if not isinstance(url, str) or not url.startswith("https://"):
            return JSONResponse({"error": "A Site-hivatkozás érvénytelen"}, status_code=500)
        return RedirectResponse(
            url,
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )
    name = artifact["name"].replace("\r", "").replace("\n", "").replace('"', "")
    encoded_name = quote_from_bytes(name.encode("utf-8"))
    fallback_name = name.encode("ascii", "ignore").decode() or "artifact"
    disposition = f"attachment; filename=\"{fallback_name}\"; filename*=UTF-8''{encoded_name}"
    return Response(
        content=bytes(artifact["content"]),
        media_type=artifact["content_type"],
        headers={
            "Content-Disposition": disposition,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.api_route(
    "/api/{api_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def unknown_api(api_path: str):
    return JSONResponse({"error": "Nem található"}, status_code=404)


@app.api_route(
    "/{request_path:path}",
    methods=["GET", "HEAD"],
)
async def serve_static(request_path: str):
    relative = Path(request_path or "index.html")
    target = (ROOT / relative).resolve()
    try:
        target.relative_to(ROOT)
    except ValueError:
        return JSONResponse({"error": "Nem található"}, status_code=404)
    if not target.is_file():
        return JSONResponse({"error": "Nem található"}, status_code=404)
    content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if content_type.startswith("text/") or content_type in (
        "application/javascript",
        "application/manifest+json",
    ):
        content_type += "; charset=utf-8"
    return FileResponse(
        target,
        media_type=content_type,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def main():
    port = int(os.environ.get("PORT", "5000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
