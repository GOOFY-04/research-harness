"""Bounded subprocess execution with file-backed logs and process-tree cleanup."""
import os
import signal
import subprocess
import tempfile
from harness.tools.metrics import capture_metrics


def run_command(command: list[str], cwd, timeout: float, log_limit=20000) -> dict:
    if timeout <= 0:
        raise ValueError("Timeout must be positive")
    # Avoid handing the model provider's credentials to generated programs.
    env = {k: v for k, v in os.environ.items()
           if not any(word in k.upper() for word in ("TOKEN", "SECRET", "API_KEY", "PASSWORD"))}
    env.update(
        PYTHONIOENCODING="utf-8",
        PYTHONUTF8="1",
        PYTHONNOUSERSITE="1",
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
        NUMEXPR_NUM_THREADS="1",
        VECLIB_MAXIMUM_THREADS="1",
        BLIS_NUM_THREADS="1",
    )
    env.pop("PYTHONPATH", None)
    options = {"start_new_session": True} if os.name != "nt" else {}
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(command, cwd=cwd, stdout=out, stderr=err,
                                   stdin=subprocess.DEVNULL, env=env, **options)
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            timed_out = isinstance(exc, subprocess.TimeoutExpired)
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
            if isinstance(exc, KeyboardInterrupt):
                raise
        def read_tail(stream):
            size = stream.seek(0, 2)
            stream.seek(max(0, size - log_limit))
            return ("[log truncated]\n" if size > log_limit else "") + stream.read().decode("utf-8", errors="replace")
        metrics, metric_errors = capture_metrics(out)
        return {"success": process.returncode == 0 and not timed_out,
                "returncode": process.returncode, "stdout": read_tail(out),
                "stderr": read_tail(err), "timed_out": timed_out, "command": command,
                "emitted_metrics": metrics, "metric_capture_errors": metric_errors}
