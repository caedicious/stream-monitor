"""Build the Chrome Web Store submission zip for the companion extension.

Produces dist/stream_monitor_chrome_<version>.zip with every file at the
root of the archive (required by the Chrome Web Store).
"""

import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "chrome_extension"
DIST = ROOT / "dist"


def main() -> None:
    manifest = json.loads((SRC / "manifest.json").read_text(encoding="utf-8"))
    version = manifest["version"]

    DIST.mkdir(parents=True, exist_ok=True)
    out = DIST / f"stream_monitor_chrome_{version}.zip"
    if out.exists():
        out.unlink()

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(SRC.rglob("*")):
            if path.is_file():
                arcname = path.relative_to(SRC).as_posix()
                zf.write(path, arcname)
                print(f"  + {arcname}")

    print(f"\nBuilt {out} ({out.stat().st_size:,} bytes)")

    # Verify by listing back the contents
    with zipfile.ZipFile(out, "r") as zf:
        names = zf.namelist()
        assert "manifest.json" in names, "manifest.json must be at the zip root"
        print(f"Verified: {len(names)} files at root, manifest.json present")


if __name__ == "__main__":
    main()
