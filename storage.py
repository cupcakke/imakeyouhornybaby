import asyncio
import atexit
import base64
import json
import logging
import math
import os
import random
import threading
import time
import weakref
from contextlib import contextmanager
from datetime import date as _date
from datetime import datetime as _datetime
from datetime import time as _time_of_day
from datetime import timedelta as _timedelta
from decimal import Decimal
from html import escape
from urllib.parse import quote
from uuid import UUID

import psycopg
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import event_bus


_log = logging.getLogger(__name__)

_pool = None
_pool_lock = threading.RLock()
_pool_condition = threading.Condition(_pool_lock)
_pool_closing = False
_pool_acquiring = 0
_vm_startup_process_lock = threading.Lock()
_VM_STARTUP_ADVISORY_LOCK_KEY = 0x494E535441564D

_listener_thread = None
_control_thread = None
_listener_stop = threading.Event()
_listener_ready = threading.Event()

_job_controls = weakref.WeakValueDictionary()
_job_controls_lock = threading.RLock()


class _EventBusSignal:
    __slots__ = ("job_id", "event_id")

    def __init__(self, job_id, event_id):
        self.job_id = None if job_id is None else str(job_id)
        self.event_id = event_id

    def __repr__(self):
        return (
            f"_EventBusSignal(job_id={self.job_id!r}, "
            f"event_id={self.event_id!r})"
        )


class _EventBusSubscriptionAdapter:
    _OWN_ATTRIBUTES = frozenset(
        ("subscription", "job_id", "queue", "_loop", "_lock", "_closed")
    )

    def __init__(self, subscription, job_id, loop):
        self.subscription = subscription
        self.job_id = str(job_id)
        self.queue = asyncio.Queue(maxsize=1)
        self._loop = loop
        self._lock = threading.Lock()
        self._closed = False

    def __getattr__(self, name):
        if name in _EventBusSubscriptionAdapter._OWN_ATTRIBUTES:
            raise AttributeError(name)
        return getattr(self.subscription, name)

    def close(self):
        with self._lock:
            self._closed = True

    def _enqueue(self, signal):
        with self._lock:
            if self._closed:
                return

        queue = self.queue

        if queue.full():
            try:
                pending = queue.get_nowait()
            except asyncio.QueueEmpty:
                pending = None
            else:
                queue.task_done()

            if pending is not None:
                previous = pending.event_id
                current = signal.event_id
                if type(previous) is int and type(current) is int:
                    merged = max(previous, current)
                else:
                    merged = current
                signal = _EventBusSignal(self.job_id, merged)

        try:
            queue.put_nowait(signal)
        except asyncio.QueueFull:
            return

    def deliver(self, event_id):
        signal = _EventBusSignal(self.job_id, event_id)

        with self._lock:
            if self._closed:
                return
            loop = self._loop

        if loop is None:
            self._enqueue(signal)
            return

        if loop.is_closed():
            return

        try:
            loop.call_soon_threadsafe(self._enqueue, signal)
        except RuntimeError:
            return


def _call_argument(args, kwargs, index, name):
    if len(args) > index:
        return args[index]
    return kwargs.get(name)


def _install_event_bus_queue_compatibility():
    if getattr(event_bus, "_ai_chat_queue_compat_installed", False):
        return

    original_subscribe = event_bus.subscribe
    original_unsubscribe = event_bus.unsubscribe
    original_publish = event_bus.publish
    original_publish_all = event_bus.publish_all

    adapters = weakref.WeakSet()
    adapters_lock = threading.Lock()

    def registered(job_id=None):
        with adapters_lock:
            if job_id is None:
                return tuple(adapters)
            key = str(job_id)
            return tuple(
                adapter for adapter in adapters if adapter.job_id == key
            )

    def subscribe(*args, **kwargs):
        subscription = original_subscribe(*args, **kwargs)

        if hasattr(subscription, "queue"):
            return subscription

        job_id = _call_argument(args, kwargs, 0, "job_id")
        if job_id is None:
            job_id = getattr(subscription, "job_id", None)

        if job_id is None:
            return subscription

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        adapter = _EventBusSubscriptionAdapter(subscription, job_id, loop)

        with adapters_lock:
            adapters.add(adapter)

        return adapter

    def unsubscribe(subscription):
        if isinstance(subscription, _EventBusSubscriptionAdapter):
            subscription.close()

            with adapters_lock:
                adapters.discard(subscription)

            return original_unsubscribe(subscription.subscription)

        return original_unsubscribe(subscription)

    def publish(*args, **kwargs):
        job_id = _call_argument(args, kwargs, 0, "job_id")
        event_id = _call_argument(args, kwargs, 1, "event_id")

        try:
            return original_publish(*args, **kwargs)
        finally:
            if job_id is not None:
                for adapter in registered(job_id):
                    adapter.deliver(event_id)

    def publish_all(*args, **kwargs):
        try:
            return original_publish_all(*args, **kwargs)
        finally:
            for adapter in registered():
                adapter.deliver(0)

    event_bus.subscribe = subscribe
    event_bus.unsubscribe = unsubscribe
    event_bus.publish = publish
    event_bus.publish_all = publish_all
    event_bus._ai_chat_queue_compat_installed = True
    event_bus._ai_chat_queue_compat_original_subscribe = original_subscribe
    event_bus._ai_chat_queue_compat_original_unsubscribe = original_unsubscribe
    event_bus._ai_chat_queue_compat_original_publish = original_publish
    event_bus._ai_chat_queue_compat_original_publish_all = original_publish_all


