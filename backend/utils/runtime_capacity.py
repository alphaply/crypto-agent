"""Process-local resource commitments; release follows the physical worker.

Defaults are calibration settings, not a verified production capacity profile.
No account, operation or configuration identifiers are metric labels here.
"""
from __future__ import annotations

import contextvars
import json
import math
import os
import threading
import time
import weakref
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass


ROLE_DEFAULTS = {'agent': 2, 'mcp_read': 4, 'mcp_execute': 2, 'mcp_auth': 2,
                 'maintenance': 2, 'recovery': 2, 'memory': 2, 'news': 4,
                 'metadata': 2, 'stream': 2, 'paper': 2}
BYTE_KINDS = frozenset({'catalogue', 'client', 'quotes', 'coordinator', 'parse',
                        'news', 'stream', 'paper_payload', 'other'})
current_capacity = contextvars.ContextVar('runtime_capacity_lease', default=None)


class CapacityBusy(ValueError):
    """This invocation did not start; it says nothing about an old operation."""
    def __init__(self, stage='capacity', reason='busy'):
        self.stage, self.reason = stage, reason
        super().__init__(f'Runtime capacity busy at {stage}: {reason}')


def positive_setting(name: str, default: int | float, *, minimum=1):
    raw = os.getenv(name)
    try:
        value = float(default if raw is None else raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{name} must be a finite number >= {minimum}') from exc
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f'{name} must be a finite number >= {minimum}')
    if isinstance(default, int):
        if not value.is_integer():
            raise ValueError(f'{name} must be an integer')
        return int(value)
    return value


@dataclass(eq=False)
class CapacityTicket:
    manager: 'CapacityManager'
    role: str
    critical: bool
    created_at: float
    waiting: bool = True
    ready: bool = True

    def try_acquire(self):
        return self.manager._start(self)

    def cancel(self):
        self.manager._cancel(self)


class CapacityLease:
    def __init__(self, manager, role, critical):
        self.manager, self.role, self.critical = manager, role, critical
        self.released, self.started = False, False

    def mark_started(self):
        with self.manager.condition:
            if not self.released and not self.started:
                self.started = True
                self.manager.running[self.role] += 1

    def release(self):
        with self.manager.condition:
            if self.released:
                return
            self.released = True
            self.manager.active[self.role] -= 1
            if self.started:
                self.manager.running[self.role] -= 1
            if not self.critical:
                self.manager.ordinary -= 1
            self.manager.condition.notify_all()


class ByteLease:
    def __init__(self, manager, kind, amount):
        self.manager, self.kind, self.amount = manager, kind, amount
        self.released = False

    def release(self):
        with self.manager.condition:
            if not self.released:
                self.released = True
                self.manager.bytes[self.kind] -= self.amount
                self.manager.condition.notify_all()

    def transfer(self, kind, amount):
        """Retain part of admitted refresh space without a second admission.

        The total commitment remains unchanged, including while two refreshes
        finish simultaneously at the aggregate byte limit.
        """
        if kind not in BYTE_KINDS or type(amount) is not int or amount < 0:
            raise ValueError('Invalid byte transfer')
        with self.manager.condition:
            if self.released or amount > self.amount:
                raise ValueError('Byte transfer exceeds the live commitment')
            self.amount -= amount
            self.manager.bytes[self.kind] -= amount
            self.manager.bytes[kind] += amount
            return ByteLease(self.manager, kind, amount)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.release()


