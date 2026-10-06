import asyncio
import collections
import enum
import operator
import threading
import weakref
from dataclasses import dataclass


class SignalKind(enum.Enum):
    EVENT = "event"
    BROADCAST = "broadcast"
    OVERFLOW = "overflow"


@dataclass(frozen=True)
class EventSignal:
    event_id: int
    kind: SignalKind = SignalKind.EVENT
    dropped: int = 0

    @property
    def is_event(self):
        return self.kind is SignalKind.EVENT

    @property
    def is_broadcast(self):
        return self.kind is SignalKind.BROADCAST

    @property
    def is_overflow(self):
        return self.kind is SignalKind.OVERFLOW


class SubscriptionClosed(Exception):
    pass


_lock = threading.RLock()
_subscriptions = {}
_queue_limit = 1024


def _validate_job_id(job_id):
    if job_id is None:
        raise TypeError("job_id must not be None")
    return str(job_id)


def _validate_event_id(event_id):
    if isinstance(event_id, bool):
        raise TypeError("event_id must be an integer, not bool")
    try:
        return operator.index(event_id)
    except TypeError:
        raise TypeError(
            f"event_id must be an integer, got {type(event_id).__name__}"
        ) from None


def _validate_limit(limit):
    if isinstance(limit, bool):
        raise TypeError("limit must be an integer, not bool")
    try:
        value = operator.index(limit)
    except TypeError:
        raise TypeError(
            f"limit must be an integer, got {type(limit).__name__}"
        ) from None
    if value < 2:
        raise ValueError("limit must be at least 2")
    return value


def _validate_loop(loop):
    if not isinstance(loop, asyncio.AbstractEventLoop):
        raise TypeError(
            f"loop must be an asyncio event loop, got {type(loop).__name__}"
        )
    if loop.is_closed():
        raise RuntimeError("cannot subscribe on a closed event loop")
    return loop


class EventSubscription:
    __slots__ = (
        "job_id",
        "loop",
        "_limit",
        "_lock",
        "_buffer",
        "_waiters",
        "_scheduled",
        "_closed",
        "__weakref__",
    )

    def __init__(self, job_id, loop, limit=_queue_limit):
        self.job_id = _validate_job_id(job_id)
        self.loop = _validate_loop(loop)
        self._limit = _validate_limit(limit)
        self._lock = threading.Lock()
        self._buffer = collections.deque()
        self._waiters = collections.deque()
        self._scheduled = False
        self._closed = False

    def __repr__(self):
        return (
            f"{type(self).__name__}(job_id={self.job_id!r}, "
            f"closed={self._closed}, pending={self.qsize()})"
        )

    @property
    def closed(self):
        return self._closed

    @property
    def limit(self):
        return self._limit

    def qsize(self):
        with self._lock:
            return len(self._buffer)

    def empty(self):
        with self._lock:
            return not self._buffer

    def _make_room_locked(self):
        dropped = 0
        highest = 0
        while self._buffer and len(self._buffer) >= self._limit - 1:
            oldest = self._buffer.popleft()
            dropped += oldest.dropped if oldest.is_overflow else 1
            highest = max(highest, oldest.event_id)
        if self._buffer and self._buffer[0].is_overflow:
            head = self._buffer.popleft()
            dropped += head.dropped
            highest = max(highest, head.event_id)
        self._buffer.appendleft(
            EventSignal(highest, SignalKind.OVERFLOW, dropped)
        )

    def _notify(self, signal):
        with self._lock:
            if self._closed or self.loop.is_closed():
                return False
            if len(self._buffer) >= self._limit:
                self._make_room_locked()
            self._buffer.append(signal)
            if self._scheduled:
                return True
            self._scheduled = True
        try:
            self.loop.call_soon_threadsafe(self._wake)
        except RuntimeError:
            with self._lock:
                self._scheduled = False
            return False
        return True

    def _wake(self):
        with self._lock:
            self._scheduled = False
            waiters = tuple(self._waiters)
            self._waiters.clear()
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    def _check_loop(self):
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError(
                "EventSubscription is bound to a different event loop"
            )

    def get_nowait(self):
        with self._lock:
            if self._buffer:
                return self._buffer.popleft()
            if self._closed:
                raise SubscriptionClosed(self.job_id)
        raise asyncio.QueueEmpty

    async def get(self):
        self._check_loop()
        while True:
            with self._lock:
                if self._buffer:
                    return self._buffer.popleft()
                if self._closed:
                    raise SubscriptionClosed(self.job_id)
                waiter = self.loop.create_future()
                self._waiters.append(waiter)
            try:
                await waiter
            finally:
                with self._lock:
                    try:
                        self._waiters.remove(waiter)
                    except ValueError:
                        pass

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._buffer.clear()
            wake_pending = self._scheduled
            self._scheduled = True
        _forget(self)
        if wake_pending:
            return
        try:
            self.loop.call_soon_threadsafe(self._wake)
        except RuntimeError:
            pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.get()
        except SubscriptionClosed:
            raise StopAsyncIteration from None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.close()
        return False


def _forget(subscription):
    with _lock:
        subscribers = _subscriptions.get(subscription.job_id)
        if subscribers is None:
            return
        subscribers.discard(subscription)
        if not subscribers:
            _subscriptions.pop(subscription.job_id, None)


def subscribe(job_id, loop=None, limit=None):
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError(
                "subscribe() requires a running event loop "
                "or an explicit loop argument"
            ) from None
    subscription = EventSubscription(
        job_id,
        loop,
        _queue_limit if limit is None else limit,
    )
    with _lock:
        subscribers = _subscriptions.get(subscription.job_id)
        if subscribers is None:
            subscribers = weakref.WeakSet()
            _subscriptions[subscription.job_id] = subscribers
        subscribers.add(subscription)
    return subscription


def unsubscribe(subscription):
    if not isinstance(subscription, EventSubscription):
        raise TypeError(
            f"expected EventSubscription, got {type(subscription).__name__}"
        )
    subscription.close()


def subscriber_count(job_id=None):
    with _lock:
        if job_id is None:
            return sum(len(tuple(s)) for s in _subscriptions.values())
        subscribers = _subscriptions.get(_validate_job_id(job_id))
        if subscribers is None:
            return 0
        return len(tuple(subscribers))


def _publish_to(subscriptions, signal):
    delivered = 0
    stale = []
    for subscription in subscriptions:
        if subscription._notify(signal):
            delivered += 1
        else:
            stale.append(subscription)
    for subscription in stale:
        subscription.close()
    return delivered


def publish(job_id, event_id):
    key = _validate_job_id(job_id)
    signal = EventSignal(_validate_event_id(event_id))
    with _lock:
        subscribers = _subscriptions.get(key)
        if subscribers is None:
            return 0
        targets = tuple(subscribers)
        if not targets:
            _subscriptions.pop(key, None)
            return 0
        return _publish_to(targets, signal)


def publish_all():
    signal = EventSignal(0, SignalKind.BROADCAST)
    with _lock:
        targets = []
        for key in tuple(_subscriptions):
            subscribers = tuple(_subscriptions[key])
            if not subscribers:
                _subscriptions.pop(key, None)
                continue
            targets.extend(subscribers)
        return _publish_to(tuple(targets), signal)