_install_event_bus_queue_compatibility()


def _json_safe(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value

    if isinstance(value, float):
        return value if math.isfinite(value) else None

    if isinstance(value, Decimal):
        if not value.is_finite():
            return None

        if value == value.to_integral_value():
            return int(value)

        number = float(value)
        return number if math.isfinite(number) else str(value)

    if isinstance(value, (_datetime, _date, _time_of_day)):
        return value.isoformat()

    if isinstance(value, _timedelta):
        return value.total_seconds()

    if isinstance(value, UUID):
        return str(value)

    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")

    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}

    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]

    return str(value)


class _ControlEvent(threading.Event):
    def __init__(self, control):
        super().__init__()
        self._control = control

    def set(self):
        with self._control.lock:
            super().set()
            self._control.revision += 1

    def clear(self):
        with self._control.lock:
            super().clear()
            self._control.revision += 1


class _JobControl:
    def __init__(self):
        self.lock = threading.RLock()
        self.revision = 0
        self.cancel_event = _ControlEvent(self)
        self.redirect_event = _ControlEvent(self)


def _job_control(job_id):
    key = str(job_id)
    with _job_controls_lock:
        control = _job_controls.get(key)
        if control is None:
            control = _JobControl()
            _job_controls[key] = control
        return control


def _existing_job_control(job_id):
    with _job_controls_lock:
        return _job_controls.get(str(job_id))


def _apply_control_snapshot(control, revision, cancel_requested, has_redirects):
    with control.lock:
        if control.revision != revision:
            return

        if cancel_requested:
            if not control.cancel_event.is_set():
                control.cancel_event.set()
        elif control.cancel_event.is_set():
            control.cancel_event.clear()

        if has_redirects:
            if not control.redirect_event.is_set():
                control.redirect_event.set()
        elif control.redirect_event.is_set():
            control.redirect_event.clear()


def _read_control_state(job_id, control=None):
    revision = None
    if control is not None:
        with control.lock:
            revision = control.revision

    with connect() as connection:
        connection.execute("SET LOCAL statement_timeout = '5s'")
        row = connection.execute(
            """
            SELECT cancel_requested, redirects
            FROM ai_chat_jobs
            WHERE job_id = %s
            """,
            (job_id,),
        ).fetchone()

    cancel_requested = bool(row and row["cancel_requested"])
    has_redirects = bool(row and row["redirects"])

    if control is not None:
        _apply_control_snapshot(
            control,
            revision,
            cancel_requested,
            has_redirects,
        )

    return cancel_requested, has_redirects


def _resynchronize_controls(pool, stop_event):
    with _job_controls_lock:
        registered = tuple(_job_controls.items())

    if not registered:
        return

    controls = {}
    for job_id, control in registered:
        with control.lock:
            controls[job_id] = (control, control.revision)

    job_ids = tuple(controls)
    rows = {}

    with pool.connection(timeout=2) as connection:
        connection.execute("SET LOCAL statement_timeout = '2s'")

        for offset in range(0, len(job_ids), 256):
            if stop_event.is_set():
                return

            batch = job_ids[offset:offset + 256]
            placeholders = ", ".join("%s" for _ in batch)
            query = (
                "SELECT job_id, cancel_requested, redirects "
                "FROM ai_chat_jobs "
                f"WHERE job_id IN ({placeholders})"
            )

            for row in connection.execute(query, batch).fetchall():
                rows[str(row["job_id"])] = row

    for job_id, (control, revision) in controls.items():
        if stop_event.is_set():
            return

        with _job_controls_lock:
            if _job_controls.get(job_id) is not control:
                continue

        row = rows.get(job_id)
        _apply_control_snapshot(
            control,
            revision,
            bool(row and row["cancel_requested"]),
            bool(row and row["redirects"]),
        )


