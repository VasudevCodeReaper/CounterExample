"""Sandbox worker process for Python implementations.

Run as ``python -I _python_worker.py``: isolated mode, no user site-packages,
and nothing imported from the ``counterexample`` package, so code under test
cannot shadow or monkey-patch the engine.  The parent talks to the worker over
JSON lines:

    parent -> {"op": "setup", "code": ..., "entrypoint": ..., "mode": "function"}
    worker -> {"ok": true, "entry": "solve"}
    parent -> {"op": "call", "id": 7, "args": [...], "kwargs": {...}}
    worker -> {"id": 7, "ok": true, "out": ..., "ns": 1234}

Other ops: ``profile`` (repeat timings + tracemalloc peak), ``trace`` (line-level
execution trace with changed locals) and ``exit``.  The user's ``print`` output
is captured per call and never reaches the protocol stream.
"""

import asyncio
import base64
import dataclasses
import inspect
import io
import json
import math
import os
import reprlib
import sys
import threading
import time
import traceback
from fractions import Fraction

MAX_DEPTH = 200
MAX_ITER_ITEMS = 1_000_000
STDOUT_LIMIT = 2000
WORKER_STACK_SIZE = 16 * 1024 * 1024


async def _async_noop():
    """Provide a baseline for scheduling one coroutine on the worker loop."""

    return None

# --------------------------------------------------------------------------- #
# Private copy of counterexample.codec.encode/decode (keep in sync).
# --------------------------------------------------------------------------- #


def _sort_key(v):
    return repr(v)


def encode(value, _depth=0):
    if _depth > MAX_DEPTH:
        return {"$repr": reprlib.repr(value)}
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if math.isnan(value):
            return {"$float": "nan"}
        if math.isinf(value):
            return {"$float": "inf" if value > 0 else "-inf"}
        return value
    d = _depth + 1
    if isinstance(value, list):
        return [encode(v, d) for v in value]
    if isinstance(value, tuple):
        return {"$tuple": [encode(v, d) for v in value]}
    if isinstance(value, dict):
        if all(isinstance(k, str) and not k.startswith("$") for k in value):
            return {k: encode(v, d) for k, v in value.items()}
        return {"$dict": [[encode(k, d), encode(v, d)] for k, v in value.items()]}
    if isinstance(value, frozenset):
        return {"$frozenset": [encode(v, d) for v in sorted(value, key=_sort_key)]}
    if isinstance(value, set):
        return {"$set": [encode(v, d) for v in sorted(value, key=_sort_key)]}
    if isinstance(value, (bytes, bytearray)):
        return {"$bytes": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, complex):
        return {"$complex": [encode(value.real, d), encode(value.imag, d)]}
    if isinstance(value, Fraction):
        return {"$fraction": [value.numerator, value.denominator]}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = {f.name: encode(getattr(value, f.name), d) for f in dataclasses.fields(value)}
        return {"$obj": type(value).__name__, "fields": fields}
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            return encode(tolist(), d)
        except Exception:
            pass
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, dict) and not callable(value):
        fields = {str(k): encode(v, d) for k, v in attrs.items() if not str(k).startswith("_")}
        return {"$obj": type(value).__name__, "fields": fields}
    return {"$repr": reprlib.repr(value)}


def _hashable(v):
    if isinstance(v, list):
        return tuple(_hashable(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted(((_hashable(k), _hashable(x)) for k, x in v.items()), key=repr))
    if isinstance(v, set):
        return frozenset(v)
    return v


def decode(obj):
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, list):
        return [decode(v) for v in obj]
    if isinstance(obj, dict):
        if obj:
            tag = next(iter(obj))
            if isinstance(tag, str) and tag.startswith("$"):
                body = obj[tag]
                if tag == "$float":
                    return float(body)
                if tag == "$tuple":
                    return tuple(decode(v) for v in body)
                if tag == "$dict":
                    return {_hashable(decode(k)): decode(v) for k, v in body}
                if tag == "$set":
                    return {_hashable(decode(v)) for v in body}
                if tag == "$frozenset":
                    return frozenset(_hashable(decode(v)) for v in body)
                if tag == "$bytes":
                    return base64.b64decode(body)
                if tag == "$complex":
                    return complex(decode(body[0]), decode(body[1]))
                if tag == "$fraction":
                    return Fraction(body[0], body[1])
        return {k: decode(v) for k, v in obj.items()}
    return obj


