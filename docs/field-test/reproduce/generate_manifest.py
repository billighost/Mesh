import os
import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
REPORT_DIR = PROJECT_DIR / "report"

def generate_manifest():
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "package_version": "0.8.1b2",
        "tested_wheel_sha256": "e1a7a0af71abe3f51b02150d9a2d5fdc1d9d8a618937e9d41bf899c08ead06ff",
        "tested_sdist_sha256": "83d6cb34e09ac0a759d5d0ebda659e70a414533b9d668c80d1eca4958c9ee91f",
        "files": []
    }

    manifest_file = REPORT_DIR / "MANIFEST.json"

    for root, _, files in os.walk(REPORT_DIR):
        for f in sorted(files):
            fpath = Path(root) / f
            if fpath == manifest_file:
                continue
            # Skip temp or cache files
            if "__pycache__" in str(fpath) or fpath.suffix == ".pyc":
                continue

            rel_path = fpath.relative_to(REPORT_DIR).as_posix()
            data = fpath.read_bytes()
            sha256 = hashlib.sha256(data).hexdigest()

            manifest["files"].append({
                "path": rel_path,
                "size_bytes": len(data),
                "sha256": sha256
            })

    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Generated MANIFEST.json with {len(manifest['files'])} entries.")
    return manifest

if __name__ == "__main__":
    generate_manifest()
