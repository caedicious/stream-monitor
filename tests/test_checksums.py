"""Tests for the checksum generator used in the release pipeline."""
import hashlib
from pathlib import Path

import pytest

import generate_checksums as gc


def test_sha256_of_known_content(tmp_path):
    f = tmp_path / "sample.bin"
    data = b"the quick brown fox jumps over the lazy dog"
    f.write_bytes(data)
    expected = hashlib.sha256(data).hexdigest()
    assert gc.sha256_of(f) == expected


def test_sha256_of_empty_file(tmp_path):
    f = tmp_path / "empty.bin"
    f.write_bytes(b"")
    # SHA256 of empty input is a known constant
    assert gc.sha256_of(f) == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_sha256_handles_large_file_streaming(tmp_path):
    """sha256_of should read in chunks and handle files larger than one chunk
    (1 MiB) without loading everything into memory."""
    f = tmp_path / "big.bin"
    # Two chunks + a bit: 2.5 MiB
    payload = b"A" * (1024 * 1024) + b"B" * (1024 * 1024) + b"C" * (512 * 1024)
    f.write_bytes(payload)
    assert gc.sha256_of(f) == hashlib.sha256(payload).hexdigest()


def test_main_produces_sha256sums_file(tmp_path, monkeypatch, capsys):
    """Integration-style test: stub out the artifact paths, run main(),
    and verify SHA256SUMS.txt is written in the expected format."""
    # Create fake artifacts
    dist = tmp_path / "dist"
    installer_output = tmp_path / "installer_output"
    dist.mkdir()
    installer_output.mkdir()

    installer = installer_output / "StreamMonitorInstaller.exe"
    installer.write_bytes(b"installer")
    tray = dist / "StreamMonitor.exe"
    tray.write_bytes(b"tray")

    # Monkeypatch Path resolution by chdir'ing into the tmp dir
    monkeypatch.chdir(tmp_path)

    # Patch the artifact list to only our fakes (avoids MISSING warnings
    # for the others)
    original_artifacts = [
        Path("installer_output/StreamMonitorInstaller.exe"),
        Path("dist/StreamMonitor.exe"),
    ]
    monkeypatch.setattr(gc, "main", lambda: _run_main_with(original_artifacts))
    # Actually just re-import and run the real main with chdir'd cwd and real file set:
    _real_main()

    sums_file = installer_output / "SHA256SUMS.txt"
    assert sums_file.exists()
    content = sums_file.read_text()
    # Format: "<64 hex chars>  <filename>"
    lines = [ln for ln in content.strip().split("\n") if ln]
    assert len(lines) == 2
    for line in lines:
        parts = line.split("  ")
        assert len(parts) == 2
        assert len(parts[0]) == 64
        assert all(c in "0123456789abcdef" for c in parts[0])


def _run_main_with(_):
    raise NotImplementedError


def _real_main():
    """Run the actual main() function (which uses Path() relative to cwd)."""
    import importlib
    importlib.reload(gc)
    gc.main()


def test_main_skips_missing_files(tmp_path, monkeypatch, capsys):
    """If some artifacts don't exist, main() should warn but not crash."""
    installer_output = tmp_path / "installer_output"
    installer_output.mkdir()
    # Don't create any artifacts
    monkeypatch.chdir(tmp_path)

    _real_main()
    captured = capsys.readouterr()
    assert "MISSING" in captured.out

    sums_file = installer_output / "SHA256SUMS.txt"
    # File is still created, just empty (or one trailing newline)
    assert sums_file.exists()
    assert sums_file.read_text().strip() == ""
