import base64
import json
import mimetypes
import os
import posixpath
import shlex
import threading
import tempfile
import time
import uuid

from instavm import InstaVM
from openai import OpenAI

import storage


MODEL = "abliterated-model-large-v2"
IMAGE_MODEL = "abliterated-model"
SYSTEM_MESSAGE = """You are a capable work agent. Complete the user's request using the isolated InstaVM sandbox assigned to this conversation. The sandbox has a full shell, can run code, access the internet, and create files. Use the shell and file tools for real work instead of describing hypothetical work. Research current information when needed, inspect provided files, and create finished usable files when requested. Publish requested Sites so they have a working URL. Report meaningful progress as you work, preserve useful intermediate files in the sandbox, and use the artifact tool for every finished document, spreadsheet, presentation, report, or other downloadable file. Do not claim an operation succeeded unless its tool result confirms it. The user may redirect you during a task; follow the latest instruction before starting further work. Reply in the user's language."""


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "sandbox_execute",
            "description": "Run a shell command in this conversation's isolated InstaVM Linux sandbox. The shell has internet access and can run installed code, install packages, inspect the workspace, and create files.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sandbox_read_file",
            "description": "Read a file from the conversation's InstaVM sandbox and return its contents.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sandbox_write_file",
            "description": "Write complete text content to a file inside the conversation's InstaVM sandbox, creating parent directories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "publish_file",
            "description": "Publish a finished file from the sandbox as a downloadable artifact in the conversation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "name": {"type": "string"},
                },
                "required": ["path", "name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "publish_site",
            "description": "Start a web site from a sandbox directory and publish a working public InstaVM URL. Use when the user asks for a usable, shareable Site.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string"},
                    "port": {"type": "integer"},
                    "name": {"type": "string"},
                },
                "required": ["directory", "port", "name"],
                "additionalProperties": False,
            },
        },
    },
]


_running_jobs = set()
_running_lock = threading.Lock()


class JobCancelled(Exception):
    pass


def launch_job(job_id):
    with _running_lock:
        if job_id in _running_jobs:
            return
        _running_jobs.add(job_id)
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()


def resume_pending_jobs():
    for row in storage.pending_jobs():
        launch_job(row["job_id"])


def _event(job_id, payload):
    storage.append_event(job_id, payload)


def _progress(job_id, text):
    _event(
        job_id,
        {
            "type": "agent_event",
            "progress": text,
            "event": {"type": "progress", "step_id": text},
        },
    )


def _make_client(session_id=None):
    api_key = os.environ.get("INSTAVM_API_KEY")
    if not api_key:
        raise RuntimeError("INSTAVM_API_KEY is not configured")
    client = InstaVM(api_key=api_key, auto_start_session=False, timeout=3600, max_retries=0)
    if session_id:
        client.session_id = session_id
    return client


def _ensure_sandbox(conversation_id, vm_id, session_id, job_id):
    if vm_id and session_id:
        client = _make_client(session_id)
        try:
            record = client.vms.get(vm_id)
            if isinstance(record.get("vm"), dict):
                record = record["vm"]
            vm_status = str(record.get("status") or record.get("state") or "")
            vm_status = vm_status.strip().lower().replace("-", "_").replace(" ", "_")
            if vm_status in (
                "terminated",
                "deleted",
                "failed",
                "expired",
                "stopped",
                "killed",
                "unavailable",
                "not_available",
            ):
                vm_id = None
                session_id = None
        except Exception as error:
            status_code = getattr(getattr(error, "response", None), "status_code", None)
            if status_code not in (404, 410):
                raise
            vm_id = None
            session_id = None
        if vm_id and session_id:
            return client, vm_id, session_id
    _progress(job_id, "Új, elkülönített InstaVM sandbox indítása")
    client = _make_client()
    created = client.vms.create(
        wait=True,
        vm_lifetime_seconds=3600,
        auto_suspend=True,
        auto_resume=True,
    )
    vm_id = created.get("vm_id")
    session_id = created.get("session_id")
    if not vm_id or not session_id:
        raise RuntimeError("Az InstaVM nem adott vissza VM- és session-azonosítót")
    client.session_id = session_id
    storage.update_sandbox(conversation_id, vm_id, session_id)
    _event(
        job_id,
        {
            "type": "agent_event",
            "event": {"type": "progress", "step_id": "Az elkülönített sandbox elindult"},
        },
    )
    return client, vm_id, session_id


