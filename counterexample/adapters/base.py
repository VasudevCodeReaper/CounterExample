"""Process sandbox shared by every language adapter.

Each implementation (oracle or candidate) runs in its own long-lived child
process speaking JSON lines.  :class:`ProcessSide` streams a batch of call
messages into the child from a writer thread while the caller consumes replies
from a reader thread with a per-call deadline, so:

* a hung call becomes ``status="timeout"`` and the child is killed & respawned;
* a segfault / ``os._exit`` / OOM-kill becomes ``status="crash"``;
* thousands of small calls cost one process start, not thousands.
"""

from __future__ import annotations

import collections
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from counterexample.codec import decode

_EOF = object()
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class WorkerError(RuntimeError):
    """The implementation could not be loaded (syntax error, missing entrypoint, compile error...)."""

    def __init__(self, message: str, info: dict[str, Any] | None = None):
        super().__init__(message)
        self.info = info or {}


@dataclass
class CallResult:
    status: str  # ok | error | timeout | crash | skipped
    out: Any = None
    out_raw: Any = None
    args_after: Any = None
    ns: int | None = None
    exc: str | None = None
    msg: str | None = None
    line: int | None = None
    tb: str | None = None
    stdout: str | None = None
    stderr: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def error_summary(self) -> str:
        if self.status == "timeout":
            return "timed out"
        if self.status == "crash":
            return "process crashed" + (f": {self.stderr.strip().splitlines()[-1]}" if self.stderr and self.stderr.strip() else "")
        if self.status == "error":
            return self.msg or self.exc or "error"
        return self.status


def parse_reply(reply: dict[str, Any]) -> CallResult:
    if reply.get("ok"):
        out_raw = reply.get("out")
        res = CallResult(status="ok", out=decode(out_raw), out_raw=out_raw, ns=reply.get("ns"), stdout=reply.get("stdout"))
        if "args_after" in reply:
            res.args_after = decode(reply["args_after"])
        for key in ("ns_min", "ns_median", "samples", "peak_bytes", "trace", "truncated"):
            if key in reply:
                res.extra[key] = reply[key]
        return res
    return CallResult(
        status="error",
        ns=reply.get("ns"),
        exc=reply.get("exc"),
        msg=reply.get("msg"),
        line=reply.get("line"),
        tb=reply.get("tb"),
        stdout=reply.get("stdout"),
        extra={k: reply[k] for k in ("trace", "truncated") if k in reply},
    )


