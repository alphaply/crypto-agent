"""MCP worker ownership and deadlines independent of the Web thread pool.

Capacity belongs to the physical worker, not the HTTP request. A disconnect
therefore cannot release capacity while an exchange write is still running.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import hashlib
import logging
import math
import os
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass

import anyio
from backend.utils.runtime_capacity import get_capacity_manager, current_capacity, CapacityBusy

logger = logging.getLogger('MCPResilience')
BUDGET_DEFAULTS = {'read': 20, 'execute': 40, 'auth': 3, 'recovery': 20}
POOL_DEFAULTS = {'read': 4, 'execute': 2, 'auth': 2, 'recovery': 2}


def capacity_role(kind):
    return 'recovery' if kind == 'recovery' else 'mcp_' + kind


def setting(name, default, minimum=0.001, maximum=300):
    try:
        value = float(os.getenv('MCP_' + name, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f'MCP_{name} must be a finite number in [{minimum}, {maximum}]') from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f'MCP_{name} must be a finite number in [{minimum}, {maximum}]')
    if name.endswith(('_CONCURRENCY', '_QUEUE_LIMIT')) and not value.is_integer():
        raise ValueError(f'MCP_{name} must be an integer')
    return value


class BudgetExceeded(ValueError):
    pass


@dataclass
class Budget:
    deadline: float | None
    request_id: str
    kind: str
    stage: str = 'queue'
    write_started: bool = False
    scope_ref: str = '-'

    def remaining(self, stage=None, default=30):
        if stage:
            self.stage = stage
        if self.deadline is None:
            return default
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BudgetExceeded(f'MCP budget exhausted at {self.stage}; request={self.request_id}')
        return remaining


current_budget = contextvars.ContextVar('mcp_budget', default=None)


def remaining(stage=None, default=30):
    budget = current_budget.get()
    return budget.remaining(stage, default) if budget else default


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
    category = ('metadata' if 'metadata' in name or name == 'initialization' else
                'authentication' if 'auth' in name else 'persistence' if 'persist' in name else
                'queue' if 'queue' in name else 'exchange' if 'exchange' in name or 'account_' in name else
                'execution')
    from backend.utils.resource_diagnostics import record_resource_stage
    identities = {'request_id': budget.request_id, 'role': capacity_role(budget.kind)} if budget else {}
    record_resource_stage(category, 'started', **identities)
    try:
        yield
    except BaseException as exc:
        outcome = type(exc).__name__
        record_resource_stage(category, 'timeout' if isinstance(exc, (BudgetExceeded, TimeoutError)) else
                              'busy' if isinstance(exc, CapacityBusy) else 'failed', **identities)
        raise
    else:
        record_resource_stage(category, 'completed', **identities)
    finally:
        if budget:
            # Only internal labels and generated IDs; never exception text/args.
            logger.info('mcp_stage request_id=%s kind=%s scope_ref=%s stage=%s result=%s elapsed_ms=%d',
                        budget.request_id, budget.kind, budget.scope_ref, name, outcome,
                        (time.monotonic() - start) * 1000)


class WorkerPool:
    def __init__(self, kind, capacity, queue):
        if kind not in BUDGET_DEFAULTS or type(capacity) is not int or capacity < 1 or type(queue) is not int or queue < 0:
            raise ValueError('Invalid bounded MCP worker pool')
        self.kind, self.capacity, self.queue_limit = kind, capacity, queue
        self.executor = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix='mcp-' + kind)
        self.lock = threading.Lock()
        self.active = self.queued = 0

    def snapshot(self):
        with self.lock:
            return dict(active=self.active, queued=self.queued, capacity=self.capacity,
                        queue_limit=self.queue_limit, limits_enabled=get_capacity_manager().limits_enabled)

    def _submit_monitor_only(self, function):
        """Start a physical worker without an executor concurrency/queue gate."""
        future = Future()

        def invoke():
            if not future.set_running_or_notify_cancel():
                return
            try:
                result = function()
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)

        # A non-daemon thread keeps the same process-exit ownership as executor
        # workers. The request's cancellation cannot terminate or resubmit it.
        thread = threading.Thread(target=invoke, name='mcp-' + self.kind, daemon=False)
        thread.start()
        return future

    async def run(self, function, *, label=None, deadline=None):
        start = time.monotonic()
        timeout_key = self.kind.upper() + '_TIMEOUT_SECONDS'
        legacy = os.getenv('MCP_' + self.kind.upper() + '_BUDGET_SECONDS', BUDGET_DEFAULTS[self.kind])
        own_deadline = start + setting(timeout_key, legacy)
        if deadline is not None:
            if not isinstance(deadline, (float, int)) or not math.isfinite(deadline):
                raise ValueError('Parent deadline must be a finite monotonic timestamp')
            own_deadline = min(own_deadline, deadline)
        # Response timeout belongs to the awaiting caller. The physical worker
        # finishes its observations without inheriting a cumulative deadline.
        budget = Budget(None, uuid.uuid4().hex, self.kind)
        budget.remaining('queue')
        queue_until = min(own_deadline, start + setting('QUEUE_SECONDS', 2))
        manager = get_capacity_manager()
        limits_enabled = manager.limits_enabled
        acquired = False
        lease = ticket = None
        with self.lock:
            if not limits_enabled:
                lease = manager.acquire(capacity_role(self.kind), critical=self.kind == 'recovery', timeout=0)
                self.active += 1
                acquired = True
            elif self.active < self.capacity:
                try:
                    lease = manager.acquire(capacity_role(self.kind), critical=self.kind == 'recovery', timeout=0)
                except CapacityBusy:
                    pass
                if lease:
                    self.active += 1
                    acquired = True
            if not acquired and self.queued >= self.queue_limit:
                logger.info('mcp_rejected request_id=%s kind=%s stage=queue reason=full active=%d queued=%d',
                            budget.request_id, self.kind, self.active, self.queued)
                raise ValueError(f'MCP {self.kind} busy: bounded queue is full')
            if not acquired:
                self.queued += 1
        try:
            if not acquired:
                ticket = manager.enqueue(capacity_role(self.kind), critical=self.kind == 'recovery')
            while not acquired:
                budget.remaining('queue')
                with self.lock:
                    if ticket:
                        ticket.ready = self.active < self.capacity
                    if self.active < self.capacity:
                        lease = ticket.try_acquire()
                        if lease:
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
            if ticket:
                ticket.cancel()
            if not acquired:
                with self.lock:
                    self.queued -= 1

        context = contextvars.copy_context()

        def worker():
            token = current_budget.set(budget)
            capacity_token = current_capacity.set(lease)
            lease.mark_started()
            outcome = 'failed'
            try:
                with stage('worker'):
                    result = function()
                    outcome = result.get('status', 'returned') if isinstance(result, dict) else 'returned'
                    return result
            finally:
                current_capacity.reset(capacity_token)
                lease.release()
                current_budget.reset(token)
                with self.lock:
                    self.active -= 1
                logger.info('mcp_request request_id=%s kind=%s scope_ref=%s tool=%s result=%s elapsed_ms=%d active=%d queued=%d',
                            budget.request_id, self.kind, budget.scope_ref, label or self.kind, outcome,
                            (time.monotonic() - start) * 1000, self.snapshot()['active'], self.snapshot()['queued'])

        try:
            if limits_enabled:
                future = self.executor.submit(context.run, worker)
            else:
                future = self._submit_monitor_only(functools.partial(context.run, worker))
        except BaseException:
            lease.release()
            with self.lock:
                self.active -= 1
            raise
        # Shielding avoids cancelling a queued/running concurrent future. The
        # worker's finally releases physical capacity even if this await ends.
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        try:
            with anyio.CancelScope(shield=True):
                return await asyncio.wait_for(asyncio.shield(wrapped), max(.001, own_deadline - time.monotonic()))
        except asyncio.TimeoutError as exc:
            if wrapped.done() and not wrapped.cancelled():
                # Preserve a dependency's own TimeoutError; only an unfinished
                # worker demonstrates expiry of this outer request budget.
                return await wrapped
            from backend.utils.resource_diagnostics import record_resource_stage
            record_resource_stage('execution', 'timeout', role=capacity_role(self.kind), request_id=budget.request_id)
            raise BudgetExceeded(f'MCP response timeout at {budget.stage}; request={budget.request_id}; '
                                 'worker retains capacity until completion') from exc

_pools = {}
_pool_lock = threading.Lock()


def pool(kind):
    with _pool_lock:
        if kind not in _pools:
            capacity = int(setting(kind.upper() + '_CONCURRENCY', POOL_DEFAULTS[kind], 1, 32))
            queue = int(setting(kind.upper() + '_QUEUE_LIMIT', capacity * 2, 0, 128))
            _pools[kind] = WorkerPool(kind, capacity, queue)
        return _pools[kind]


async def run(kind, function, *, label=None, deadline=None):
    return await pool(kind).run(function, label=label, deadline=deadline)


def offloaded_auth(function):
    @functools.wraps(function)
    async def wrapped(*args, **kwargs):
        return await run('auth', functools.partial(function, *args, **kwargs), label=function.__name__)
    return wrapped


def diagnostics():
    from .store import storage_diagnostics
    return {**{kind: instance.snapshot() for kind, instance in list(_pools.items())},
            'storage': storage_diagnostics(), 'runtime': get_capacity_manager().snapshot()}