def _text_from_execution(result):
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False, default=str)
    parts = []
    for key in ("stdout", "output", "result", "stderr", "error"):
        value = result.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, (dict, list)):
            parts.append(json.dumps(value, ensure_ascii=False, default=str))
        else:
            parts.append(str(value))
    if not parts:
        return json.dumps(result, ensure_ascii=False, default=str)
    return "\n".join(parts)


def _execution_failed(result):
    if not isinstance(result, dict):
        return False
    if result.get("success") is False:
        return True
    if result.get("status") in ("error", "failed", "failure"):
        return True
    for key in ("exit_code", "return_code", "returncode"):
        if result.get(key) not in (None, 0):
            return True
    return bool(result.get("error") or result.get("error_message"))


def _run_command(client, command):
    result = client.execute(command, language="bash", timeout=3600)
    if _execution_failed(result):
        raise RuntimeError(_text_from_execution(result))
    return {"result": result, "output": _text_from_execution(result)}


def _remote_output(client, command, allow_empty=False):
    result = client.execute(command, language="bash", timeout=3600)
    if _execution_failed(result):
        raise RuntimeError(_text_from_execution(result))
    output = result.get("stdout") or result.get("output") or result.get("result") or ""
    if isinstance(output, dict):
        output = output.get("stdout") or output.get("output") or ""
    if not output and not allow_empty:
        raise RuntimeError(_text_from_execution(result))
    return str(output).strip()


def _publish_file(job, client, args):
    path = str(args["path"])
    name = str(args["name"])
    encoded = _remote_output(
        client,
        "python3 -c "
        + shlex.quote(
            "import base64,pathlib,sys;sys.stdout.write(base64.b64encode(pathlib.Path(sys.argv[1]).read_bytes()).decode())"
        )
        + " "
        + shlex.quote(path),
        allow_empty=True,
    )
    content = base64.b64decode(encoded, validate=True)
    artifact_id = str(uuid.uuid4())
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    storage.create_artifact(
        artifact_id,
        job["conversation_id"],
        job["job_id"],
        name,
        content_type,
        content,
    )
    artifact = {
        "artifact_id": artifact_id,
        "name": name,
        "content_type": content_type,
        "size": len(content),
    }
    _event(job["job_id"], {"type": "agent_event", "event": {"type": "artifact", "artifact": artifact}})
    return artifact


def _publish_site(job, client, vm_id, args):
    directory = str(args["directory"])
    port = int(args["port"])
    if port < 1 or port > 65535:
        raise ValueError("A port értéke 1 és 65535 közé essen")
    launch = (
        "nohup python3 -m http.server "
        + str(port)
        + " --bind 0.0.0.0 --directory "
        + shlex.quote(directory)
        + " >/tmp/instavm-site-"
        + str(port)
        + ".log 2>&1 </dev/null &"
    )
    _run_command(client, launch)
    time.sleep(1)
    share = client.shares.create(port=port, vm_id=vm_id, is_public=True)
    url = share.get("url") or share.get("public_url") or share.get("share_url")
    if not url:
        raise RuntimeError("Az InstaVM nem adott vissza nyilvános webhely-URL-t")
    artifact_id = str(uuid.uuid4())
    content = json.dumps({"url": url, "share_id": share.get("share_id")}).encode()
    storage.create_artifact(
        artifact_id,
        job["conversation_id"],
        job["job_id"],
        str(args["name"]),
        "application/vnd.instavm.site-link+json",
        content,
    )
    artifact = {
        "artifact_id": artifact_id,
        "name": str(args["name"]),
        "content_type": "text/html",
        "url": url,
    }
    _event(job["job_id"], {"type": "agent_event", "event": {"type": "artifact", "artifact": artifact}})
    return artifact