class CapacityManager:
    def __init__(self, *, total=8, reserved=2, queue_limit=16, wait_seconds=2.0,
                 role_limits=None, byte_limit=512 * 1024 * 1024):
        if (not isinstance(total, int) or not isinstance(reserved, int)
                or total < 1 or reserved < 0 or reserved >= total
                or not isinstance(queue_limit, int) or queue_limit < 0
                or not math.isfinite(wait_seconds) or wait_seconds < 0
                or not isinstance(byte_limit, int) or byte_limit < 1):
            raise ValueError('Invalid runtime capacity configuration')
        limits = {**ROLE_DEFAULTS, **(role_limits or {})}
        if set(limits) != set(ROLE_DEFAULTS) or any(type(v) is not int or v < 1 for v in limits.values()):
            raise ValueError('Role limits must use fixed runtime roles and positive integers')
        self.total, self.reserved, self.queue_limit = total, reserved, queue_limit
        self.wait_seconds, self.role_limits, self.byte_limit = wait_seconds, limits, byte_limit
        self.condition = threading.Condition(threading.RLock())
        self.tickets = []
        self.active, self.running, self.bytes = Counter(), Counter(), Counter()
        self.ordinary = self.peak_active = self.peak_queued = self.peak_bytes = 0
        self.rejected, self.pressure, self.pressure_reason = Counter(), False, None

    @classmethod
    def from_environment(cls):
        try:
            limits = json.loads(os.getenv('RUNTIME_CAPACITY_ROLE_LIMITS', '{}'))
        except (ValueError, TypeError) as exc:
            raise ValueError('RUNTIME_CAPACITY_ROLE_LIMITS must be a JSON object') from exc
        if not isinstance(limits, dict):
            raise ValueError('RUNTIME_CAPACITY_ROLE_LIMITS must be a JSON object')
        return cls(total=positive_setting('RUNTIME_CAPACITY_TOTAL_WORKERS', 8),
                   reserved=positive_setting('RUNTIME_CAPACITY_RESERVED_WORKERS', 2, minimum=0),
                   queue_limit=positive_setting('RUNTIME_CAPACITY_QUEUE_LIMIT', 16, minimum=0),
                   wait_seconds=positive_setting('RUNTIME_CAPACITY_QUEUE_SECONDS', 2.0, minimum=0),
                   role_limits=limits, byte_limit=positive_setting('RUNTIME_CAPACITY_BYTES', 512 * 1024 * 1024))

    def _can_start(self, role, critical):
        return (sum(self.active.values()) < self.total and self.active[role] < self.role_limits[role]
                and (critical or not self.pressure and self.ordinary < self.total - self.reserved))

    def enqueue(self, role, *, critical=False):
        if role not in self.role_limits:
            raise ValueError(f'Unknown runtime role: {role}')
        with self.condition:
            if len(self.tickets) >= self.queue_limit:
                self.rejected[role] += 1
                raise CapacityBusy(role, 'queue_full')
            ticket = CapacityTicket(self, role, critical, time.monotonic())
            self.tickets.append(ticket)
            self.peak_queued = max(self.peak_queued, len(self.tickets))
            return ticket

    def _start(self, ticket):
        with self.condition:
            if not ticket.waiting:
                raise ValueError('Capacity ticket is no longer queued')
            eligible = next((item for item in self.tickets if item.ready and self._can_start(item.role, item.critical)), None)
            if eligible is not ticket:
                return None
            self.tickets.remove(ticket)
            ticket.waiting = False
            self.active[ticket.role] += 1
            self.ordinary += int(not ticket.critical)
            self.peak_active = max(self.peak_active, sum(self.active.values()))
            return CapacityLease(self, ticket.role, ticket.critical)

    def _cancel(self, ticket):
        with self.condition:
            if ticket.waiting:
                self.tickets.remove(ticket)
                ticket.waiting = False
                self.condition.notify_all()

    def acquire(self, role, *, critical=False, timeout=None):
        if role not in self.role_limits:
            raise ValueError(f'Unknown runtime role: {role}')
        with self.condition:
            if not self.tickets and self._can_start(role, critical):
                self.active[role] += 1
                self.ordinary += int(not critical)
                self.peak_active = max(self.peak_active, sum(self.active.values()))
                return CapacityLease(self, role, critical)
        ticket = self.enqueue(role, critical=critical)
        deadline = time.monotonic() + (self.wait_seconds if timeout is None else max(0, timeout))
        try:
            with self.condition:
                while True:
                    lease = ticket.try_acquire()
                    if lease:
                        return lease
                    left = deadline - time.monotonic()
                    if left <= 0:
                        self.rejected[role] += 1
                        raise CapacityBusy(role, 'pressure' if self.pressure and not critical else 'queue_timeout')
                    self.condition.wait(left)
        finally:
            ticket.cancel()

    def reserve_bytes(self, kind, amount, *, timeout=0):
        if kind not in BYTE_KINDS or type(amount) is not int or amount < 0:
            raise ValueError('Invalid byte commitment kind or amount')
        deadline = time.monotonic() + max(0, timeout)
        with self.condition:
            while sum(self.bytes.values()) + amount > self.byte_limit:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise CapacityBusy(kind, 'byte_budget')
                self.condition.wait(left)
            self.bytes[kind] += amount
            self.peak_bytes = max(self.peak_bytes, sum(self.bytes.values()))
            return ByteLease(self, kind, amount)

    def set_pressure(self, active, reason=None):
        with self.condition:
            self.pressure = bool(active)
            self.pressure_reason = str(reason)[:64] if active and reason else None
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return {'settings': {'total_workers': self.total, 'reserved_workers': self.reserved,
                                 'queue_limit': self.queue_limit, 'queue_seconds': self.wait_seconds,
                                 'byte_limit': self.byte_limit, 'role_limits': dict(self.role_limits)},
                    'active': sum(self.active.values()), 'running': sum(self.running.values()),
                    'queued': len(self.tickets), 'bytes': sum(self.bytes.values()),
                    'peaks': {'active': self.peak_active, 'queued': self.peak_queued, 'bytes': self.peak_bytes},
                    'pressure': self.pressure, 'pressure_reason': self.pressure_reason,
                    'roles': {role: {'active': self.active[role], 'running': self.running[role],
                                     'queued': sum(t.role == role for t in self.tickets),
                                     'rejected': self.rejected[role]} for role in self.role_limits},
                    'byte_classes': {kind: self.bytes[kind] for kind in sorted(BYTE_KINDS)}}


_manager = None
_manager_lock = threading.Lock()


def get_capacity_manager():
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = CapacityManager.from_environment()
        return _manager


@contextmanager
def capacity_scope(role, *, critical=False, timeout=None, inherit=True):
    parent = current_capacity.get()
    if inherit and parent and not parent.released:
        yield parent
        return
    lease = get_capacity_manager().acquire(role, critical=critical, timeout=timeout)
    lease.mark_started()
    token = current_capacity.set(lease)
    try:
        yield lease
    finally:
        current_capacity.reset(token)
        lease.release()


def reserve_bytes(kind, amount, *, timeout=0):
    return get_capacity_manager().reserve_bytes(kind, amount, timeout=timeout)


def submit_capacity(executor, role, function, *args, critical=False, timeout=0, **kwargs):
    """Reserve before submitting. A cancelled queued future releases only itself."""
    lease = get_capacity_manager().acquire(role, critical=critical, timeout=timeout)
    context = contextvars.copy_context()

    def worker():
        lease.mark_started()
        token = current_capacity.set(lease)
        try:
            return function(*args, **kwargs)
        finally:
            current_capacity.reset(token)
            lease.release()

    try:
        future = executor.submit(context.run, worker)
    except BaseException:
        lease.release()
        raise
    future.add_done_callback(lambda done: lease.release() if done.cancelled() else None)
    weakref.finalize(future, lambda: lease.release() if not lease.started else None)
    return future
