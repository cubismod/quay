# Reproducing the OTel + gevent crash (PROJQUAY-8902)

## Background

When `FEATURE_OTEL_TRACING` is enabled under gunicorn with gevent workers and
`preload_app = True`, the OpenTelemetry `BatchSpanProcessor` daemon thread
crashes during fork/shutdown. This causes worker failures and crash-looping pods
in production.

The bug requires three conditions:
1. `monkey.patch_all()` — threads become greenlets with different identity semantics
2. `preload_app = True` — app loads in master, `os.register_at_fork` callback registered
3. `BatchSpanProcessor` — spawns a daemon thread that gets reinit'd after fork

Local dev defaults hide this because `QUAY_HOTRELOAD=true` disables `preload_app`
and `gunicorn_local.py` lacks `monkey.patch_all()`.

## Setup

### 1. Add monkey-patching to local gunicorn config

Add these lines to the **top** of `conf/gunicorn_local.py` (before all other imports):

```python
from gevent import monkey

monkey.patch_all()
```

### 2. Enable OTel tracing

Add to `local-dev/stack/config.yaml`:

```yaml
FEATURE_OTEL_TRACING: true
```

### 3. Disable hot-reload

Start the environment with hot-reload disabled so `preload_app = True` is used:

```bash
QUAY_HOTRELOAD=false docker compose up -d
```

## Reproducing the runtime error (AssertionError)

Once the container starts, the `BatchSpanProcessor` greenlet will fail during
normal operation. Check logs:

```bash
docker logs quay-quay -f 2>&1 | grep -E "AssertionError|KeyError|OtelBatch|failed with"
```

Expected output:

```
gunicorn-registry stderr | AssertionError: (None, <callback at 0x... args=([],)>)
gunicorn-registry stderr | <callback at 0x... args=([],)> failed with AssertionError
```

## Reproducing the shutdown error (KeyError)

This is the error seen in production during version promotion. It occurs when
gunicorn-registry is gracefully terminated.

Find the gunicorn-registry master PID (lowest PID = master, workers are forked from it):

```bash
docker exec quay-quay sh -c 'for p in /proc/[0-9]*/cmdline; do pid=$(echo $p | cut -d/ -f3); printf "%s " "$pid"; cat "$p" 2>/dev/null | tr "\0" " "; echo; done | grep "gunicorn.*registry" | sort -n | head -1 | cut -d" " -f1'
```

Send SIGTERM to simulate a version promotion:

```bash
docker exec quay-quay sh -c 'kill -TERM $(for p in /proc/[0-9]*/cmdline; do pid=$(echo $p | cut -d/ -f3); printf "%s " "$pid"; cat "$p" 2>/dev/null | tr "\0" " "; echo; done | grep "gunicorn.*registry" | sort -n | head -1 | cut -d" " -f1)'
```

Expected output in logs:

```
gunicorn-registry stderr | File "/usr/lib64/python3.12/threading.py", line 1111, in _delete
gunicorn-registry stderr |     del _active[get_ident()]
gunicorn-registry stderr | KeyError: <greenlet-id>
```

## Root cause

`opentelemetry-sdk==1.32.1` includes `os.register_at_fork(after_in_child=self._at_fork_reinit)`
in `BatchSpanProcessor.__init__`. After gunicorn forks workers, `_at_fork_reinit` spawns
a new `threading.Thread` — but under `monkey.patch_all()` this becomes a greenlet whose
thread identity doesn't match what CPython's `threading._active` dict expects. The
`Condition` variable operations also fail because gevent's hub state is inconsistent
after fork.

The OTel SDK removed `register_at_fork` in versions >= 1.34.1, likely due to this
class of bug. Quay is pinned to 1.32.1.