def _execute_tool(name, args, job, client, vm_id):
    if name == "sandbox_execute":
        return _run_command(client, str(args["command"]))
    if name == "sandbox_read_file":
        return _run_command(client, "cat -- " + shlex.quote(str(args["path"])))
    if name == "sandbox_write_file":
        path = str(args["path"])
        content = str(args["content"]).encode()
        parent = posixpath.dirname(path)
        if parent:
            _run_command(client, "mkdir -p -- " + shlex.quote(parent))
        with tempfile.NamedTemporaryFile(delete=False) as temporary:
            temporary.write(content)
            temporary_path = temporary.name
        try:
            result = client.upload_file(temporary_path, path)
            if _execution_failed(result):
                raise RuntimeError(_text_from_execution(result))
            return result
        finally:
            os.unlink(temporary_path)
    if name == "publish_file":
        return _publish_file(job, client, args)
    if name == "publish_site":
        return _publish_site(job, client, vm_id, args)
    raise ValueError(f"Ismeretlen eszköz: {name}")


def _new_model_client():
    api_key = os.environ.get("ABLITERATION_API_KEY")
    if not api_key:
        raise RuntimeError("ABLITERATION_API_KEY is not configured")
    return OpenAI(
        api_key=api_key,
        base_url="https://api.abliteration.ai/v1",
        timeout=3600,
        max_retries=2,
    )


def _append_redirects(job_id, agent_messages):
    redirects = storage.take_redirects(job_id)
    if not redirects:
        return False
    after_assistant = bool(agent_messages and agent_messages[-1].get("role") == "assistant")
    for index, item in enumerate(redirects):
        if after_assistant and index == 0:
            prefix = "The user redirected this work. Follow the latest instruction and continue without repeating completed output: "
        elif index == 0:
            prefix = "The user redirected this work. Follow the latest instruction: "
        else:
            prefix = "Additional instruction from the user: "
        if isinstance(item, list):
            content = [{"type": "text", "text": prefix}]
            content.extend(item)
        else:
            content = prefix + str(item)
        agent_messages.append({"role": "user", "content": content})
    storage.save_agent_messages(job_id, agent_messages)
    _progress(job_id, "A felhasználó frissítette a feladatot; az új irányt követem")
    return True