def _control_reconciler(pool, stop_event):
    while not stop_event.is_set():
        try:
            _resynchronize_controls(pool, stop_event)
        except Exception:
            if not stop_event.is_set():
                _log.exception("Failed to reconcile job controls")

        stop_event.wait(5)


def _safe_publish(job_id, event_id):
    try:
        event_bus.publish(job_id, event_id)
    except Exception:
        _log.exception("Failed to publish an event signal")


def _safe_publish_all():
    try:
        event_bus.publish_all()
    except Exception:
        _log.exception("Failed to publish a resynchronization signal")


def _apply_control_notification(connection, payload):
    try:
        notification = json.loads(payload)
    except (TypeError, ValueError):
        return

    if not isinstance(notification, dict):
        return

    job_id = notification.get("job_id")
    action = notification.get("action")

    if not isinstance(job_id, str) or not job_id:
        return

    if action not in ("cancel", "redirect"):
        return

    control = _existing_job_control(job_id)
    if control is None:
        return

    with control.lock:
        if action == "cancel":
            control.cancel_event.set()
        else:
            control.redirect_event.set()


def _notification_listener(database_url, stop_event, ready_event):
    backoff = 0.5
    last_broadcast = float("-inf")
    last_invalid_log = float("-inf")

    def broadcast_if_due(interval):
        nonlocal last_broadcast
        now = time.monotonic()
        if now - last_broadcast >= interval:
            _safe_publish_all()
            last_broadcast = now

    while not stop_event.is_set():
        connected_at = None

        try:
            with psycopg.connect(
                database_url,
                autocommit=True,
                connect_timeout=5,
                options="-c statement_timeout=5000",
            ) as connection:
                connection.execute("LISTEN ai_chat_events")
                connection.execute("LISTEN ai_chat_control")

                connected_at = time.monotonic()
                next_health_check = connected_at + 30
                ready_event.set()
                broadcast_if_due(5)

                while not stop_event.is_set():
                    for notification in connection.notifies(timeout=1):
                        if stop_event.is_set():
                            break

                        if notification.channel == "ai_chat_events":
                            try:
                                data = json.loads(notification.payload)
                                if not isinstance(data, dict):
                                    raise ValueError("Invalid event notification")

                                job_id = data.get("job_id")
                                event_id = data.get("event_id")

                                if not isinstance(job_id, str) or not job_id:
                                    raise ValueError("Invalid notification job ID")

                                if type(event_id) is not int or event_id < 0:
                                    raise ValueError("Invalid notification event ID")
                            except (TypeError, ValueError):
                                now = time.monotonic()
                                if now - last_invalid_log >= 5:
                                    _log.warning(
                                        "Received an invalid event notification"
                                    )
                                    last_invalid_log = now
                                broadcast_if_due(5)
                            else:
                                _safe_publish(job_id, event_id)

                        elif notification.channel == "ai_chat_control":
                            _apply_control_notification(
                                connection,
                                notification.payload,
                            )

                        broadcast_if_due(30)

                    if stop_event.is_set():
                        break

                    now = time.monotonic()
                    if now >= next_health_check:
                        connection.execute("SELECT 1")
                        next_health_check = time.monotonic() + 30

                    broadcast_if_due(30)

        except Exception:
            if not stop_event.is_set():
                _log.exception("PostgreSQL notification listener failed")
        finally:
            was_ready = ready_event.is_set()
            ready_event.clear()

            if was_ready and not stop_event.is_set():
                broadcast_if_due(5)

        if stop_event.is_set():
            break

        if connected_at is not None and time.monotonic() - connected_at >= 10:
            backoff = 0.5

        broadcast_if_due(30)

        delay = min(backoff, 30) * (0.5 + random.random())
        if stop_event.wait(delay):
            break

        backoff = min(backoff * 2, 30)

    ready_event.clear()