# --------------------------------------------------------------------------- #
# Protocol plumbing
# --------------------------------------------------------------------------- #


class Protocol:
    def __init__(self):
        # Keep private handles on the real stdin/stdout, then point fds 0/1 at
        # devnull so user code can neither read commands nor corrupt replies.
        self.inp = os.fdopen(os.dup(0), "rb")
        self.out = os.fdopen(os.dup(1), "wb")
        null_r = os.open(os.devnull, os.O_RDONLY)
        null_w = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null_r, 0)
        os.dup2(null_w, 1)
        sys.stdin = io.StringIO("")
        self.lock = threading.Lock()

    def read(self):
        line = self.inp.readline()
        if not line:
            return None
        return json.loads(line.decode("utf-8"))

    def send(self, msg):
        data = json.dumps(msg, separators=(",", ":"), allow_nan=False, default=repr).encode("utf-8") + b"\n"
        with self.lock:
            self.out.write(data)
            self.out.flush()


def _materialize(result):
    """Turn lazy iterators (map, filter, generators) into lists."""
    if isinstance(result, (list, tuple, dict, set, frozenset, str, bytes, bytearray)) or result is None:
        return result
    if hasattr(result, "__next__") and hasattr(result, "__iter__"):
        out = []
        for i, item in enumerate(result):
            if i >= MAX_ITER_ITEMS:
                break
            out.append(item)
        return out
    return result


def _invoke(target, args, kwargs):
    """Call a target and resolve coroutine or other awaitable results.

    ``AsyncFunctionDef`` entrypoints are valid Python entrypoints, but invoking
    one produces a coroutine object.  The target owns one reusable event loop,
    so scheduling setup is not repeated for every arena sample.  Its calibrated
    scheduling cost is returned separately from the implementation timing.
    """
    result = target.fn(*args, **kwargs)
    if inspect.isawaitable(result):
        return target.resolve_awaitable(result), target.await_overhead_ns
    return result, 0


def _exc_info(exc, code_name):
    tb = traceback.extract_tb(exc.__traceback__)
    frames = [f for f in tb if f.filename == code_name] or tb[-1:]
    line = frames[-1].lineno if frames else None
    text = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    return {
        "exc": type(exc).__name__,
        "msg": text[:1000],
        "line": line,
        "tb": "".join(traceback.format_list(frames[-6:]))[-3000:],
    }