def _contains_image(value):
    if isinstance(value, dict):
        if value.get("type") in ("image", "image_url", "input_image") or "image_url" in value:
            return True
        return any(_contains_image(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_image(item) for item in value)
    return False


def _model_for_messages(agent_messages):
    if any(message.get("role") == "user" and _contains_image(message.get("content")) for message in agent_messages):
        return IMAGE_MODEL
    return MODEL


def _consume_stream(job, client, sandbox, vm_id, agent_messages, model):
    completion = client.chat.completions.create(
        model=model,
        temperature=1,
        top_p=1,
        messages=agent_messages,
        max_tokens=120000,
        reasoning_effort="max",
        web_search_options={"search_context_size": "medium"},
        tools=TOOLS,
        tool_choice="auto",
        stream=True,
        extra_body={"include_reasoning": True},
    )
    text = []
    reasoning = []
    tool_calls = {}
    finish_reason = None
    was_redirected = False
    try:
        for chunk in completion:
            if storage.is_cancel_requested(job["job_id"]):
                raise JobCancelled()
            if storage.has_redirects(job["job_id"]):
                was_redirected = True
                break
            choice = chunk.choices[0] if chunk.choices else None
            if not choice:
                continue
            delta = choice.delta
            content_delta = getattr(delta, "content", None)
            reasoning_delta = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if isinstance(content_delta, str) and content_delta:
                text.append(content_delta)
                _event(job["job_id"], {"type": "delta", "content": content_delta})
            if isinstance(reasoning_delta, str) and reasoning_delta:
                reasoning.append(reasoning_delta)
                _event(job["job_id"], {"type": "delta", "reasoning_content": reasoning_delta})
            for call_delta in getattr(delta, "tool_calls", None) or []:
                index = call_delta.index
                record = tool_calls.setdefault(
                    index,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if call_delta.id:
                    record["id"] += call_delta.id
                if call_delta.type:
                    record["type"] = call_delta.type
                function = call_delta.function
                if function and function.name:
                    record["function"]["name"] += function.name
                if function and function.arguments:
                    record["function"]["arguments"] += function.arguments
            if choice.finish_reason:
                finish_reason = choice.finish_reason
    finally:
        close = getattr(completion, "close", None)
        if close:
            close()
    return "".join(text), "".join(reasoning), [tool_calls[index] for index in sorted(tool_calls)], finish_reason, was_redirected


def _repair_dangling_tool_calls(job_id, agent_messages):
    changed = False
    repaired_events = []
    index = 0
    while index < len(agent_messages):
        message = agent_messages[index]
        tool_calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if not isinstance(tool_calls, list) or not tool_calls:
            index += 1
            continue
        expected_ids = {
            call.get("id")
            for call in tool_calls
            if isinstance(call, dict) and call.get("id")
        }
        cursor = index + 1
        completed_ids = set()
        while cursor < len(agent_messages) and agent_messages[cursor].get("role") == "tool":
            call_id = agent_messages[cursor].get("tool_call_id")
            if call_id in expected_ids:
                completed_ids.add(call_id)
            cursor += 1
        missing_calls = [
            call
            for call in tool_calls
            if isinstance(call, dict) and call.get("id") and call.get("id") not in completed_ids
        ]
        for call in missing_calls:
            name = (call.get("function") or {}).get("name", "tool")
            agent_messages.insert(
                cursor,
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(
                        {
                            "ok": False,
                            "error": (
                                "The server restarted before this tool result was saved. "
                                "The tool was not rerun because it may already have completed."
                            ),
                        },
                        ensure_ascii=False,
                    ),
                },
            )
            repaired_events.append((name, call["id"]))
            cursor += 1
            changed = True
        index = cursor
    if changed:
        storage.save_agent_messages(job_id, agent_messages)
        for name, _ in repaired_events:
            _event(
                job_id,
                {"type": "agent_event", "event": {"type": "tool_result", "tool": name, "ok": False}},
            )
    return changed


def _handle_tool_calls(job, client, sandbox, vm_id, agent_messages, assistant_text, reasoning, tool_calls):
    assistant_message = {"role": "assistant", "content": assistant_text or None, "tool_calls": tool_calls}
    agent_messages.append(assistant_message)
    if reasoning:
        assistant_message["reasoning_content"] = reasoning
    storage.save_agent_messages(job["job_id"], agent_messages)
    for index, tool_call in enumerate(tool_calls):
        if storage.is_cancel_requested(job["job_id"]):
            raise JobCancelled()
        if storage.has_redirects(job["job_id"]):
            skipped_calls = tool_calls[index:]
            for skipped_call in skipped_calls:
                agent_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": skipped_call["id"],
                        "content": json.dumps(
                            {
                                "ok": False,
                                "error": "The user redirected the task before this tool ran; it was skipped.",
                            },
                            ensure_ascii=False,
                        ),
                    }
                )
                _event(
                    job["job_id"],
                    {
                        "type": "agent_event",
                        "event": {
                            "type": "tool_result",
                            "tool": (skipped_call.get("function") or {}).get("name", "tool"),
                            "ok": False,
                        },
                    },
                )
            storage.save_agent_messages(job["job_id"], agent_messages)
            return
        name = tool_call["function"]["name"]
        call_id = tool_call["id"]
        _event(
            job["job_id"],
            {"type": "agent_event", "event": {"type": "tool_start", "tool": name}},
        )
        try:
            args = json.loads(tool_call["function"]["arguments"] or "{}")
            result = _execute_tool(name, args, job, sandbox, vm_id)
            tool_result = {"ok": True, "result": result}
            _event(
                job["job_id"],
                {"type": "agent_event", "event": {"type": "tool_result", "tool": name, "ok": True}},
            )
        except JobCancelled:
            raise
        except Exception as error:
            tool_result = {"ok": False, "error": str(error)}
            _event(
                job["job_id"],
                {"type": "agent_event", "event": {"type": "tool_result", "tool": name, "ok": False}},
            )
        agent_messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(tool_result, ensure_ascii=False, default=str),
            }
        )
        storage.save_agent_messages(job["job_id"], agent_messages)


