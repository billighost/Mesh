"""
mesh/_model_download.py — Detached helper that downloads the pinned ONNX model.

`mesh setup` and `mesh daemon start` are short-lived CLI processes. Starting the
download on a daemon *thread* inside them was a no-op: CPython terminates daemon
threads during interpreter shutdown, so the download never finished (or never
began) while status stayed at "downloading". This module is spawned as a
detached child process so the download outlives the CLI command.

It is an internal entry point; nothing here prompts the user. The parent has
already obtained consent before spawning, so the child proceeds without asking.
"""

from __future__ import annotations


def main() -> int:
    from .memory import ensure_model_downloaded

    # blocking=True waits for the worker thread in *this* process, which keeps
    # the interpreter alive until the verified download completes.
    ensure_model_downloaded(
        blocking=True,
        timeout=1800.0,
        interactive_prompt=False,
        assume_yes=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
