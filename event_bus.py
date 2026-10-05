import asyncio
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class EventSignal:
    event_id: int


class EventSubscription:
    def __init__(self, job_id, loop, queue):
        self.job_id = job_id
        self.loop = loop
        self.queue = queue


_lock = threading.RLock()
_subscriptions = {}
_queue_limit = 1024


def subscribe(job_id):
    loop = asyncio.get_running_loop()
    subscription = EventSubscription(
        str(job_id),
        loop,
        asyncio.Queue(maxsize=_queue_limit),
    )
    with _lock:
        _subscriptions.setdefault(subscription.job_id, set()).add(subscription)
    return subscription


def unsubscribe(subscription):
    with _lock:
        subscribers = _subscriptions.get(subscription.job_id)
        if subscribers is None:
            return
        subscribers.discard(subscription)
        if not subscribers:
            _subscriptions.pop(subscription.job_id, None)


def _deliver(subscription, signal):
    try:
        subscription.queue.put_nowait(signal)
    except asyncio.QueueFull:
        while True:
            try:
                subscription.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        subscription.queue.put_nowait(EventSignal(0))


def _publish_to(subscriptions, event_id):
    signal = EventSignal(int(event_id))
    stale = []
    for subscription in subscriptions:
        try:
            subscription.loop.call_soon_threadsafe(_deliver, subscription, signal)
        except RuntimeError:
            stale.append(subscription)
    for subscription in stale:
        unsubscribe(subscription)


def publish(job_id, event_id):
    with _lock:
        subscriptions = tuple(_subscriptions.get(str(job_id), ()))
    _publish_to(subscriptions, event_id)


def publish_all():
    with _lock:
        subscriptions = tuple(
            subscription
            for job_subscriptions in _subscriptions.values()
            for subscription in job_subscriptions
        )
    _publish_to(subscriptions, 0)
