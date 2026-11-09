# Background worker scheduler

The Gmail and AI workers run continuously with RQ's blocking dequeue and do not
start schedulers. A single `rq-scheduler` Vercel Daemon runs
`python -m app.worker.scheduler` and manages both queues. RQ 3.0.0 acquires a
Redis scheduler lock per queue and scans each managed queue's scheduled-job
registry.

The scheduler interval is configured with
`RQ_SCHEDULER_INTERVAL_SECONDS` and defaults to 90 seconds. Delayed retries
remain enabled with `Retry(max=2, interval=[15, 60])`. Since RQ checks scheduled
jobs once per scheduler interval, a retry may be promoted up to approximately
90 seconds after its due time, plus processing and service/network delays.
Choose a shorter interval if tighter retry timing is required; that increases
idle Redis command usage.

Vercel is configured for one scheduler instance in the `bom1` region. RQ's
per-queue Redis locks prevent a second scheduler with a different process
identity from managing the same queue concurrently. Graceful shutdown releases
those locks. After an abrupt termination, a lock remains until its TTL expires
(the interval plus 60 seconds); RQ 3.0.0 may take longer to retry lock
acquisition because it uses a 10-minute scheduler fallback period. During that
recovery window, scheduled retries can be later than the nominal 90-second
interval. Confirm Vercel's daemon termination/restart behavior and monitor
scheduler startup and queue-lock logs after deployment.