class ProcessSide:
    """One sandboxed implementation running in a persistent child process."""

    def __init__(
        self,
        argv: list[str],
        *,
        label: str,
        setup: dict[str, Any] | None = None,
        timeout_s: float = 2.0,
        startup_timeout_s: float = 60.0,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ):
        self.argv = argv
        self.label = label
        self.setup = setup
        self.timeout_s = timeout_s
        self.startup_timeout_s = startup_timeout_s
        self.env = env
        self.cwd = cwd
        self.proc: subprocess.Popen[bytes] | None = None
        self.replies: queue.Queue[Any] = queue.Queue()
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=60)
        self.entry: str | None = None
        self.restarts = 0
        self._tmpdir: str | None = None

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> None:
        if self.cwd is None:
            if self._tmpdir is None:
                self._tmpdir = tempfile.mkdtemp(prefix="cx-sandbox-")
            cwd = self._tmpdir
        else:
            cwd = self.cwd
        kwargs: dict[str, Any] = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        else:
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        self.replies = queue.Queue()
        self.stderr_tail.clear()
        self.proc = subprocess.Popen(
            self.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=self.env,
            **kwargs,
        )
        threading.Thread(target=self._read_stdout, args=(self.proc, self.replies), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(self.proc,), daemon=True).start()
        if self.setup is not None:
            self._write(self.proc, [json.dumps(self.setup).encode("utf-8") + b"\n"])
        try:
            ready = self.replies.get(timeout=self.startup_timeout_s)
        except queue.Empty:
            self.kill()
            raise WorkerError(f"{self.label} worker did not start within {self.startup_timeout_s:.0f}s")
        if ready is _EOF:
            time.sleep(0.05)
            tail = "".join(self.stderr_tail).strip()
            self.kill()
            raise WorkerError(f"{self.label} worker exited during startup" + (f": {tail[-2000:]}" if tail else ""), {"stderr": tail})
        if not ready.get("ok"):
            self.kill()
            raise WorkerError(f"{self.label} failed to load: {ready.get('error') or ready.get('msg')}", ready)
        self.entry = ready.get("entry")

    def kill(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                if os.name == "posix":
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        proc.kill()
                else:
                    proc.kill()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - best effort cleanup
            pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self._write(self.proc, [b'{"op":"exit"}\n'])
                self.proc.wait(timeout=1)
            except Exception:  # noqa: BLE001
                pass
        self.kill()
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    def __enter__(self) -> "ProcessSide":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- io threads -------------------------------------------------------- #
    @staticmethod
    def _read_stdout(proc: subprocess.Popen[bytes], q: queue.Queue[Any]) -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    q.put(json.loads(line.decode("utf-8", errors="replace")))
                except ValueError:
                    q.put({"ok": False, "exc": "ProtocolError", "msg": "unparseable worker reply: " + line[:200].decode("utf-8", "replace")})
        except (OSError, ValueError):
            pass
        q.put(_EOF)

    def _read_stderr(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stderr is not None
        try:
            for line in proc.stderr:
                self.stderr_tail.append(line.decode("utf-8", errors="replace"))
        except (OSError, ValueError):
            pass

    @staticmethod
    def _write(proc: subprocess.Popen[bytes], lines: list[bytes]) -> None:
        try:
            assert proc.stdin is not None
            for line in lines:
                proc.stdin.write(line)
            proc.stdin.flush()
        except (OSError, ValueError):
            pass  # child died; the reader reports EOF

    # -- execution --------------------------------------------------------- #
    def run(
        self,
        messages: list[dict[str, Any]] | list[bytes],
        *,
        timeout_s: float | None = None,
        deadline: float | None = None,
        on_result: Callable[[int, CallResult], None] | None = None,
    ) -> list[CallResult]:
        """Execute every message in order, returning one :class:`CallResult` each."""
        timeout = self.timeout_s if timeout_s is None else timeout_s
        payload = [m if isinstance(m, bytes) else json.dumps(m, separators=(",", ":")).encode("utf-8") + b"\n" for m in messages]
        results: list[CallResult] = []
        i = 0
        while i < len(payload):
            if deadline is not None and time.monotonic() > deadline:
                results.extend(CallResult(status="skipped") for _ in range(len(payload) - i))
                break
            if self.proc is None:
                self.start()
                self.restarts += 1
            proc = self.proc
            assert proc is not None
            batch = payload[i:]
            writer = threading.Thread(target=self._write, args=(proc, batch), daemon=True)
            writer.start()
            consumed = 0
            broke = False
            for _ in batch:
                t0 = time.monotonic()
                try:
                    item = self.replies.get(timeout=timeout)
                except queue.Empty:
                    res = CallResult(status="timeout", ns=int((time.monotonic() - t0) * 1e9))
                    broke = True
                else:
                    if item is _EOF:
                        time.sleep(0.02)  # let the stderr reader catch up
                        code = proc.poll()
                        res = CallResult(status="crash", stderr="".join(self.stderr_tail)[-3000:], msg=f"exit code {code}")
                        broke = True
                    else:
                        res = parse_reply(item)
                results.append(res)
                consumed += 1
                if on_result is not None:
                    on_result(len(results) - 1, res)
                if broke:
                    break
                if deadline is not None and time.monotonic() > deadline:
                    break
            i += consumed
            if broke:
                self.kill()
            elif consumed < len(batch):
                # Deadline hit mid-batch: the child still has queued work.
                self.kill()
        return results

    def call(self, message: dict[str, Any], timeout_s: float | None = None) -> CallResult:
        return self.run([message], timeout_s=timeout_s)[0]


def sandbox_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if extra:
        env.update(extra)
    return env
