"""Run pytest in an isolated home with a bounded worker pool and optional resource report."""
from __future__ import annotations

import argparse
import json
import os
import site
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
_PLUGINS = ("pytest_asyncio.plugin", "pytest_cov.plugin", "_hypothesis_pytestplugin", "anyio.pytest_plugin")
_HOME_BINDINGS = {
    "MESH_HOME": "mesh", "MESH_DIR": "mesh", "HOME": "home", "USERPROFILE": "home",
    "APPDATA": "appdata", "LOCALAPPDATA": "localappdata", "XDG_CONFIG_HOME": "xdg",
    "CODEX_HOME": "home/.codex", "CLAUDE_CONFIG_DIR": "home/.claude",
    "HYPOTHESIS_STORAGE_DIRECTORY": "hypothesis",
}
_TEMP_BINDINGS = {"TMPDIR": "tmp", "TMP": "tmp", "TEMP": "tmp"}
_NO_CLOUD_KEYS = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    "AZURE_OPENAI_API_KEY", "COHERE_API_KEY", "MISTRAL_API_KEY", "DEEPSEEK_API_KEY",
    "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_ACCESS_TOKEN", "AZURE_OPENAI_AD_TOKEN",
)
_TEST_SETTINGS = frozenset({
    "MESH_TEST_WORKERS", "MESH_EMBEDDING_BACKEND", "MESH_QUERY_MODE",
    "MESH_FORCE_HASH_EMBED", "MESH_FORCE_FASTEMBED", "MESH_FORCE_PYTORCH", "MESH_REQUIRE_MODEL",
    "MESH_BG_ONNX", "MESH_SYNC_ONNX", "MESH_CLI_FORCE_ONNX",
    "MESH_MODEL_SHA256", "MESH_TOKENIZER_SHA256", "MESH_NO_DAEMON_PROXY", "MESH_NO_OUTBOX_WORKER",
})
_THREAD_LIMITS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")
_ISOLATED_ENV_KEYS = frozenset({
    *_HOME_BINDINGS, *_TEMP_BINDINGS, *_NO_CLOUD_KEYS, *_THREAD_LIMITS,
    "PYTHONUSERBASE", "PYTHONUTF8", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTEST_PLUGINS", "PYTEST_ADDOPTS",
    "HYPOTHESIS_PROFILE",
})


def isolated_environment(root: Path, inherited: dict[str, str] | None = None) -> dict[str, str]:
    """Bind a fresh runner-owned root before pytest, plugins, or Mesh imports."""
    # Only the runner's freshly allocated directory is canonicalized. Nested
    # tempfile calls must also avoid OS aliases such as macOS's /var symlink.
    root = root.resolve()
    environment = {
        name: value for name, value in (os.environ if inherited is None else inherited).items()
        if not name.startswith("MESH_") or name in _TEST_SETTINGS
    }
    environment.setdefault("PYTHONUSERBASE", site.getuserbase())
    for name, directory in {**_HOME_BINDINGS, **_TEMP_BINDINGS}.items():
        path = root / directory
        path.mkdir(parents=True, exist_ok=True)
        environment[name] = str(path)
    environment["MESH_TEST_RUN_ROOT"] = str(root)
    for name in ("MESH_NO_AUTO_DAEMON", "MESH_NO_MODEL_DOWNLOAD", "MESH_NO_UPDATE_CHECK", "MESH_NO_IMPLICIT_CLOUD"):
        environment[name] = "1"
    for name in (
        "MESH_AUTO_APPROVE", "MESH_AUTO_START_DAEMON", "MESH_ALLOW_CLOUD_CAPTURE",
        "MESH_ALLOW_CLOUD_SYNC", "MESH_ALLOW_LOG_SYNC", "MESH_ALLOW_DAEMON_DOWNLOAD", "MESH_ENABLE_AUTO_SYNC",
    ):
        environment[name] = "0"
    for name in _NO_CLOUD_KEYS:
        environment.pop(name, None)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    # Unrelated user plugins can load config or launch processes before conftest.
    environment.pop("PYTEST_PLUGINS", None)
    environment.pop("PYTEST_ADDOPTS", None)
    for name in _THREAD_LIMITS:
        environment[name] = "1"
    environment["PYTHONUTF8"] = "1"
    return environment


def apply_isolated_environment(environment: dict[str, str]) -> None:
    """Replace managed test settings without transiently clearing the process environment."""
    for name in os.environ.keys() | environment.keys():
        if name.startswith("MESH_") or name in _ISOLATED_ENV_KEYS:
            if name in environment:
                os.environ[name] = environment[name]
            else:
                os.environ.pop(name, None)