def open_pool():
    global _pool
    global _listener_thread
    global _control_thread
    global _listener_stop
    global _listener_ready

    with _pool_condition:
        while _pool_closing:
            _pool_condition.wait()

        if _pool is not None:
            if _listener_thread is None or not _listener_thread.is_alive():
                raise RuntimeError(
                    "The PostgreSQL notification listener is not running"
                )
            if _control_thread is None or not _control_thread.is_alive():
                raise RuntimeError("The job-control reconciler is not running")
            return

        if (
            _listener_thread is not None
            and _listener_thread.is_alive()
        ) or (
            _control_thread is not None
            and _control_thread.is_alive()
        ):
            raise RuntimeError("A previous database background thread is still running")

        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL is not configured")

        try:
            max_size = int(os.environ.get("DB_POOL_MAX_SIZE", "8"))
        except ValueError as error:
            raise RuntimeError(
                "DB_POOL_MAX_SIZE must be an integer"
            ) from error

        if max_size < 1:
            raise RuntimeError("DB_POOL_MAX_SIZE must be at least 1")

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
        except BaseException:
            try:
                pool.close(timeout=5)
            except Exception:
                _log.exception("Failed to close a pool after startup failure")
            raise

        stop_event = threading.Event()
        ready_event = threading.Event()

        listener = threading.Thread(
            target=_notification_listener,
            args=(database_url, stop_event, ready_event),
            name="postgres-notification-listener",
            daemon=True,
        )
        reconciler = threading.Thread(
            target=_control_reconciler,
            args=(pool, stop_event),
            name="postgres-job-control-reconciler",
            daemon=True,
        )

        try:
            listener.start()

            if (
                not ready_event.wait(timeout=15)
                or not ready_event.is_set()
                or not listener.is_alive()
            ):
                raise RuntimeError(
                    "PostgreSQL notification listener did not become ready"
                )

            reconciler.start()
        except BaseException:
            stop_event.set()

            if listener.ident is not None:
                listener.join(timeout=15)

            if reconciler.ident is not None:
                reconciler.join(timeout=10)

            try:
                pool.close(timeout=5)
            except Exception:
                _log.exception("Failed to close a pool after startup failure")

            _listener_thread = listener if listener.is_alive() else None
            _control_thread = reconciler if reconciler.is_alive() else None
            raise

        _pool = pool
        _listener_thread = listener
        _control_thread = reconciler
        _listener_stop = stop_event
        _listener_ready = ready_event


def close_pool():
    global _pool
    global _listener_thread
    global _control_thread
    global _pool_closing

    with _pool_condition:
        while _pool_closing:
            _pool_condition.wait()

        pool = _pool
        listener = _listener_thread
        reconciler = _control_thread

        if pool is None and not any(
            thread is not None and thread.is_alive()
            for thread in (listener, reconciler)
        ):
            _listener_thread = None
            _control_thread = None
            _listener_ready.clear()
            return

        if threading.current_thread() in (listener, reconciler):
            raise RuntimeError(
                "A database background thread cannot close its own pool"
            )

        _pool_closing = True
        _listener_stop.set()
        _listener_ready.clear()

    try:
        for thread in (listener, reconciler):
            if thread is not None and thread.ident is not None:
                thread.join(timeout=15)

        with _pool_condition:
            while _pool_acquiring:
                _pool_condition.wait()

        if pool is not None:
            pool.close(timeout=5)
    finally:
        with _pool_condition:
            if _pool is pool:
                _pool = None

            _listener_thread = (
                listener if listener is not None and listener.is_alive() else None
            )
            _control_thread = (
                reconciler
                if reconciler is not None and reconciler.is_alive()
                else None
            )

            _pool_closing = False
            _pool_condition.notify_all()

    if _listener_thread is not None or _control_thread is not None:
        raise RuntimeError("A database background thread did not stop")


@contextmanager
def connect():
    global _pool_acquiring

    with _pool_lock:
        if _pool_closing:
            raise RuntimeError("The database pool is closing")

        if _pool is None:
            open_pool()

        pool = _pool
        _pool_acquiring += 1

    acquiring = True

    try:
        with pool.connection(timeout=10) as connection:
            with _pool_condition:
                _pool_acquiring -= 1
                acquiring = False
                _pool_condition.notify_all()

            yield connection
    finally:
        if acquiring:
            with _pool_condition:
                _pool_acquiring -= 1
                _pool_condition.notify_all()


@contextmanager
def instavm_vm_startup_lock():
    with _vm_startup_process_lock:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL is not configured")

        with psycopg.connect(
            database_url,
            connect_timeout=10,
            autocommit=True,
        ) as connection:
            connection.execute(
                "SELECT pg_advisory_lock(%s)",
                (_VM_STARTUP_ADVISORY_LOCK_KEY,),
            )
            try:
                yield
            finally:
                result = connection.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (_VM_STARTUP_ADVISORY_LOCK_KEY,),
                ).fetchone()
                if result is None or result[0] is not True:
                    raise RuntimeError(
                        "The InstaVM startup lock could not be released"
                    )


