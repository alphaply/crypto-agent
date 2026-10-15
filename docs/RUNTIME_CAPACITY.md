# Runtime capacity

Web and scheduler enforce **separate process-local budgets**. Their container
limits and observed peaks must both fit the declared host capacity. See
[DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) for verified capacity profiles and
[RESOURCE_DIAGNOSTICS.md](RESOURCE_DIAGNOSTICS.md) for effective limits and
persistent incident evidence. Publishing an image does not change production
container limits.

Python 3.11 or newer is required for incremental SQLite blob reads. Size checks
open the stored blob and inspect its byte length before allocating its contents.
`length(CAST(payload AS BLOB))` and `substr(CAST(...))` are not safe substitutes:
SQLite can materialize the whole value inside its own VM.

## Worker admission

`runtime_capacity` counts a worker when it is committed to an executor, before
execution begins. `active` includes such commitments; `running` records workers
that have actually started. There is one total budget across Agent, MCP reads,
MCP writes/authentication, maintenance, recovery, memory, news, metadata, streams
and Paper. Dedicated executor limits remain additional bounds.

Sequential child work borrows its parent's live lease. Parallel children acquire
their own commitments before submission. Saturated parallel fan-out returns
`CapacityBusy` within its wait budget, rather than waiting indefinitely for a
parent-held slot. Eligible queued work advances in FIFO order; a blocked role
does not prevent another eligible role or reserved recovery from progressing.
There is no unbounded executor backlog behind this admission boundary.

`critical=True` permits original-result recovery and existing protection duties
to use reserved workers. Reserved workers are **included in the total**. Pausing
decisions does not release durable ownership or authorize another task to claim
an unresolved position. Pressure admission restricts ordinary work while bounded
critical maintenance remains available. All work still obeys its role limit.

A caller deadline/disconnect does not release a running worker. MCP returns the
timeout stage while the physical worker keeps its lease until completion. A
successfully cancelled, demonstrably unstarted queued future releases only its
own commitment. Financial recovery uses original identities; neither cancellation
nor a worker disappearing authorizes replay or continuation of an unattempted
batch tail. An authoritative trading ACK returns directly; it does not acquire
another read slot for compulsory post-submit order/position verification.

MCP request budgets include local/global queues and dependencies. A passed
absolute monotonic parent `deadline` further bounds the pool budget; nested
`current_budget` deadlines are inherited. Paper maintenance uses this to preserve
one round deadline across individual calls. Authentication runs off the event
loop. Health and static pages use lightweight routes independent of exchange
worker waits.
Paper original-receipt/batch recovery uses the dedicated internal `recovery`
pool, which acquires a critical recovery lease from the same total. It does not
queue behind ordinary read-pool workers before attempting reserved admission.

Scheduler Agent sizing uses the smaller of the configured Agent count and the
ceiling of readable effective CPU quota/affinity. Missing quota/affinity remains
unavailable in diagnostics; it is not replaced with a purported measured host
CPU count. The verified profile/container CPU limit remains the operational
capacity boundary.

## Public metadata, clients and observations

Every backend `load_markets` call goes through `public_catalogue`. Public cache
keys distinguish venue, market type, public endpoint maps, precision mode and
public options/version. Credentials and credential rotation do not create public
metadata identities. Different public options/endpoints intentionally remain
different keys. Web and scheduler each load their own process-local copy.

Cold loading and refresh are single-flight per key. A dedicated anonymous CCXT
loader obtains public metadata. Its raw response body is bounded before JSON
parsing. Compact snapshots retain normalized fields and native fields required
by the pinned CCXT implementation (`orderTypes`, filters, OKX `uly`/`instFamily`,
Binance `pair`, and related execution capabilities). Unused native payload is
discarded. Compaction checks retained byte limits before copying each row.

Each mutable account client gets independent compact markets, currencies,
derived CCXT indexes, time adjustment and credentials. Mutating one client cannot
mutate another. Account coordinator keys remain credential-sensitive because
pacing and account mutual exclusion must follow the actual account credentials.
Client slots have a separate entry cap and a base byte commitment in addition to
their metadata copies, so a tiny directory cannot admit unbounded SDK clients.

Byte commitments include retained compact cache entries, mutable consumers,
refresh old/new overlap, parsed loader/snapshot space, bounded HTTP buffers,
quotes and coordinators. A refresh reserves its peak **before** loading. Its new
retained snapshot transfers bytes out of that already-admitted reservation, so
two simultaneous refreshes cannot both deadlock on a second allocation at the
aggregate limit. Raw loader size is constrained by refresh space; retained
snapshot size is constrained separately by the catalogue limit.

