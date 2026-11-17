"""Provision the fixed public CI fixture; no model runtime or LLM service is called."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.request

import production_runner as runner

REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
BASE = f"https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/{REVISION}"
FILES = {
    "model.onnx": ("onnx/model.onnx", 90405214, "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452"),
    "tokenizer.json": ("tokenizer.json", 466247, "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    directory = runner.private_path(args.directory)
    directory.mkdir(parents=True, exist_ok=True)
    allowed = runner.assignments(runner.REPO / "mesh/memory.py")
    for filename, key in (("model.onnx", "_ALLOWED_MODEL_SHA256"), ("tokenizer.json", "_ALLOWED_TOKENIZER_SHA256")):
        if FILES[filename][2] not in allowed.get(key, set()):
            raise runner.ControlError("PinnedFixtureNotSourceAllowlisted")
    cached = {}
    for filename, (_, expected_bytes, expected_sha) in FILES.items():
        path = directory / filename
        runner.no_links(path)
        if path.exists():
            if path.stat().st_size != expected_bytes or runner.sha_file(path, expected_bytes) != expected_sha:
                raise runner.ControlError("CachedFixtureDigestMismatch")
            cached[filename] = expected_sha
    if len(cached) == len(FILES):
        print(json.dumps({"status": "verified_cached", "fixture_revision": REVISION, "hashes": cached}))
        return 0
    started = time.monotonic()
    for filename, (remote, expected_bytes, expected_sha) in FILES.items():
        if filename in cached:
            continue
        destination = directory / filename
        temporary = directory / (filename + ".download")
        runner.no_links(destination)
        if destination.exists() or temporary.exists():
            raise runner.ControlError("FixtureDestinationAlreadyExists")
        digest, total = hashlib.sha256(), 0
        request = urllib.request.Request(BASE + "/" + remote, headers={"User-Agent": "Mesh-production-fixture/1"})
        with urllib.request.urlopen(request, timeout=30) as response, temporary.open("xb") as output:
            while chunk := response.read(65536):
                total += len(chunk)
                if total > expected_bytes or time.monotonic() - started > 360:
                    raise runner.ControlError("FixtureTransferBoundExceeded")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if total != expected_bytes or digest.hexdigest() != expected_sha:
            raise runner.ControlError("PinnedFixtureDigestMismatch")
        # Only this named file created by the provisioner is replaced.
        print(json.dumps({"verified_temporary_file": temporary.name, "bytes": total, "sha256": expected_sha}))
        os.replace(temporary, destination)
    print(json.dumps({"status": "verified", "fixture_revision": REVISION,
                      "hashes": {name: value[2] for name, value in FILES.items()}}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "unavailable", "error_class": type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1) from None
