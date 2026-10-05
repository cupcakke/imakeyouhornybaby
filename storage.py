import json
import os
import threading

from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import event_bus


_pool = None
_pool_lock = threading.RLock()
_listener_thread = None
_listener_stop = threading.Event()
_listener_ready = threading.Event()
_job_controls = {}
_job_controls_lock = threading.RLock()


class _JobControl:
    def __init__(self):
        self.cancel_event = threading.Event()
        self.redirect_event = threading.Event()
        self.lock = threading.RLock()


def _job_control(job_id):
    job_id = str(job_id)
    with _job_controls_lock:
        control = _job_controls.get(job_id)
        if control is None:
            control = _JobControl()
            _job_controls[job_id] = control
        return control


def _apply_control_notification(connection, payload):
    try:
        notification = json.loads(payload)
        job_id = str(notification["job_id"])
        action = notification["action"]
    except (TypeError, ValueError, KeyError):
        return
    control = _job_control(job_id)
    with control.lock:
        if action == "cancel":
            control.cancel_event.set()
        elif action == "redirect":
            row = connection.execute(
                "SELECT redirects FROM ai_chat_jobs WHERE job_id = %s",
                (job_id,),
            ).fetchone()
            if row and row["redirects"]:
                control.redirect_event.set()
            else:
                control.redirect_event.clear()


def _notification_listener():
    while not _listener_stop.is_set():
        pool = _pool
        if pool is None:
            return
        try:
            with pool.connection(timeout=5) as connection:
                connection.autocommit = True
                connection.execute("LISTEN ai_chat_events")
                connection.execute("LISTEN ai_chat_control")
                _listener_ready.set()
                event_bus.publish_all()
                while not _listener_stop.is_set():
                    notification = next(
                        connection.notifies(timeout=1, stop_after=1),
                        None,
                    )
                    if notification is None:
                        continue
                    if notification.channel == "ai_chat_events":
                        try:
                            data = json.loads(notification.payload)
                            event_bus.publish(data["job_id"], data["event_id"])
                        except (TypeError, ValueError, KeyError):
                            event_bus.publish_all()
                    elif notification.channel == "ai_chat_control":
                        _apply_control_notification(connection, notification.payload)
        except Exception:
            if not _listener_stop.is_set():
                event_bus.publish_all()
                _listener_stop.wait(1)


def open_pool():
    global _pool, _listener_thread
    with _pool_lock:
        if _pool is not None:
            return
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL is not configured")
        try:
            max_size = int(os.environ.get("DB_POOL_MAX_SIZE", "8"))
        except ValueError as error:
            raise RuntimeError("DB_POOL_MAX_SIZE must be an integer") from error
        if max_size < 2:
            raise RuntimeError("DB_POOL_MAX_SIZE must be at least 2")
        pool = ConnectionPool(
            conninfo=database_url,
            min_size=1,
            max_size=max_size,
            timeout=10,
            max_waiting=100,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        try:
            pool.open(wait=True, timeout=15)
        except Exception:
            pool.close()
            raise
        _pool = pool
        _listener_stop.clear()
        _listener_ready.clear()
        _listener_thread = threading.Thread(
            target=_notification_listener,
            name="postgres-notification-listener",
            daemon=True,
        )
        _listener_thread.start()
        if not _listener_ready.wait(timeout=15):
            _listener_stop.set()
            _listener_thread.join(timeout=5)
            pool.close(timeout=5)
            _pool = None
            _listener_thread = None
            raise RuntimeError("PostgreSQL notification listener did not become ready")


def close_pool():
    global _pool, _listener_thread
    with _pool_lock:
        pool = _pool
        listener = _listener_thread
        if pool is None:
            return
        _listener_stop.set()
    if listener is not None:
        listener.join(timeout=5)
    pool.close(timeout=5)
    with _pool_lock:
        if _pool is pool:
            _pool = None
            _listener_thread = None


def connect():
    if _pool is None:
        open_pool()
    return _pool.connection(timeout=10)


def upsert_conversation(conversation_id, messages, title):
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO ai_chat_conversations (conversation_id, title, messages)
            VALUES (%s, %s, %s)
            ON CONFLICT (conversation_id) DO UPDATE
            SET title = EXCLUDED.title,
                messages = EXCLUDED.messages,
                updated_at = NOW()
            """,
            (conversation_id, title, Jsonb(messages)),
        )


def create_job(job_id, conversation_id, agent_messages):
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO ai_chat_jobs (job_id, conversation_id, status, agent_messages)
            VALUES (%s, %s, 'running', %s)
            """,
            (job_id, conversation_id, Jsonb(agent_messages)),
        )
        connection.execute(
            """
            UPDATE ai_chat_conversations
            SET active_job_id = %s, updated_at = NOW()
            WHERE conversation_id = %s
            """,
            (job_id, conversation_id),
        )
    _job_control(job_id)