These are conservative Python-object/commitment estimates, not RSS. Recursive
size traversal counts each object identity once and stops at its bound. Real RSS,
cgroup current/peak, CPU throttling and allocator retention are measured
independently during dual-container verification.

Public quotes also use bounded single-flight refresh. A failed refresh preserves
the old observation time and does not publish a new success timestamp. Expired
public observations are not current private-account evidence or write admission.
Metadata refresh failures retain source/observation information on the exception
and in bounded cache diagnostics. Reads that require current metadata fail
explicitly; the cache does not silently authorize trades with stale metadata.

Clients release their consumer/account references on close. Idle cache entries,
quote refresh locks and coordinators are retired on maintenance and later
access. Entry/byte caps can reclaim unreferenced LRU objects earlier. Active
refreshes, locks and account users are pinned. Credential lookup pins the
coordinator under the same lock as lookup, preventing retirement races from
creating duplicate account locks. Deleting a task does not delete durable trade
receipts, ownership evidence or unknown submission identities.

## Bounded protection and ownership scans

`protection_plan_activity` is a small indexed projection of original plan state,
size and observation time. Triggers keep new writes atomic with the original
plan. Legacy indexing advances with a persistent bounded cursor. DONE plans are
excluded at the storage index before polling. Oversized/malformed originals stay
UNKNOWN; incomplete legacy indexing remains explicit, and original JSON is
retained. The projection never changes a financial fact or releases ownership.
Recent completed-plan metadata has a separate bounded cursor so late-fill
accounting can continue for paused tasks without loading DONE plan JSON or
returning completed plans to protection polling.

Execution ownership migration uses durable phases/cursors and disk-backed
regular/conditional identity evidence. It scans bounded pages of events, current
plans and legacy orders, retaining a fixed snapshot of each accepted source.
Only after all source facts are examined can ambiguous algo aliases be retired
and candidate links published. Positive regular-order evidence on later pages
therefore cannot be lost. Work resumes after process restart. Oversized sources
remain unprocessed with an incomplete result; synchronization performs no
exchange reads until ownership indexing is complete. Raw stream fills may be
saved without assigning a new owner while historical ownership is incomplete.
Default execution context exposes that incompleteness instead of treating a
partial historical index as a complete reconciliation.

## Binance spot metadata response sizing

On 2026-10-15 the production `/api/v3/exchangeInfo` response contained 3,729 symbols: 322,077 compressed bytes, but 17,767,621 decoded bytes. The HTTP bound correctly applies to decoded content and rejected it at 8 MiB. Public Binance spot refreshes now request `showPermissionSets=false`, which leaves the symbol scope unchanged and omits a field neither CCXT nor the retained catalogue consumes. The measured decoded body becomes 6,759,438 bytes; the 8 MiB HTTP limit remains enforced. Futures requests do not receive the spot-only parameter.

The actual CCXT raw maps occupied 34,819,893 resident bytes and the retained compact maps 32,641,483 bytes, exceeding the former 64 MiB refresh budget together. Defaults now reserve 128 MiB for a refresh and allow 512 MiB of aggregate commitments, including warm old/new catalogues and client copies. Owned loader maps are cleared before releasing refresh space. Production concurrent spot/swap cold loads and expired refreshes succeeded with a measured commitment peak of 495,366,640 bytes and process peak RSS of 303,536 KiB in a separate diagnostic process. This is dependency sizing evidence, not a replacement for isolated resource acceptance or authenticated MCP verification.

Use the frozen `scripts/capacity/metadata-fix-v13.json` for local resource acceptance and `scripts/capacity/production-metadata-fix-v1.json` for the upgraded target host. Historical v12 reports and profiles remain unchanged.

## Settings

All values below are finite **calibration defaults**, not verified production
sizing. A frozen measured capacity profile supplies the accepted deployment
values. Invalid numeric/role configuration raises a clear error.