def pytest_command(workers: int, arguments: list[str]) -> list[str]:
    if not 1 <= workers <= 4:
        raise ValueError("Use between one and four test workers.")
    if any(argument.startswith(("-n", "--numprocesses", "--dist", "--maxprocesses")) for argument in arguments):
        raise ValueError("Configure parallelism with --workers instead of pytest worker flags.")
    command = [sys.executable, "-m", "pytest"]
    for plugin in _PLUGINS:
        command.extend(["-p", plugin])
    if workers > 1:
        command.extend(["-p", "xdist.plugin", "-n", str(workers), "--dist", "loadfile"])
    if not any(argument.startswith("--durations") for argument in arguments):
        command.append("--durations=20")
    command.extend(arguments)
    return command


class _ResourceSampler:
    """Sample summed RSS and CPU for the pytest process tree without a required dependency."""

    def __init__(self, process_id: int) -> None:
        try:
            import psutil
        except ImportError:
            self._psutil = None
            self._process = None
        else:
            self._psutil = psutil
            try:
                self._process = psutil.Process(process_id)
            except psutil.Error:
                self._process = None
        self._stop = threading.Event()
        self._cpu: dict[tuple[int, float], float] = {}
        self._rss_peak = 0
        self._count_peak = 0
        self._samples = 0
        self._thread = threading.Thread(target=self._run, name="mesh-test-resource-sampler", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _sample(self) -> None:
        if self._process is None or self._psutil is None:
            return
        try:
            processes = [self._process, *self._process.children(recursive=True)]
        except self._psutil.Error:
            return
        rss = 0
        observed = 0
        for process in processes:
            try:
                with process.oneshot():
                    cpu = process.cpu_times()
                    rss += process.memory_info().rss
                    key = (process.pid, process.create_time())
                    self._cpu[key] = max(self._cpu.get(key, 0.0), cpu.user + cpu.system)
                    observed += 1
            except self._psutil.Error:
                continue
        self._rss_peak = max(self._rss_peak, rss)
        self._count_peak = max(self._count_peak, observed)
        self._samples += 1

    def _run(self) -> None:
        while not self._stop.is_set():
            self._sample()
            self._stop.wait(0.25)

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        self._thread.join(timeout=2)
        return {
            "resource_sampling_available": self._process is not None,
            "resource_sample_count": self._samples,
            "resource_sampling_interval_seconds": 0.25,
            "sampled_peak_process_tree_rss_bytes": self._rss_peak if self._samples else None,
            "sampled_process_tree_cpu_seconds": round(sum(self._cpu.values()), 3) if self._samples else None,
            "sampled_peak_process_count": self._count_peak if self._samples else None,
            "resource_notes": "RSS is summed across processes and may count shared pages repeatedly; sampled CPU can miss short-lived processes.",
        }


def _stop_process_tree(process: subprocess.Popen[Any]) -> None:
    try:
        import psutil
    except ImportError:
        process.terminate()
    else:
        try:
            children = psutil.Process(process.pid).children(recursive=True)
        except psutil.Error:
            children = []
        for child in reversed(children):
            try:
                child.terminate()
            except psutil.Error:
                pass
        process.terminate()
        _, alive = psutil.wait_procs(children, timeout=5)
        for child in alive:
            try:
                child.kill()
            except psutil.Error:
                pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run_tests(workers: int, arguments: list[str], report_path: Path | None = None) -> int:
    command = pytest_command(workers, arguments)
    with tempfile.TemporaryDirectory(prefix="mesh-test-run-", ignore_cleanup_errors=True) as temporary:
        environment = isolated_environment(Path(temporary))
        started = time.perf_counter()
        process = subprocess.Popen(command, cwd=REPO_ROOT, env=environment)
        sampler = _ResourceSampler(process.pid)
        sampler.start()
        try:
            return_code = process.wait()
        except KeyboardInterrupt:
            _stop_process_tree(process)
            return_code = 130
        finally:
            resources = sampler.stop()
        report = {
            "workers": workers, "distribution": "loadfile" if workers > 1 else "sequential",
            "pytest_arguments": arguments, "exit_code": return_code,
            "wall_seconds": round(time.perf_counter() - started, 3),
            "python_version": sys.version.split()[0], **resources,
        }
        print(f"\nMesh test run: {report['wall_seconds']:.3f}s with {workers} worker(s); exit {return_code}.", flush=True)
        if report_path is not None:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return return_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, epilog="Pass pytest options after --; all selected tests and coverage options are retained.")
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=2, help="Bounded pytest workers (default: 2).")
    parser.add_argument("--report", type=Path, help="Write wall time and optional sampled resource metrics as JSON.")
    options, arguments = parser.parse_known_args(argv)
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    try:
        return run_tests(options.workers, arguments, options.report)
    except ValueError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
