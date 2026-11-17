"""GitHub Actions only: provision the separately requested, hash-pinned C4 fixture."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.request

from retrieval_c4_model import MODEL


def provision(directory, helpers):
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("RemoteActionsRequired")
    directory.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    completed = {}
    for kind in ("model", "tokenizer"):
        filename = MODEL[kind + "_file"]
        expected_bytes, expected_hash = MODEL[kind + "_bytes"], MODEL[kind + "_sha256"]
        destination = helpers.safe_member(directory, filename, must_exist=False)
        if destination.exists():
            if destination.stat().st_size != expected_bytes or helpers.digest_file(destination) != expected_hash:
                raise ValueError("CachedC4FixtureHashMismatch")
            completed[filename] = expected_hash
            continue
        temporary = helpers.safe_member(directory, filename + ".download", must_exist=False)
        if temporary.exists():
            raise ValueError("C4TemporaryDestinationAlreadyExists")
        url = ("https://huggingface.co/" + MODEL["repository"] + "/resolve/" + MODEL["revision"]
               + "/" + MODEL[kind + "_remote"])
        request = urllib.request.Request(url, headers={"User-Agent": "Mesh-optional-C4-fixture/1"})
        digest, total = hashlib.sha256(), 0
        try:
            with urllib.request.urlopen(request, timeout=30) as response, temporary.open("xb") as stream:
                while chunk := response.read(65536):
                    total += len(chunk)
                    if total > expected_bytes or time.monotonic() - started > 360:
                        raise ValueError("C4FixtureTransferBoundExceeded")
                    digest.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if total != expected_bytes or digest.hexdigest() != expected_hash:
                raise ValueError("C4FixtureHashMismatch")
            helpers.safe_member(directory, filename, must_exist=False)
            helpers.safe_member(directory, temporary.name)
            if destination.exists():
                raise ValueError("C4FixtureDestinationAppeared")
            os.replace(temporary, destination)
            completed[filename] = expected_hash
        finally:
            if temporary.exists():
                helpers.safe_member(directory, temporary.name)
                print(json.dumps({"owned_cleanup_files": [temporary.name]}), flush=True)
                temporary.unlink()
    return completed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args(argv)
    if os.environ.get("GITHUB_ACTIONS") != "true":
        print(json.dumps({"status": "unavailable", "failure_type": "RemoteActionsRequired"}))
        return 2
    from retrieval_c4 import checked_helpers

    helpers = checked_helpers(args.repository)
    directory = helpers.isolated_artifact_path(args.directory, args.repository)
    hashes = provision(directory, helpers)
    print(json.dumps({"status": "verified", "revision": MODEL["revision"], "hashes": hashes}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "unavailable", "failure_type": type(exc).__name__}))
        raise SystemExit(2) from None