| Setting | Default | Meaning |
| --- | ---: | --- |
| `RUNTIME_CAPACITY_TOTAL_WORKERS` | 8 | Process-wide committed workers |
| `RUNTIME_CAPACITY_RESERVED_WORKERS` | 2 | Critical reserve included in total; must be smaller than total |
| `RUNTIME_CAPACITY_QUEUE_LIMIT` | 16 | Process-wide waiting tickets; 0 allows immediate admission only |
| `RUNTIME_CAPACITY_QUEUE_SECONDS` | 2 | Admission wait budget |
| `RUNTIME_CAPACITY_BYTES` | 536870912 | Total admitted retained/transient bytes |
| `RUNTIME_CAPACITY_ROLE_LIMITS` | `{}` | JSON overrides for the fixed role map |
| `RUNTIME_CATALOGUE_BYTES` | 67108864 | Aggregate retained compact catalogue bytes, including replacement overlap |
| `RUNTIME_CATALOGUE_REFRESH_BYTES` | 134217728 | One raw-loader/compact refresh peak; default is twice retained catalogue cap and at least 8× response cap |
| `RUNTIME_CATALOGUE_ENTRIES` | 32 | Retained/in-flight public keys |
| `RUNTIME_HTTP_RESPONSE_BYTES` | 8388608 | Bounded streamed response body; separate parse amplification commitment |
| `RUNTIME_CACHE_IDLE_SECONDS` | 300 | Idle retirement for unused runtime resources |
| `RUNTIME_CATALOGUE_TTL_SECONDS` | 300 | Metadata refresh TTL |
| `RUNTIME_CATALOGUE_RETRY_SECONDS` | 2 | Failed metadata refresh backoff |
| `RUNTIME_COORDINATOR_ENTRIES` | 128 | Account coordinator key cap |
| `RUNTIME_CLIENT_ENTRIES` | 32 | Live consumer/client slots, including failed initializers until close/GC |
| `RUNTIME_CLIENT_BASE_BYTES` | 262144 | Base SDK/client commitment in addition to metadata copies |
| `RUNTIME_QUOTE_ENTRIES` | 256 | Public observations/refresh-lock key cap |
| `RUNTIME_QUOTE_BYTES` | 8388608 | Retained quote and copy commitment cap |
| `RUNTIME_QUOTE_REFRESH_BYTES` | 16777216 | One quote refresh parse/clone reservation |
| `RUNTIME_SCAN_BATCH_SIZE` | 32 | Protection/ownership references per bounded page |
| `RUNTIME_MAX_RECORD_BYTES` | 4194304 | Full original record decode limit |
| `RUNTIME_INDEX_SCAN_SECONDS` | 0.25 | Ownership indexing time budget checked between records, plus page-size bound |
| `MCP_RECOVERY_CONCURRENCY` | 2 | Dedicated internal original-result recovery workers within total/role budget |
| `MCP_RECOVERY_QUEUE_LIMIT` | 4 | Bounded internal recovery queue |
| `MCP_RECOVERY_BUDGET_SECONDS` | 20 | Recovery request budget, further bounded by its parent round |

Default role limits are Agent 2, MCP read 4, MCP execute 2, MCP auth 2,
maintenance 2, recovery 2, memory 2, news 4, metadata 2, stream 2 and Paper 2.
They are ceilings within the aggregate budget. MCP pool concurrency/queue and
request budgets add their own constraints; fractional worker counts, invalid
numbers and out-of-range values are rejected rather than silently clamped.

For example, with total 6 and reserve 2, ordinary work can commit at most 4
workers. Two critical jobs may use the remaining workers, subject to role caps;
the total is still at most 6. If a timed-out ordinary worker is still running,
it remains in those four commitments.

Run focused offline verification with Python 3.11:

```powershell
uv run --python 3.11 python -m pytest -q tests/test_runtime_capacity.py tests/test_public_catalogue.py tests/test_mcp_capacity_integration.py tests/test_mcp_resilience.py tests/test_protection_scan.py tests/test_execution_plan_index.py
```

These tests verify admission/lifetime, single-flight loading, actual CCXT
precision and contract units, isolation, cache retirement, parent deadlines,
bounded active-plan migration and cross-page alias evidence. The capacity
verification reports provide actual dual-container peak and SLO evidence; unit
tests alone do not establish a safe production memory/swap/CPU allocation.

News source parsing holds `RUNTIME_NEWS_WORK_BYTES` (32 MiB by default) within
the aggregate byte budget. The three memory/failure/digest caches together use
at most `RUNTIME_NEWS_CACHE_BYTES` (16 MiB); each cache and the live source-lock
map use `RUNTIME_NEWS_CACHE_ENTRIES` (64). Source locks retire after the last
waiting/active user. Cache commitments release when their actual retained
values are no longer referenced. Busy fetching preserves durable stale facts
and explicit source health rather than creating additional workers.

Execution and announcement streams reserve `RUNTIME_STREAM_BYTES` (8 MiB)
before starting each physical thread. Execution batches contain at most
`RUNTIME_STREAM_ITEMS` (1000) trades. Idle receive checks allow shutdown;
reconnect backoff is interruptible. Credential replacement first stops the old
worker and waits for it to finish; only its `finally` releases commitments.
Deleted/unused configurations are retired by maintenance. These commitments
estimate work sets; cgroup/RSS observations independently measure actual memory.