def _set_transaction_timeouts(connection):
    connection.execute("SET LOCAL lock_timeout = '5s'")
    connection.execute("SET LOCAL statement_timeout = '15s'")


def _validate_messages(messages, name):
    if not isinstance(messages, list):
        raise TypeError(f"{name} must be a list")

    if any(not isinstance(message, dict) for message in messages):
        raise TypeError(f"{name} must contain only dictionaries")


def upsert_conversation(conversation_id, messages, title):
    _validate_messages(messages, "messages")

    with connect() as connection:
        connection.execute(
            """
            INSERT INTO ai_chat_conversations
                (conversation_id, title, messages)
            VALUES (%s, %s, %s)
            ON CONFLICT (conversation_id) DO UPDATE
            SET title = EXCLUDED.title,
                messages = EXCLUDED.messages,
                updated_at = NOW()
            """,
            (conversation_id, title, Jsonb(messages)),
        )


def create_job(job_id, conversation_id, agent_messages):
    _validate_messages(agent_messages, "agent_messages")

    with connect() as connection:
        _set_transaction_timeouts(connection)

        connection.execute(
            """
            INSERT INTO ai_chat_jobs
                (job_id, conversation_id, status, agent_messages)
            VALUES (%s, %s, 'running', %s)
            """,
            (job_id, conversation_id, Jsonb(agent_messages)),
        )

        row = connection.execute(
            """
            UPDATE ai_chat_conversations
            SET active_job_id = %s,
                updated_at = NOW()
            WHERE conversation_id = %s
              AND (
                  active_job_id IS NULL
                  OR active_job_id = %s
              )
            RETURNING conversation_id
            """,
            (job_id, conversation_id, job_id),
        ).fetchone()

        if row is None:
            raise RuntimeError(
                "The conversation does not exist or has another active job"
            )


