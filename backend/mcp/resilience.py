"""Bounded MCP workers and deadlines independent of the Web thread pool.

Capacity belongs to the physical worker, not the HTTP request. A disconnect
therefore cannot release capacity while an exchange write is still running.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import hashlib
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass

import anyio

logger = logging.getLogger('MCPResilience')


def setting(name, default, minimum=0.001, maximum=300):
    try:
        value = float(os.getenv('MCP_' + name, default))
        return min(maximum, max(minimum, value)) if value == value else float(default)
    except (TypeError, ValueError):
        return float(default)


class BudgetExceeded(ValueError):
    pass


@dataclass
class Budget:
    deadline: float
    request_id: str
    kind: str
    stage: str = 'queue'
    write_started: bool = False
    scope_ref: str = '-'

    def remaining(self, stage=None):
        if stage:
            self.stage = stage
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BudgetExceeded(f'MCP budget exhausted at {self.stage}; request={self.request_id}')
        return remaining


current_budget = contextvars.ContextVar('mcp_budget', default=None)


def remaining(stage=None, default=30):
    budget = current_budget.get()
    return budget.remaining(stage) if budget else default


def bind_scope(profile_id):
    budget = current_budget.get()
    if budget and profile_id:
        budget.scope_ref = hashlib.sha256(str(profile_id).encode()).hexdigest()[:12]


@contextmanager
def stage(name):
    start = time.monotonic()
    budget = current_budget.get()
    remaining(name)
    outcome = 'completed'
    try:
        yield
    except BaseException as exc:
        outcome = type(exc).__name__
        raise
    finally:
        if budget:
            # Only internal labels and generated IDs; never exception text/args.
            logger.info('mcp_stage request_id=%s kind=%s scope_ref=%s stage=%s result=%s elapsed_ms=%d',
                        budget.request_id, budget.kind, budget.scope_ref, name, outcome,
                        (time.monotonic() - start) * 1000)


class WorkerPool:
    def __init__(self, kind, capacity, queue):
        self.kind, self.capacity, self.queue_limit = kind, capacity, queue
        self.executor = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix='mcp-' + kind)
        self.lock = threading.Lock()
        self.active = self.queued = 0

    def snapshot(self):
        with self.lock:
            return dict(active=self.active, queued=self.queued, capacity=self.capacity, queue_limit=self.queue_limit)

    async def run(self, function, *, label=None):
        start = time.monotonic()
        budget = Budget(start + setting(self.kind.upper() + '_BUDGET_SECONDS',
                                       {'read': 20, 'execute': 40, 'auth': 3}[self.kind]),
                        uuid.uuid4().hex, self.kind)
        queue_until = min(budget.deadline, start + setting('QUEUE_SECONDS', 2))
        with self.lock:
            if self.queued >= self.queue_limit and self.active >= self.capacity:
                logger.info('mcp_rejected request_id=%s kind=%s stage=queue reason=full active=%d queued=%d',
                            budget.request_id, self.kind, self.active, self.queued)
                raise ValueError(f'MCP {self.kind} busy: bounded queue is full')
            self.queued += 1
        acquired = False
        try:
            while True:
                budget.remaining('queue')
                with self.lock:
                    if self.active < self.capacity:
                        self.active += 1
                        self.queued -= 1
                        acquired = True
                        break
                if time.monotonic() >= queue_until:
                    logger.info('mcp_rejected request_id=%s kind=%s stage=queue reason=timeout',
                                budget.request_id, self.kind)
                    raise ValueError(f'MCP {self.kind} queue timeout; request={budget.request_id}')
                await anyio.sleep(min(0.01, max(0.001, queue_until - time.monotonic())))
        finally:
            if not acquired:
                with self.lock:
                    self.queued -= 1

        context = contextvars.copy_context()

        def worker():
            token = current_budget.set(budget)
            outcome = 'failed'
            try:
                with stage('worker'):
                    result = function()
                    outcome = result.get('status', 'returned') if isinstance(result, dict) else 'returned'
                    return result
            finally:
                current_budget.reset(token)
                with self.lock:
                    self.active -= 1
                logger.info('mcp_request request_id=%s kind=%s scope_ref=%s tool=%s result=%s elapsed_ms=%d active=%d queued=%d',
                            budget.request_id, self.kind, budget.scope_ref, label or self.kind, outcome,
                            (time.monotonic() - start) * 1000, self.snapshot()['active'], self.snapshot()['queued'])

        try:
            future = self.executor.submit(context.run, worker)
        except BaseException:
            with self.lock:
                self.active -= 1
            raise
        # Shielding avoids cancelling a queued/running concurrent future. The
        # worker's finally releases physical capacity even if this await ends.
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        with anyio.CancelScope(shield=True):
            return await asyncio.shield(wrapped)

_pools = {}
_pool_lock = threading.Lock()


def pool(kind):
    with _pool_lock:
        if kind not in _pools:
            capacity = int(setting(kind.upper() + '_CONCURRENCY', {'read': 4, 'execute': 2, 'auth': 2}[kind], 1, 32))
            queue = int(setting(kind.upper() + '_QUEUE_LIMIT', capacity * 2, 0, 128))
            _pools[kind] = WorkerPool(kind, capacity, queue)
        return _pools[kind]


async def run(kind, function, *, label=None):
    return await pool(kind).run(function, label=label)


def offloaded_auth(function):
    @functools.wraps(function)
    async def wrapped(*args, **kwargs):
        return await run('auth', functools.partial(function, *args, **kwargs), label=function.__name__)
    return wrapped


def diagnostics():
    from .store import storage_diagnostics
    return {**{kind: instance.snapshot() for kind, instance in list(_pools.items())},
            'storage': storage_diagnostics()}
