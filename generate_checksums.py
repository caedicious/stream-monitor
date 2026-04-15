"""
Generate SHA256 checksums for release artifacts.
Run after build.bat to produce SHA256SUMS.txt for GitHub releases.
"""
import hashlib
from pathlib import Path


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    artifacts = [
        Path("installer_output/StreamMonitorInstaller.exe"),
        Path("dist/StreamMonitor.exe"),
        Path("dist/StreamMonitorSetup.exe"),
        Path("dist/StreamMonitorSettings.exe"),
        Path("dist/stream_monitor_tab_closer.xpi"),
    ]

    lines = []
    for path in artifacts:
        if not path.exists():
            print(f"  MISSING: {path}")
            continue
        digest = sha256_of(path)
        size_kb = path.stat().st_size / 1024
        # GNU coreutils sha256sum format: "<hash>  <filename>"
        lines.append(f"{digest}  {path.name}")
        print(f"  {path.name:<35} {size_kb:>8.1f} KB  {digest[:16]}...")

    output = Path("installer_output/SHA256SUMS.txt")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nWrote {output}")
    print("\nUsers can verify with:")
    print("  Get-FileHash StreamMonitorInstaller.exe -Algorithm SHA256")
    print("  # Compare against SHA256SUMS.txt")


if __name__ == "__main__":
    main()