class Target:
    def __init__(self, setup):
        self.mode = setup.get("mode", "function")
        self.code_name = setup.get("filename", "<candidate>")
        self.namespace = {"__name__": "__counterexample__", "__builtins__": __builtins__}
        self.client = None
        self.base_url = None
        self._async_loop = None
        self.await_overhead_ns = 0
        code = setup["code"]
        self.source = code
        if self.mode == "fastapi" and code.strip().startswith(("http://", "https://")):
            self.base_url = code.strip().rstrip("/")
            self.fn = self._http_url_call
            self.name = self.base_url
            return
        compiled = compile(code, self.code_name, "exec")
        sink = io.StringIO()
        old = sys.stdout
        sys.stdout = sink
        try:
            exec(compiled, self.namespace)
        finally:
            sys.stdout = old
        if self.mode == "fastapi":
            self._setup_fastapi(setup.get("entrypoint"))
        else:
            self.fn, self.name = self._resolve(setup.get("entrypoint"), setup.get("role", "candidate"))
            if inspect.iscoroutinefunction(self.fn) or getattr(self.fn, "_counterexample_async", False):
                self._prepare_async_loop()

    def _prepare_async_loop(self):
        if self._async_loop is not None:
            return
        loop = asyncio.new_event_loop()
        samples = []
        for _ in range(7):
            t0 = time.perf_counter_ns()
            loop.run_until_complete(_async_noop())
            samples.append(time.perf_counter_ns() - t0)
        samples.sort()
        self._async_loop = loop
        self.await_overhead_ns = samples[len(samples) // 2]

    def resolve_awaitable(self, awaitable):
        self._prepare_async_loop()
        assert self._async_loop is not None
        return self._async_loop.run_until_complete(awaitable)

    def close(self):
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
        if self._async_loop is None:
            return
        try:
            pending = asyncio.all_tasks(self._async_loop)
            for task in pending:
                task.cancel()
            if pending:
                self._async_loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        finally:
            self._async_loop.close()
            self._async_loop = None

    # -- entrypoint resolution -------------------------------------------- #
    def _resolve(self, entry, role):
        ns = self.namespace
        if entry:
            if "." in entry:
                cls_name, meth = entry.split(".", 1)
                cls = ns.get(cls_name)
                if cls is None:
                    raise LookupError(f"class {cls_name!r} not found")
                return self._method_caller(cls, meth), entry
            fn = ns.get(entry)
            if fn is None:
                sol = ns.get("Solution")
                if isinstance(sol, type) and hasattr(sol, entry):
                    return self._method_caller(sol, entry), f"Solution.{entry}"
                raise LookupError(f"function {entry!r} not found in {role} code")
            if isinstance(fn, type):
                raise LookupError(f"{entry!r} is a class; use 'Class.method' as the entrypoint")
            return fn, entry
        funcs = [
            (k, v) for k, v in ns.items()
            if callable(v) and not isinstance(v, type) and getattr(v, "__module__", None) == "__counterexample__"
            and not k.startswith("_")
        ]
        preferred = {
            "oracle": ["oracle", "brute_force", "bruteforce", "reference", "naive", "solve", "solution"],
            "candidate": ["candidate", "optimized", "fast", "solve", "solution"],
        }.get(role, [])
        for name in preferred:
            for k, v in funcs:
                if k == name:
                    return v, k
        if len(funcs) == 1:
            return funcs[0][1], funcs[0][0]
        sol = ns.get("Solution")
        if isinstance(sol, type):
            methods = [m for m, v in vars(sol).items() if callable(v) and not m.startswith("_")]
            if methods:
                return self._method_caller(sol, methods[0]), f"Solution.{methods[0]}"
        if funcs:
            # Helpers are usually defined first; the entrypoint last.
            return funcs[-1][1], funcs[-1][0]
        raise LookupError(f"no function found in {role} code; pass an explicit entrypoint")

    @staticmethod
    def _method_caller(cls, meth):
        if not hasattr(cls, meth):
            raise LookupError(f"{cls.__name__} has no method {meth!r}")

        def call(*args, **kwargs):
            return getattr(cls(), meth)(*args, **kwargs)

        call.__name__ = meth
        call._counterexample_async = inspect.iscoroutinefunction(getattr(cls, meth))
        return call

    # -- fastapi ----------------------------------------------------------- #
    def _setup_fastapi(self, entry):
        from fastapi.testclient import TestClient  # imported lazily: optional dependency

        name = entry or "app"
        app = self.namespace.get(name)
        if app is None:
            raise LookupError(f"FastAPI app {name!r} not found (define `app = FastAPI()` or pass entrypoint)")
        if callable(app) and not hasattr(app, "router"):
            app = app()  # factory function
        self.client = TestClient(app, raise_server_exceptions=False)
        self.fn = self._testclient_call
        self.name = name

    @staticmethod
    def _normalize_response(status, headers, body_bytes):
        ctype = headers.get("content-type", "")
        out = {"status_code": status}
        if "json" in ctype:
            try:
                out["json"] = json.loads(body_bytes.decode("utf-8"))
                return out
            except ValueError:
                pass
        out["text"] = body_bytes.decode("utf-8", errors="replace")
        return out

    def _testclient_call(self, request):
        r = self.client.request(
            request.get("method", "GET"),
            request.get("path", "/"),
            params=request.get("params") or None,
            json=request.get("json"),
            headers=request.get("headers") or None,
        )
        return self._normalize_response(r.status_code, {k.lower(): v for k, v in r.headers.items()}, r.content)

    def _http_url_call(self, request):
        import urllib.error
        import urllib.parse
        import urllib.request

        url = self.base_url + request.get("path", "/")
        if request.get("params"):
            url += "?" + urllib.parse.urlencode(request["params"], doseq=True)
        data = None
        headers = dict(request.get("headers") or {})
        if request.get("json") is not None:
            data = json.dumps(request["json"]).encode("utf-8")
            headers.setdefault("content-type", "application/json")
        req = urllib.request.Request(url, data=data, method=request.get("method", "GET"), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return self._normalize_response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
        except urllib.error.HTTPError as err:
            return self._normalize_response(err.code, {k.lower(): v for k, v in err.headers.items()}, err.read())


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #


def op_call(target, msg):
    args = decode(msg.get("args", []))
    kwargs = decode(msg.get("kwargs", {}))
    sink = io.StringIO()
    old = sys.stdout
    sys.stdout = sink
    t0 = time.perf_counter_ns()
    try:
        result, overhead_ns = _invoke(target, args, kwargs)
        result = _materialize(result)
        ns = max(1, time.perf_counter_ns() - t0 - overhead_ns)
    except BaseException as exc:  # noqa: BLE001 - user code may raise anything
        ns = time.perf_counter_ns() - t0
        sys.stdout = old
        if isinstance(exc, KeyboardInterrupt):
            raise
        reply = {"id": msg.get("id"), "ok": False, "ns": ns, **_exc_info(exc, target.code_name)}
        printed = sink.getvalue()
        if printed:
            reply["stdout"] = printed[-STDOUT_LIMIT:]
        return reply
    finally:
        sys.stdout = old
    reply = {"id": msg.get("id"), "ok": True, "ns": ns, "out": encode(result)}
    if result is None or msg.get("observe_args"):
        reply["args_after"] = encode(list(args))
    printed = sink.getvalue()
    if printed and msg.get("capture", True):
        reply["stdout"] = printed[-STDOUT_LIMIT:]
    return reply


def op_profile(target, msg):
    import copy
    import tracemalloc

    args = decode(msg.get("args", []))
    kwargs = decode(msg.get("kwargs", {}))
    repeat = max(1, int(msg.get("repeat", 5)))
    min_ns = int(msg.get("min_time_ns", 2_000_000))
    budget_ns = int(msg.get("budget_ns", 5_000_000_000))
    timings = []
    sink = io.StringIO()
    old = sys.stdout
    sys.stdout = sink
    start = time.perf_counter_ns()
    try:
        for _ in range(repeat):
            loops = 0
            total = 0
            while True:
                a, k = copy.deepcopy(args), copy.deepcopy(kwargs)
                t0 = time.perf_counter_ns()
                result, overhead_ns = _invoke(target, a, k)
                _materialize(result)
                total += max(1, time.perf_counter_ns() - t0 - overhead_ns)
                loops += 1
                if total >= min_ns or loops >= 1000 or time.perf_counter_ns() - start > budget_ns:
                    break
            timings.append(total / loops)
            if time.perf_counter_ns() - start > budget_ns:
                break
        peak = None
        if msg.get("memory", True):
            a, k = copy.deepcopy(args), copy.deepcopy(kwargs)
            tracemalloc.start()
            try:
                result, _ = _invoke(target, a, k)
                _materialize(result)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
    except BaseException as exc:  # noqa: BLE001
        sys.stdout = old
        if isinstance(exc, KeyboardInterrupt):
            raise
        return {"id": msg.get("id"), "ok": False, **_exc_info(exc, target.code_name)}
    finally:
        sys.stdout = old
    timings.sort()
    return {
        "id": msg.get("id"),
        "ok": True,
        "ns_min": timings[0],
        "ns_median": timings[len(timings) // 2],
        "samples": len(timings),
        "peak_bytes": peak,
    }


def _short(v, limit=120):
    try:
        text = repr(v)
    except Exception:  # noqa: BLE001
        text = "<unrepresentable>"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def op_trace(target, msg):
    """Line-by-line trace of the entrypoint on one (small, shrunk) input."""
    args = decode(msg.get("args", []))
    kwargs = decode(msg.get("kwargs", {}))
    max_events = int(msg.get("max_events", 200))
    code_name = target.code_name
    source_lines = target.source.splitlines()
    events = []
    last_locals = {}
    truncated = [False]

    def tracer(frame, event, arg):
        if frame.f_code.co_filename != code_name:
            return None
        if len(events) >= max_events:
            truncated[0] = True
            return None
        if event in ("line", "return"):
            key = id(frame)
            current = {k: _short(v) for k, v in frame.f_locals.items() if not k.startswith("__")}
            prev = last_locals.get(key, {})
            changed = {k: v for k, v in current.items() if prev.get(k) != v}
            last_locals[key] = current
            lineno = frame.f_lineno
            entry = {
                "event": event,
                "func": frame.f_code.co_name,
                "line": lineno,
                "code": source_lines[lineno - 1].strip() if 0 < lineno <= len(source_lines) else "",
            }
            if changed:
                entry["changed"] = changed
            if event == "return":
                entry["return"] = _short(arg)
            events.append(entry)
        return tracer

    sink = io.StringIO()
    old = sys.stdout
    sys.stdout = sink
    result = None
    error = None
    sys.settrace(tracer)
    try:
        result, _ = _invoke(target, args, kwargs)
        result = _materialize(result)
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, KeyboardInterrupt):
            raise
        error = _exc_info(exc, code_name)
    finally:
        sys.settrace(None)
        sys.stdout = old
    reply = {"id": msg.get("id"), "ok": error is None, "trace": events, "truncated": truncated[0]}
    if error:
        reply.update(error)
    else:
        reply["out"] = encode(result)
    return reply


def main():
    proto = Protocol()
    try:
        import resource  # POSIX only

        limit_mb = int(os.environ.get("CX_MEMORY_MB", "0"))
        if limit_mb > 0:
            limit = limit_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ImportError, ValueError, OSError):
        pass
    setup = proto.read()
    if setup is None:
        return
    sys.setrecursionlimit(int(setup.get("recursion_limit", 20000)))
    try:
        target = Target(setup)
    except BaseException as exc:  # noqa: BLE001
        info = _exc_info(exc, setup.get("filename", "<candidate>"))
        info["tb"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:]
        proto.send({"ok": False, "error": info["msg"], **info})
        return
    proto.send({"ok": True, "entry": target.name})
    ops = {"call": op_call, "profile": op_profile, "trace": op_trace}
    try:
        while True:
            msg = proto.read()
            if msg is None or msg.get("op") == "exit":
                return
            handler = ops.get(msg.get("op"))
            if handler is None:
                proto.send({"id": msg.get("id"), "ok": False, "exc": "ProtocolError", "msg": f"unknown op {msg.get('op')!r}"})
                continue
            proto.send(handler(target, msg))
    finally:
        target.close()


if __name__ == "__main__":
    # Give the protocol worker enough C stack for deeply recursive user code,
    # then restore the process default before it can create framework threads.
    # In particular, FastAPI/TestClient must not inherit an enormous stack for
    # every request thread under the worker's memory limit.
    start_gate = threading.Event()

    def run_worker():
        start_gate.wait()
        main()

    previous_stack_size = None
    try:
        previous_stack_size = threading.stack_size()
        threading.stack_size(WORKER_STACK_SIZE)
    except (ValueError, RuntimeError):
        pass
    worker = threading.Thread(target=run_worker, name="counterexample-worker")
    try:
        worker.start()
    finally:
        if previous_stack_size is not None:
            try:
                threading.stack_size(previous_stack_size)
            except (ValueError, RuntimeError):
                pass
        start_gate.set()
    worker.join()
    sys.stdout.flush()
    os._exit(0)
