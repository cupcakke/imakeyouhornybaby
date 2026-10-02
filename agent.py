Send back the complete code with all the fixes. Fix each of the listed errors one by one, making sure to actually correct them so that there are 0 errors remaining. Keep the original imports, since the files exist. Write out every single character; do not abbreviate anything. Fix every error. There must be exactly one file. Do not write anything else; just output the complete code, and it must not contain any comments. Never, under any circumstances, use simplified, substitute, dummy, simulated, or fake code. Write the entire file as complete, unabridged, production-ready code in a single code block. It must be 100% error-free, a complete, error-free file, and must be submitted as a downloadable file. These requirements are mandatory and must be strictly adhered to. If no list of errors is provided, you must find all the errors and fix them. If there were comments in the original code, delete them. And most importantly: YOU MUST NEVER SIMPLIFY! 



The query points to a core systems problem: an LLM agent is not merely a model with a larger prompt, but a harness-mediated dynamical system operating over an effectively unbounded trajectory.

A useful abstraction is:

sₜ₊₁ = E(sₜ, aₜ, oₜ)
aₜ ~ πθ(Cₜ, sₜ)
Cₜ = H(hₜ, sₜ)

where sₜ is external/environmental state, aₜ is a model action or tool call, oₜ is the resulting observation, hₜ is interaction history, H is the harness/context policy, and Cₜ is the bounded context actually supplied to the model. The raw trajectory grows as actions and observations accumulate, while the model’s context window remains finite. This is explicitly identified as a central long-horizon-agent constraint: retaining the complete history eventually becomes impractical because context is finite and inference cost increases with input length. [Adapting Context Compression for Long-Horizon Agents with Counterfactual Continuations](https://arxiv.org/html/2609.36526 "arxiv.org/html/2609.36526")

“State-space infinity” should therefore be understood in two related senses:

1. The environment state can be effectively unbounded. Files, databases, websites, processes, browser sessions, generated artifacts, and external tool state can continue to change. Even if each individual variable is finite, their combinations and the number of possible trajectories are not practically enumerable.

2. The agent’s history is unbounded. A ReAct-style loop repeatedly appends reasoning, actions, tool results, and observations. The resulting trajectory can outgrow any fixed context window, so the full state cannot remain directly visible to the model. [Language Model "Shape" | Alex L. Zhang](https://alexzhang13.github.io/blog/2026/shape/ "alexzhang13.github.io/blog/2026/shape")[Adapting Context Compression for Long-Horizon Agents with Counterfactual Continuations](https://arxiv.org/html/2609.36526 "arxiv.org/html/2609.36526")

The harness is the mechanism that makes this unbounded process executable. It mediates between model and environment, constructs the next model input, maintains auxiliary state, and can transform or reject actions. A formal harness can therefore be viewed as a stateful transducer rather than as a prompt template. [Harness Evolution as Learning: Approximation, Generalization, and Optimization Limits of Self-Improving Personal Agents](https://arxiv.org/html/2609.36892 "arxiv.org/html/2609.36892") In practical loop engineering, the harness commonly includes persistent state, machine-checkable stop conditions, verifiers, token budgets, and escalation or recovery logic. [Loop Engineering: Building Blocks, Adoption, and Impact (Supplementary Material)](https://exa.ai/library/publication/vttz6ng0dfv "exa.ai/library/publication/vttz6ng0dfv")

The key distinction is between operational state and model-visible context:

• Operational state: the full external state required to continue execution, including files, tool outputs, checkpoints, structured task state, and persistent memory.
• Model-visible context: a bounded projection of that state at the current step.
• Harness policy: the procedure deciding what to retain, summarize, retrieve, compress, delegate, or expose next.

An “infinite-context agent” is therefore not literally a model with an infinite context window. It is a finite-context model coupled to an unbounded external state store and a context-management policy. Candidate implementations include:

• Lossy compaction: replace older history with summaries. This is simple but can discard details needed for later decisions. [Language Model "Shape" | Alex L. Zhang](https://alexzhang13.github.io/blog/2026/shape/ "alexzhang13.github.io/blog/2026/shape")[Adapting Context Compression for Long-Horizon Agents with Counterfactual Continuations](https://arxiv.org/html/2609.36526 "arxiv.org/html/2609.36526")
• Retrieval-based memory: store history externally and retrieve relevant fragments on demand.
• Structured belief state: maintain explicit entities, relations, unresolved requirements, evidence, and progress rather than relying on raw transcript retention. Recent work frames this as continual belief maintenance with consistency and stagnation checks. [Beyond Memory: Harnessing Long-Horizon Agents with Explicit Belief States](https://arxiv.org/html/2610.01415 "arxiv.org/html/2610.01415")
• File-backed editable context: let the model maintain context as an externally stored file, including unrestricted updates and multiple contexts for multi-agent systems. [Context Language Models](https://arxiv.org/html/2609.37725 "arxiv.org/html/2609.37725")
• Tail-recursive delegation: when the context approaches its limit, delegate continuation to another agent while passing prior history by reference plus a progress summary. [Harness as a Language: A Minimalist Agent Framework With Maximal Expressivity](https://arxiv.org/html/2609.26891 "arxiv.org/html/2609.26891")
• Learned compaction: train the agent to decide when to compact, what state to preserve, and how to continue after compaction. AutoCompact reports improvements on coding benchmarks while keeping execution within fixed context budgets. [AutoCompact: Learning When to CompactContext in Long-Horizon Coding Agents](https://arxiv.org/html/2610.02163 "arxiv.org/html/2610.02163")
• Adaptive action-space management: expose only a task-conditioned subset of a much larger, changing tool/action space, refreshing or revising it from execution feedback. [AnyAct: Universal Action for Self-Evolving Agents](https://arxiv.org/html/2609.37025 "arxiv.org/html/2609.37025")

The central failure mode is not context overflow alone. It is state aliasing: two materially different world states can produce the same compressed context, causing the model to choose the same action despite different required behavior. Any compression scheme is safe only to the extent that it preserves distinctions relevant to future decisions. A summary that is linguistically plausible but omits a constraint, failed attempt, file mutation, credential-free session state, or unresolved dependency can make continuation incorrect while appearing coherent.

A robust agent-loop harness should therefore maintain an explicit continuation state with at least:

• current objective and acceptance conditions;
• completed, pending, and blocked steps;
• authoritative artifacts and their locations;
• current environment facts and evidence supporting them;
• actions already attempted and their outcomes;
• unresolved questions and dependencies;
• recovery/checkpoint information;
• a bounded recent interaction window;
• searchable long-term records for older details.

The harness should compact based on semantic state transitions, not merely token count. Before compaction, it should extract durable facts, preserve exact references to authoritative artifacts, record failed approaches, identify open obligations, and verify that the post-compaction context can reproduce the next required decision. The loop should also detect stagnation, repeated actions, contradictory state, tool failure, and progress loss; otherwise an unbounded state space permits infinite looping rather than useful autonomy.

The practical conclusion is: context-window scaling helps, but does not solve the problem. Larger windows delay truncation; they do not eliminate trajectory growth, retrieval requirements, stale observations, cost, or the need to distinguish relevant from irrelevant history. The durable architecture is a bounded-context policy operating over persistent external state, with explicit checkpoints, structured belief or task state, selective retrieval, compaction, delegation, and verifiable continuation. Recent harness research characterizes the same direction: the harness—not only the base model—determines the effective policy and can be revised or learned from execution feedback. [Harness Evolution as Learning: Approximation, Generalization, and Optimization Limits of Self-Improving Personal Agents


                                                              
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
from urllib.parse import urlsplit

from instavm import InstaVM
from openai import OpenAI

import storage


MODEL = "abliterated-model-large-v2"
IMAGE_MODEL = "abliterated-model"
SYSTEM_MESSAGE = """You are a capable digital work agent operating in this conversation's isolated, cloud-hosted InstaVM Linux sandbox. You can run shell commands, read and write files, and operate an isolated cloud browser that can navigate pages, inspect content, click, type, fill forms, scroll, and capture screenshots.

For every new request, first inspect the sandbox with sandbox_execute using a harmless command such as `pwd && ls -la`. Next, create a concise ordered checklist with update_checklist before doing substantive work. Keep it current as steps start, finish, or become blocked. For complex tasks, work through the checklist one step at a time. Use the browser tools for websites and browser-based graphical interfaces; use shell and file tools for code and local files. For code or multi-step workflows, run the actual code/process, inspect errors and output, fix failures, and rerun the relevant checks before presenting the result. Do not substitute instructions or a hypothetical description for requested work. Publish requested Sites so they have a working URL. Preserve useful intermediate files in the sandbox, and use the artifact tool for every finished document, spreadsheet, presentation, report, or other downloadable file. Never claim an operation succeeded unless its tool result confirms it. The user may redirect you during a task; follow the latest instruction before starting further work. Reply in the user's language."""


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
    {
        "type": "function",
        "function": {
            "name": "update_checklist",
            "description": "Create or update the concise execution checklist for the current task. Send the full current list each time so completed and blocked steps stay accurate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "title": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed", "blocked"],
                                },
                            },
                            "required": ["id", "title", "status"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["steps"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": "Open an HTTP or HTTPS page in this conversation's isolated cloud browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "wait_timeout": {"type": "integer"},
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_inspect",
            "description": "Read the current page's clean text, interactive controls, and selector anchors to decide what to do next.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "Click a page element using a CSS selector from the current browser page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "force": {"type": "boolean"},
                },
                "required": ["selector"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_fill",
            "description": "Replace the value of a form field selected by CSS selector.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["selector", "value"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_type",
            "description": "Type text into an element selected by CSS selector without replacing existing content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["selector", "text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_scroll",
            "description": "Scroll the current page, or a selected scrollable element.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
                "required": ["y"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_wait",
            "description": "Wait for the page to load or for a selected element to appear.",
            "parameters": {
                "type": "object",
                "properties": {
                    "condition": {
                        "type": "string",
                        "enum": ["load", "domcontentloaded", "networkidle", "selector"],
                    },
                    "selector": {"type": "string"},
                },
                "required": ["condition"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": "Capture the current browser page as a downloadable PNG artifact for visual review.",
            "parameters": {
                "type": "object",
                "properties": {"full_page": {"type": "boolean"}},
                "additionalProperties": False,
            },
        },
    },
]


_running_jobs = set()
_running_lock = threading.Lock()
_browser_sessions = {}
_browser_sessions_lock = threading.Lock()


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


def _ensure_browser_session(job, client):
    session_id = getattr(client, "_work_browser_session_id", None)
    if session_id:
        return session_id

    conversation_id = job["conversation_id"]
    with _browser_sessions_lock:
        session_id = _browser_sessions.get(conversation_id)
        if session_id:
            try:
                info = client.get_browser_session(session_id)
                if isinstance(info, dict):
                    details = info.get("session") if isinstance(info.get("session"), dict) else info
                    status = str(details.get("status") or details.get("state") or "").strip().lower()
                    if status in {"closed", "deleted", "expired", "failed", "killed"}:
                        session_id = None
            except Exception as error:
                message = str(error).lower()
                if "not found" in message or "expired" in message:
                    session_id = None
                else:
                    raise

        if not session_id:
            session_id = client.create_browser_session()
            if not session_id:
                raise RuntimeError("Az InstaVM nem adott vissza böngésző-session azonosítót")
            _browser_sessions[conversation_id] = session_id

    client._work_browser_session_id = session_id
    return session_id


def _update_checklist(job, args):
    steps = args.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
        raise ValueError("Az ellenőrzőlista 1–8 lépésből álljon")
    normalized = []
    seen_ids = set()
    allowed_statuses = {"pending", "in_progress", "completed", "blocked"}
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("Minden ellenőrzőlista-lépés objektum legyen")
        step_id = str(step.get("id") or "").strip()
        title = str(step.get("title") or "").strip()
        status = str(step.get("status") or "").strip()
        if not step_id or step_id in seen_ids or not title or status not in allowed_statuses:
            raise ValueError("Az ellenőrzőlista-lépés azonosítója, címe vagy állapota érvénytelen")
        seen_ids.add(step_id)
        normalized.append({"id": step_id[:80], "title": title[:200], "status": status})
    _event(
        job["job_id"],
        {
            "type": "agent_event",
            "progress": "Ellenőrzőlista frissítve",
            "event": {"type": "checklist", "steps": normalized},
        },
    )
    return {"steps": normalized}


def _browser_inspect(client, session_id):
    result = client.browser_extract_content(
        session_id=session_id,
        include_interactive=True,
        include_anchors=True,
        max_anchors=40,
    )
    if not isinstance(result, dict):
        return result
    output = {
        key: result[key]
        for key in ("url", "title", "readable_content")
        if key in result
    }
    readable = output.get("readable_content")
    if isinstance(readable, dict) and isinstance(readable.get("content"), str):
        readable = dict(readable)
        readable["content"] = readable["content"][:30000]
        output["readable_content"] = readable
    for key in ("interactive_elements", "content_anchors"):
        if key in result:
            output[key] = result[key][:40] if isinstance(result[key], list) else result[key]
    return output


def _browser_screenshot(job, client, session_id, full_page):
    encoded = client.browser_screenshot(session_id=session_id, full_page=full_page, format="png")
    if not isinstance(encoded, str) or not encoded:
        raise RuntimeError("A böngésző nem adott vissza képernyőképet")
    if encoded.startswith("data:") and "," in encoded:
        encoded = encoded.split(",", 1)[1]
    image = base64.b64decode(encoded, validate=True)
    artifact_id = str(uuid.uuid4())
    name = "browser-screenshot.png"
    storage.create_artifact(
        artifact_id,
        job["conversation_id"],
        job["job_id"],
        name,
        "image/png",
        image,
    )
    artifact = {
        "artifact_id": artifact_id,
        "name": name,
        "content_type": "image/png",
        "size": len(image),
    }
    _event(job["job_id"], {"type": "agent_event", "event": {"type": "artifact", "artifact": artifact}})
    return artifact


def _execute_browser_tool(name, args, job, client):
    session_id = _ensure_browser_session(job, client)
    if name == "browser_navigate":
        url = str(args["url"]).strip()
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("A böngésző csak érvényes HTTP- vagy HTTPS-URL-t nyit meg")
        return client.browser_navigate(
            url=url,
            session_id=session_id,
            wait_timeout=max(1000, min(int(args.get("wait_timeout", 30000)), 60000)),
        )
    if name == "browser_inspect":
        return _browser_inspect(client, session_id)
    if name == "browser_click":
        return client.browser_click(
            selector=str(args["selector"]),
            session_id=session_id,
            force=bool(args.get("force", False)),
        )
    if name == "browser_fill":
        return client.browser_fill(
            selector=str(args["selector"]),
            value=str(args["value"]),
            session_id=session_id,
        )
    if name == "browser_type":
        return client.browser_type(
            selector=str(args["selector"]),
            text=str(args["text"]),
            session_id=session_id,
        )
    if name == "browser_scroll":
        return client.browser_scroll(
            session_id=session_id,
            selector=args.get("selector"),
            x=args.get("x"),
            y=int(args["y"]),
        )
    if name == "browser_wait":
        condition = str(args["condition"])
        selector = args.get("selector")
        if condition == "selector" and not selector:
            raise ValueError("A selector feltételhez CSS-szelektor is szükséges")
        return client.browser_wait(
            condition=condition,
            session_id=session_id,
            selector=selector,
        )
    if name == "browser_screenshot":
        return _browser_screenshot(job, client, session_id, bool(args.get("full_page", True)))
    raise ValueError(f"Ismeretlen böngészőeszköz: {name}")


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
    if name == "update_checklist":
        return _update_checklist(job, args)
    if name.startswith("browser_"):
        return _execute_browser_tool(name, args, job, client)
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


def _consume_stream(
    job,
    client,
    sandbox,
    vm_id,
    agent_messages,
    model,
    require_shell=False,
    require_checklist=False,
):
    completion = client.chat.completions.create(
        model=model,
        temperature=1,
        top_p=1,
        messages=agent_messages,
        max_tokens=120000,
        reasoning_effort="max",
        web_search_options={"search_context_size": "medium"},
        tools=TOOLS,
        tool_choice=(
            {"type": "function", "function": {"name": "sandbox_execute"}}
            if require_shell
            else (
                {"type": "function", "function": {"name": "update_checklist"}}
                if require_checklist
                else "auto"
            )
        ),
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
        require_shell = True
        require_checklist = False
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
                job,
                model_client,
                sandbox,
                vm_id,
                agent_messages,
                model,
                require_shell=require_shell,
                require_checklist=require_checklist,
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
            if require_shell and not any(
                (call.get("function") or {}).get("name") == "sandbox_execute"
                for call in tool_calls
                if isinstance(call, dict)
            ):
                raise RuntimeError("A modell nem indította el a kötelező shell-ellenőrzést.")
            if require_checklist and not any(
                (call.get("function") or {}).get("name") == "update_checklist"
                for call in tool_calls
                if isinstance(call, dict)
            ):
                raise RuntimeError("A modell nem hozta létre a kötelező feladat-ellenőrzőlistát.")
            if tool_calls:
                if require_shell:
                    require_shell = False
                    require_checklist = True
                elif require_checklist:
                    require_checklist = False
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