def _run_job(job_id):
    try:
        job = storage.get_job_and_conversation(job_id)
        if not job or job["status"] != "running":
            return
        if storage.is_cancel_requested(job_id):
            raise JobCancelled()
        agent_messages = job["agent_messages"] or []
        if not agent_messages:
            agent_messages = [{"role": "system", "content": SYSTEM_MESSAGE}]
            agent_messages.extend(
                item
                for item in (job["conversation_messages"] or [])
                if isinstance(item, dict) and item.get("role") in ("user", "assistant")
            )
        recovered_tool_calls = _repair_dangling_tool_calls(job_id, agent_messages)
        if job["response_content"] and not recovered_tool_calls:
            if not agent_messages or agent_messages[-1].get("role") != "assistant":
                agent_messages.append({"role": "assistant", "content": job["response_content"]})
                agent_messages.append(
                    {
                        "role": "user",
                        "content": "Continue the unfinished work from the previous response without repeating completed content.",
                    }
                )
        storage.save_agent_messages(job_id, agent_messages)
        _progress(job_id, "Work-feladat indítása")
        sandbox, vm_id, session_id = _ensure_sandbox(
            job["conversation_id"], job["vm_id"], job["vm_session_id"], job_id
        )
        if storage.is_cancel_requested(job_id):
            raise JobCancelled()
        model_client = _new_model_client()
        last_model = None
        while True:
            if storage.is_cancel_requested(job_id):
                raise JobCancelled()
            if _append_redirects(job_id, agent_messages):
                continue
            model = _model_for_messages(agent_messages)
            if model != last_model:
                _event(
                    job_id,
                    {
                        "type": "agent_event",
                        "event": {"type": "model_start", "model": model},
                    },
                )
                last_model = model
            text, reasoning, tool_calls, finish_reason, redirected = _consume_stream(
                job, model_client, sandbox, vm_id, agent_messages, model
            )
            if redirected or storage.has_redirects(job_id):
                if text or reasoning:
                    interrupted_message = {"role": "assistant", "content": text or None}
                    if reasoning:
                        interrupted_message["reasoning_content"] = reasoning
                    agent_messages.append(interrupted_message)
                storage.save_agent_messages(job_id, agent_messages)
                _append_redirects(job_id, agent_messages)
                continue
            if tool_calls:
                _handle_tool_calls(
                    job,
                    model_client,
                    sandbox,
                    vm_id,
                    agent_messages,
                    text,
                    reasoning,
                    tool_calls,
                )
                continue
            if text or reasoning:
                if text:
                    agent_messages.append({"role": "assistant", "content": text})
                if reasoning:
                    _event(job_id, {"type": "agent_event", "event": {"type": "model_output"}})
                storage.save_agent_messages(job_id, agent_messages)
                break
            if finish_reason == "stop":
                break
            raise RuntimeError("A modell üres választ adott")
        status = "cancelled" if storage.is_cancel_requested(job_id) else "completed"
        if status == "completed":
            _event(job_id, {"type": "done"})
        storage.finish_job(job_id, status)
    except JobCancelled:
        _event(job_id, {"type": "done", "cancelled": True})
        storage.finish_job(job_id, "cancelled")
    except Exception as error:
        message = str(error) or type(error).__name__
        _event(job_id, {"type": "error", "message": message})
        storage.finish_job(job_id, "failed", message)
    finally:
        with _running_lock:
            _running_jobs.discard(job_id)