def get_job(job_id):
    with connect() as connection:
        return connection.execute(
            "SELECT * FROM ai_chat_jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()


def get_job_and_conversation(job_id):
    with connect() as connection:
        return connection.execute(
            """
            SELECT j.*, c.vm_id, c.vm_session_id, c.messages AS conversation_messages
            FROM ai_chat_jobs j
            JOIN ai_chat_conversations c USING (conversation_id)
            WHERE j.job_id = %s
            """,
            (job_id,),
        ).fetchone()


def save_agent_messages(job_id, agent_messages):
    with connect() as connection:
        connection.execute(
            """
            UPDATE ai_chat_jobs
            SET agent_messages = %s, updated_at = NOW()
            WHERE job_id = %s
            """,
            (Jsonb(agent_messages), job_id),
        )


def append_event(job_id, payload):
    content = payload.get("content") if payload.get("type") == "delta" else None
    reasoning = payload.get("reasoning_content") if payload.get("type") == "delta" else None
    artifact = payload.get("event", {}).get("artifact") if payload.get("type") == "agent_event" else None
    artifact_link = None
    if artifact and artifact.get("artifact_id"):
        name = str(artifact.get("name") or "Letöltés").replace("[", "").replace("]", "")
        artifact_link = f"\n\n[{name}](/api/artifacts/{artifact['artifact_id']})"
    with connect() as connection:
        row = connection.execute(
            """
            INSERT INTO ai_chat_events (job_id, payload)
            VALUES (%s, %s)
            RETURNING event_id
            """,
            (job_id, Jsonb(payload)),
        ).fetchone()
        connection.execute(
            """
            UPDATE ai_chat_jobs
            SET response_content = response_content || %s || %s,
                response_reasoning = response_reasoning || %s,
                progress = COALESCE(%s, progress),
                updated_at = NOW()
            WHERE job_id = %s
            """,
            (
                content or "",
                artifact_link or "",
                reasoning or "",
                payload.get("progress"),
                job_id,
            ),
        )
        event_id = row["event_id"]
        connection.execute(
            "SELECT pg_notify(%s, %s)",
            (
                "ai_chat_events",
                json.dumps(
                    {"job_id": str(job_id), "event_id": event_id},
                    separators=(",", ":"),
                ),
            ),
        )
    return event_id


def list_events(job_id, after=0):
    with connect() as connection:
        return connection.execute(
            """
            SELECT event_id, payload
            FROM ai_chat_events
            WHERE job_id = %s AND event_id > %s
            ORDER BY event_id
            """,
            (job_id, after),
        ).fetchall()


def latest_event_id(job_id):
    with connect() as connection:
        row = connection.execute(
            "SELECT COALESCE(MAX(event_id), 0) AS event_id FROM ai_chat_events WHERE job_id = %s",
            (job_id,),
        ).fetchone()
        return row["event_id"]


def request_cancel(job_id):
    with _job_controls_lock:
        control = _job_controls.get(str(job_id))
    with connect() as connection:
        row = connection.execute(
            """
            UPDATE ai_chat_jobs
            SET cancel_requested = TRUE, updated_at = NOW()
            WHERE job_id = %s AND status IN ('queued', 'running')
            RETURNING job_id
            """,
            (job_id,),
        ).fetchone()
        if row is not None:
            connection.execute(
                "SELECT pg_notify(%s, %s)",
                (
                    "ai_chat_control",
                    json.dumps(
                        {"job_id": str(job_id), "action": "cancel"},
                        separators=(",", ":"),
                    ),
                ),
            )
    if row is not None:
        if control is None:
            control = _job_control(job_id)
        control.cancel_event.set()
    return row is not None


def is_cancel_requested(job_id):
    control = _job_controls.get(str(job_id))
    if control is not None:
        return control.cancel_event.is_set()
    with connect() as connection:
        row = connection.execute(
            "SELECT cancel_requested FROM ai_chat_jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()
    requested = bool(row and row["cancel_requested"])
    if requested:
        _job_control(job_id).cancel_event.set()
    return requested


def initialize_job_state(job_id, cancel_requested=False, has_redirects=False):
    control = _job_control(job_id)
    if cancel_requested:
        control.cancel_event.set()
    if has_redirects:
        control.redirect_event.set()


def cancel_event(job_id):
    return _job_control(job_id).cancel_event


def redirect_event(job_id):
    return _job_control(job_id).redirect_event


def _notify_control(connection, job_id, action):
    connection.execute(
        "SELECT pg_notify(%s, %s)",
        (
            "ai_chat_control",
            json.dumps(
                {"job_id": str(job_id), "action": action},
                separators=(",", ":"),
            ),
        ),
    )


def add_redirect(job_id, message):
    message_content = message if isinstance(message, list) else message.strip()
    with _job_controls_lock:
        control = _job_controls.get(str(job_id))
        created_control = control is None
        if control is None:
            control = _JobControl()
            _job_controls[str(job_id)] = control
    with control.lock:
        with connect() as connection:
            row = connection.execute(
                """
                UPDATE ai_chat_jobs
                SET redirects = redirects || %s::jsonb, updated_at = NOW()
                WHERE job_id = %s AND status = 'running'
                RETURNING conversation_id
                """,
                (Jsonb([message_content]), job_id),
            ).fetchone()
            if not row:
                if created_control:
                    with _job_controls_lock:
                        if _job_controls.get(str(job_id)) is control:
                            _job_controls.pop(str(job_id), None)
                return None
            connection.execute(
                """
                UPDATE ai_chat_conversations
                SET messages = messages || %s::jsonb, updated_at = NOW()
                WHERE conversation_id = %s
                """,
                (Jsonb([{"role": "user", "content": message_content}]), row["conversation_id"]),
            )
            _notify_control(connection, job_id, "redirect")
        control.redirect_event.set()
        return row["conversation_id"]


def take_redirects(job_id):
    control = _job_control(job_id)
    with control.lock:
        with connect() as connection:
            row = connection.execute(
                """
                SELECT redirects
                FROM ai_chat_jobs
                WHERE job_id = %s
                FOR UPDATE
                """,
                (job_id,),
            ).fetchone()
            if not row:
                redirects = []
            else:
                redirects = row["redirects"] or []
                if redirects:
                    connection.execute(
                        """
                        UPDATE ai_chat_jobs
                        SET redirects = '[]'::jsonb, updated_at = NOW()
                        WHERE job_id = %s
                        """,
                        (job_id,),
                    )
        control.redirect_event.clear()
        return redirects


def has_redirects(job_id):
    control = _job_controls.get(str(job_id))
    if control is not None:
        return control.redirect_event.is_set()
    with connect() as connection:
        row = connection.execute(
            "SELECT redirects FROM ai_chat_jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()
    redirected = bool(row and row["redirects"])
    if redirected:
        _job_control(job_id).redirect_event.set()
    return redirected


def finish_job(job_id, status, error=None):
    with connect() as connection:
        row = connection.execute(
            """
            UPDATE ai_chat_jobs
            SET status = %s, error = %s, updated_at = NOW()
            WHERE job_id = %s
            RETURNING conversation_id, response_content
            """,
            (status, error, job_id),
        ).fetchone()
        if not row:
            return
        conversation_id = row["conversation_id"]
        messages = connection.execute(
            "SELECT messages FROM ai_chat_conversations WHERE conversation_id = %s FOR UPDATE",
            (conversation_id,),
        ).fetchone()["messages"]
        assistant = {"role": "assistant", "content": row["response_content"]}
        if messages and messages[-1].get("role") == "assistant":
            messages[-1] = assistant
        else:
            messages.append(assistant)
        connection.execute(
            """
            UPDATE ai_chat_conversations
            SET messages = %s,
                active_job_id = CASE WHEN active_job_id = %s THEN NULL ELSE active_job_id END,
                updated_at = NOW()
            WHERE conversation_id = %s
            """,
            (Jsonb(messages), job_id, conversation_id),
        )
        connection.execute(
            "SELECT pg_notify(%s, %s)",
            (
                "ai_chat_events",
                json.dumps(
                    {"job_id": str(job_id), "event_id": 0},
                    separators=(",", ":"),
                ),
            ),
        )
    event_bus.publish(job_id, 0)
    with _job_controls_lock:
        _job_controls.pop(str(job_id), None)


def clear_job_state(job_id):
    with _job_controls_lock:
        _job_controls.pop(str(job_id), None)


def list_conversations(conversation_ids):
    if not conversation_ids:
        return []
    with connect() as connection:
        conversations = connection.execute(
            """
            SELECT conversation_id, title, messages, vm_id, active_job_id, updated_at
            FROM ai_chat_conversations
            WHERE conversation_id = ANY(%s)
            ORDER BY updated_at DESC
            """,
            (conversation_ids,),
        ).fetchall()
        output = []
        for conversation in conversations:
            job_id = conversation["active_job_id"]
            if job_id:
                job = connection.execute(
                    """
                    SELECT job_id, conversation_id, status, progress, response_content,
                           response_reasoning, error, updated_at
                    FROM ai_chat_jobs
                    WHERE job_id = %s
                    """,
                    (job_id,),
                ).fetchone()
            else:
                job = connection.execute(
                    """
                    SELECT job_id, conversation_id, status, progress, response_content,
                           response_reasoning, error, updated_at
                    FROM ai_chat_jobs
                    WHERE conversation_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (conversation["conversation_id"],),
                ).fetchone()
            if job:
                job["last_event_id"] = latest_event_id(job["job_id"])
            conversation["latest_job"] = job
            conversation["updated_at"] = conversation["updated_at"].isoformat()
            output.append(conversation)
        return output


def pending_jobs():
    with connect() as connection:
        return connection.execute(
            "SELECT job_id FROM ai_chat_jobs WHERE status = 'running' ORDER BY created_at"
        ).fetchall()


def create_artifact(artifact_id, conversation_id, job_id, name, content_type, content):
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO ai_chat_artifacts
                (artifact_id, conversation_id, job_id, name, content_type, content)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (artifact_id, conversation_id, job_id, name, content_type, content),
        )


def get_artifact(artifact_id):
    with connect() as connection:
        return connection.execute(
            """
            SELECT artifact_id, name, content_type, content
            FROM ai_chat_artifacts
            WHERE artifact_id = %s
            """,
            (artifact_id,),
        ).fetchone()


def update_sandbox(conversation_id, vm_id, session_id):
    with connect() as connection:
        connection.execute(
            """
            UPDATE ai_chat_conversations
            SET vm_id = %s, vm_session_id = %s, updated_at = NOW()
            WHERE conversation_id = %s
            """,
            (vm_id, session_id, conversation_id),
        )