def get_job(job_id):
    with connect() as connection:
        return connection.execute(
            "SELECT * FROM ai_chat_jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()


def get_job_and_conversation(job_id):
    with connect() as connection:
        row = connection.execute(
            """
            SELECT
                j.*,
                c.conversation_id AS _joined_conversation_id,
                c.vm_id,
                c.vm_session_id,
                c.messages AS conversation_messages
            FROM ai_chat_jobs AS j
            LEFT JOIN ai_chat_conversations AS c
                ON c.conversation_id = j.conversation_id
            WHERE j.job_id = %s
            """,
            (job_id,),
        ).fetchone()

    if row is not None and row.pop("_joined_conversation_id") is None:
        raise LookupError("The job references a missing conversation")

    return row


def save_agent_messages(job_id, agent_messages):
    _validate_messages(agent_messages, "agent_messages")

    with connect() as connection:
        row = connection.execute(
            """
            UPDATE ai_chat_jobs
            SET agent_messages = %s,
                updated_at = NOW()
            WHERE job_id = %s
            RETURNING job_id
            """,
            (Jsonb(agent_messages), job_id),
        ).fetchone()

        if row is None:
            raise LookupError("Job not found")


def _artifact_link(payload):
    if payload.get("type") != "agent_event":
        return ""

    event = payload.get("event")
    if event is None:
        return ""

    if not isinstance(event, dict):
        raise TypeError("agent_event event must be a dictionary")

    artifact = event.get("artifact")
    if artifact is None:
        return ""

    if not isinstance(artifact, dict):
        raise TypeError("artifact must be a dictionary")

    artifact_id = artifact.get("artifact_id")
    if artifact_id is None or artifact_id == "":
        return ""

    if not isinstance(artifact_id, str):
        raise TypeError("artifact_id must be a string")

    name = artifact.get("name") or "Letöltés"
    name = escape(str(name), quote=False)
    name = (
        name.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("\r", " ")
        .replace("\n", " ")
    )
    encoded_id = quote(artifact_id, safe="")

    return f"\n\n[{name}](/api/artifacts/{encoded_id})"


def append_event(job_id, payload):
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dictionary")

    content = payload.get("content") if payload.get("type") == "delta" else None
    reasoning = (
        payload.get("reasoning_content")
        if payload.get("type") == "delta"
        else None
    )

    if content is not None and not isinstance(content, str):
        raise TypeError("delta content must be a string")

    if reasoning is not None and not isinstance(reasoning, str):
        raise TypeError("delta reasoning_content must be a string")

    artifact_link = _artifact_link(payload)

    with connect() as connection:
        _set_transaction_timeouts(connection)

        job = connection.execute(
            """
            SELECT status
            FROM ai_chat_jobs
            WHERE job_id = %s
            FOR UPDATE
            """,
            (job_id,),
        ).fetchone()

        if job is None:
            raise LookupError("Job not found")

        if job["status"] != "running":
            raise RuntimeError("Cannot append an event to a non-running job")

        row = connection.execute(
            """
            INSERT INTO ai_chat_events (job_id, payload)
            VALUES (%s, %s)
            RETURNING event_id
            """,
            (job_id, Jsonb(payload)),
        ).fetchone()

        event_id = row["event_id"]

        if type(event_id) is not int or event_id <= 0:
            raise ValueError("Database event IDs must be positive integers")

        updated = connection.execute(
            """
            UPDATE ai_chat_jobs
            SET response_content =
                    COALESCE(response_content, '') || %s || %s,
                response_reasoning =
                    COALESCE(response_reasoning, '') || %s,
                progress = COALESCE(%s, progress),
                updated_at = NOW()
            WHERE job_id = %s
            RETURNING job_id
            """,
            (
                content or "",
                artifact_link,
                reasoning or "",
                payload.get("progress"),
                job_id,
            ),
        ).fetchone()

        if updated is None:
            raise LookupError("Job not found")

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

    _safe_publish(job_id, event_id)
    return event_id


def list_events(job_id, after=0):
    if type(after) is not int:
        raise TypeError("after must be an integer")

    if after < 0:
        raise ValueError("after must not be negative")

    with connect() as connection:
        return connection.execute(
            """
            SELECT event_id, payload
            FROM ai_chat_events
            WHERE job_id = %s
              AND event_id > %s
            ORDER BY event_id
            """,
            (job_id, after),
        ).fetchall()


def latest_event_id(job_id):
    with connect() as connection:
        row = connection.execute(
            """
            SELECT COALESCE(MAX(event_id), 0) AS event_id
            FROM ai_chat_events
            WHERE job_id = %s
            """,
            (job_id,),
        ).fetchone()

    return row["event_id"]


async def wait_for_events(job_id, after=0, timeout=None):
    if type(after) is not int:
        raise TypeError("after must be an integer")

    if after < 0:
        raise ValueError("after must not be negative")

    if timeout is not None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
        ):
            raise TypeError("timeout must be a finite number or None")

        if timeout < 0:
            raise ValueError("timeout must not be negative")

    deadline = None if timeout is None else time.monotonic() + timeout
    subscription = event_bus.subscribe(job_id)

    try:
        while True:
            events = await asyncio.to_thread(list_events, job_id, after)
            if events:
                return events

            if deadline is None:
                interval = 1.0
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                interval = min(1.0, remaining)

            try:
                await asyncio.wait_for(subscription.queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                continue

            subscription.queue.task_done()
    finally:
        event_bus.unsubscribe(subscription)


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


def request_cancel(job_id):
    with connect() as connection:
        _set_transaction_timeouts(connection)

        row = connection.execute(
            """
            SELECT cancel_requested
            FROM ai_chat_jobs
            WHERE job_id = %s
              AND status IN ('queued', 'running')
            FOR UPDATE
            """,
            (job_id,),
        ).fetchone()

        if row is None:
            return False

        if not row["cancel_requested"]:
            connection.execute(
                """
                UPDATE ai_chat_jobs
                SET cancel_requested = TRUE,
                    updated_at = NOW()
                WHERE job_id = %s
                """,
                (job_id,),
            )
            _notify_control(connection, job_id, "cancel")

    control = _existing_job_control(job_id)
    if control is not None:
        control.cancel_event.set()

    return True


def is_cancel_requested(job_id):
    control = _existing_job_control(job_id)
    requested, _ = _read_control_state(job_id, control)
    return requested


def initialize_job_state(job_id, cancel_requested=False, has_redirects=False):
    control = _job_control(job_id)
    _read_control_state(job_id, control)

    with control.lock:
        if cancel_requested:
            control.cancel_event.set()
        if has_redirects:
            control.redirect_event.set()


def cancel_event(job_id):
    control = _job_control(job_id)
    _read_control_state(job_id, control)
    return control.cancel_event


def redirect_event(job_id):
    control = _job_control(job_id)
    _read_control_state(job_id, control)
    return control.redirect_event


def add_redirect(job_id, message):
    if isinstance(message, str):
        message_content = message.strip()
        if not message_content:
            raise ValueError("message must not be empty")
    elif isinstance(message, list):
        if not message:
            raise ValueError("message content must not be empty")

        if any(not isinstance(part, dict) for part in message):
            raise TypeError(
                "message content must contain only dictionaries"
            )

        json.dumps(message, allow_nan=False)
        message_content = message
    else:
        raise TypeError("message must be a string or a list of dictionaries")

    with connect() as connection:
        _set_transaction_timeouts(connection)

        row = connection.execute(
            """
            UPDATE ai_chat_jobs
            SET redirects =
                    COALESCE(redirects, '[]'::jsonb) || %s::jsonb,
                updated_at = NOW()
            WHERE job_id = %s
              AND status = 'running'
            RETURNING conversation_id
            """,
            (Jsonb([message_content]), job_id),
        ).fetchone()

        if row is None:
            return None

        conversation_id = row["conversation_id"]
        conversation = connection.execute(
            """
            UPDATE ai_chat_conversations
            SET messages =
                    COALESCE(messages, '[]'::jsonb) || %s::jsonb,
                updated_at = NOW()
            WHERE conversation_id = %s
            RETURNING conversation_id
            """,
            (
                Jsonb(
                    [{"role": "user", "content": message_content}]
                ),
                conversation_id,
            ),
        ).fetchone()

        if conversation is None:
            raise LookupError("The job references a missing conversation")

        _notify_control(connection, job_id, "redirect")

    control = _existing_job_control(job_id)
    if control is not None:
        control.redirect_event.set()

    return conversation_id


def take_redirects(job_id):
    with connect() as connection:
        _set_transaction_timeouts(connection)

        row = connection.execute(
            """
            SELECT redirects
            FROM ai_chat_jobs
            WHERE job_id = %s
            FOR UPDATE
            """,
            (job_id,),
        ).fetchone()

        redirects = [] if row is None else row["redirects"] or []

        if not isinstance(redirects, list):
            raise TypeError("Stored redirects must be a list")

        if redirects:
            connection.execute(
                """
                UPDATE ai_chat_jobs
                SET redirects = '[]'::jsonb,
                    updated_at = NOW()
                WHERE job_id = %s
                """,
                (job_id,),
            )

    control = _existing_job_control(job_id)
    if control is not None:
        try:
            _read_control_state(job_id, control)
        except Exception:
            _log.exception(
                "Failed to refresh redirect state after taking redirects"
            )

    return redirects


def has_redirects(job_id):
    control = _existing_job_control(job_id)
    _, redirected = _read_control_state(job_id, control)
    return redirected


def finish_job(job_id, status, error=None):
    if not isinstance(status, str) or not status:
        raise ValueError("status must be a nonempty string")

    if status in ("queued", "running"):
        raise ValueError("finish_job requires a terminal status")

    if error is not None and not isinstance(error, str):
        error = str(error)

    with connect() as connection:
        _set_transaction_timeouts(connection)

        row = connection.execute(
            """
            UPDATE ai_chat_jobs
            SET status = %s,
                error = %s,
                redirects = '[]'::jsonb,
                updated_at = NOW()
            WHERE job_id = %s
              AND status IN ('queued', 'running')
            RETURNING conversation_id, response_content
            """,
            (status, error, job_id),
        ).fetchone()

        if row is None:
            return

        conversation_id = row["conversation_id"]
        conversation = connection.execute(
            """
            SELECT messages, active_job_id
            FROM ai_chat_conversations
            WHERE conversation_id = %s
            FOR UPDATE
            """,
            (conversation_id,),
        ).fetchone()

        if conversation is None:
            raise LookupError("The job references a missing conversation")

        active_job_id = conversation["active_job_id"]
        if active_job_id is not None and str(active_job_id) != str(job_id):
            raise RuntimeError("The conversation has another active job")

        messages = conversation["messages"]
        if messages is None:
            messages = []

        _validate_messages(messages, "stored conversation messages")

        messages.append(
            {
                "role": "assistant",
                "content": row["response_content"] or "",
            }
        )

        connection.execute(
            """
            UPDATE ai_chat_conversations
            SET messages = %s,
                active_job_id = CASE
                    WHEN active_job_id = %s THEN NULL
                    ELSE active_job_id
                END,
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

    _safe_publish(job_id, 0)

    control = _existing_job_control(job_id)
    if control is not None:
        control.redirect_event.clear()

    clear_job_state(job_id)


def clear_job_state(job_id):
    with _job_controls_lock:
        _job_controls.pop(str(job_id), None)


def list_conversations(conversation_ids):
    if isinstance(conversation_ids, (str, bytes, bytearray)):
        raise TypeError("conversation_ids must be an iterable of IDs")

    identifiers = tuple(dict.fromkeys(conversation_ids))
    if not identifiers:
        return []

    output = []

    with connect() as connection:
        connection.execute(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
        )

        for offset in range(0, len(identifiers), 256):
            batch = identifiers[offset:offset + 256]
            placeholders = ", ".join("%s" for _ in batch)

            query = f"""
                SELECT
                    c.conversation_id,
                    c.title,
                    c.messages,
                    c.vm_id,
                    c.active_job_id,
                    c.updated_at,
                    EXTRACT(EPOCH FROM c.updated_at) AS sort_updated_at,
                    j.job_id AS latest_job_id,
                    j.conversation_id AS latest_job_conversation_id,
                    j.status AS latest_job_status,
                    j.progress AS latest_job_progress,
                    j.response_content AS latest_job_response_content,
                    j.response_reasoning AS latest_job_response_reasoning,
                    j.error AS latest_job_error,
                    j.updated_at AS latest_job_updated_at,
                    COALESCE(e.last_event_id, 0)
                        AS latest_job_last_event_id
                FROM ai_chat_conversations AS c
                LEFT JOIN LATERAL (
                    SELECT
                        candidate.job_id,
                        candidate.conversation_id,
                        candidate.status,
                        candidate.progress,
                        candidate.response_content,
                        candidate.response_reasoning,
                        candidate.error,
                        candidate.updated_at
                    FROM ai_chat_jobs AS candidate
                    WHERE candidate.conversation_id = c.conversation_id
                    ORDER BY
                        (candidate.job_id = c.active_job_id) DESC,
                        candidate.created_at DESC,
                        candidate.job_id DESC
                    LIMIT 1
                ) AS j ON TRUE
                LEFT JOIN LATERAL (
                    SELECT MAX(events.event_id) AS last_event_id
                    FROM ai_chat_events AS events
                    WHERE events.job_id = j.job_id
                ) AS e ON TRUE
                WHERE c.conversation_id IN ({placeholders})
            """

            for row in connection.execute(query, batch).fetchall():
                conversation = {
                    "conversation_id": row["conversation_id"],
                    "title": row["title"],
                    "messages": row["messages"],
                    "vm_id": row["vm_id"],
                    "active_job_id": row["active_job_id"],
                    "updated_at": row["updated_at"],
                }

                if row["latest_job_id"] is None:
                    conversation["latest_job"] = None
                else:
                    conversation["latest_job"] = {
                        "job_id": row["latest_job_id"],
                        "conversation_id": row[
                            "latest_job_conversation_id"
                        ],
                        "status": row["latest_job_status"],
                        "progress": row["latest_job_progress"],
                        "response_content": row[
                            "latest_job_response_content"
                        ],
                        "response_reasoning": row[
                            "latest_job_response_reasoning"
                        ],
                        "error": row["latest_job_error"],
                        "updated_at": row["latest_job_updated_at"],
                        "last_event_id": row[
                            "latest_job_last_event_id"
                        ],
                    }

                output.append(
                    (
                        row["sort_updated_at"],
                        str(row["conversation_id"]),
                        _json_safe(conversation),
                    )
                )

    output.sort(
        key=lambda item: (
            item[0] is not None,
            item[0] if item[0] is not None else 0,
            item[1],
        ),
        reverse=True,
    )

    return [conversation for _, _, conversation in output]


def pending_jobs():
    with connect() as connection:
        return connection.execute(
            """
            SELECT job_id
            FROM ai_chat_jobs
            WHERE status = 'running'
            ORDER BY created_at
            """
        ).fetchall()


def create_artifact(
    artifact_id,
    conversation_id,
    job_id,
    name,
    content_type,
    content,
):
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO ai_chat_artifacts
                (
                    artifact_id,
                    conversation_id,
                    job_id,
                    name,
                    content_type,
                    content
                )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                artifact_id,
                conversation_id,
                job_id,
                name,
                content_type,
                content,
            ),
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
        row = connection.execute(
            """
            UPDATE ai_chat_conversations
            SET vm_id = %s,
                vm_session_id = %s,
                updated_at = NOW()
            WHERE conversation_id = %s
            RETURNING conversation_id
            """,
            (vm_id, session_id, conversation_id),
        ).fetchone()

        if row is None:
            raise LookupError("Conversation not found")


def _close_on_exit():
    try:
        close_pool()
    except Exception:
        _log.exception("Failed to close the database pool during shutdown")


atexit.register(_close_on_exit)