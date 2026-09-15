#!/usr/bin/env python3
"""Set the project version from git, not from the local files.

The next version is one above the highest vMAJOR.MINOR.PATCH tag pushed
to the remote (the source of truth), written to every release surface at
once: the three .py VERSION constants, installer.iss, and both extension
manifests. This keeps the surfaces from drifting and makes it impossible
to accidentally reuse or skip a number.

Usage:
  python bump_version.py            # patch bump from the latest pushed tag
  python bump_version.py --minor    # minor bump (patch resets to 0)
  python bump_version.py --major    # major bump (minor+patch reset to 0)
  python bump_version.py --set X.Y.Z  # force an explicit version (e.g. a
                                      # store reserved the computed one)
  python bump_version.py --print    # print the computed next version only
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# file -> (regex capturing the current version, replacement template)
SURFACES = {
    "stream_monitor_tray.py": (r'VERSION = "(\d+\.\d+\.\d+)"', 'VERSION = "{v}"'),
    "settings_editor.py": (r'VERSION = "(\d+\.\d+\.\d+)"', 'VERSION = "{v}"'),
    "setup_wizard.py": (r'VERSION = "(\d+\.\d+\.\d+)"', 'VERSION = "{v}"'),
    "installer.iss": (r'#define MyAppVersion "(\d+\.\d+\.\d+)"',
                      '#define MyAppVersion "{v}"'),
    "chrome_extension/manifest.json": (r'"version": "(\d+\.\d+\.\d+)"',
                                       '"version": "{v}"'),
    "firefox_extension/manifest.json": (r'"version": "(\d+\.\d+\.\d+)"',
                                        '"version": "{v}"'),
}

TAG_RE = re.compile(r"(?:refs/tags/)?v(\d+)\.(\d+)\.(\d+)")


def pushed_versions():
    """Every vMAJOR.MINOR.PATCH that has a tag on the remote (a -pre
    suffix counts as its base number). Falls back to local tags offline."""
    try:
        out = subprocess.run(["git", "ls-remote", "--tags", "origin"],
                             capture_output=True, text=True, check=True,
                             cwd=ROOT).stdout
    except subprocess.CalledProcessError:
        out = subprocess.run(["git", "tag"], capture_output=True, text=True,
                             cwd=ROOT).stdout
    return sorted({tuple(int(x) for x in m.groups())
                   for m in TAG_RE.finditer(out)})


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--major", action="store_true")
    g.add_argument("--minor", action="store_true")
    g.add_argument("--set", dest="explicit", metavar="X.Y.Z")
    ap.add_argument("--print", dest="only_print", action="store_true")
    args = ap.parse_args()

    vers = pushed_versions()
    latest = ".".join(map(str, vers[-1])) if vers else "(none)"

    if args.explicit:
        if not re.fullmatch(r"\d+\.\d+\.\d+", args.explicit):
            sys.exit(f"--set expects X.Y.Z, got {args.explicit!r}")
        nv = args.explicit
    else:
        if not vers:
            sys.exit("No version tags found on the remote.")
        maj, minr, pat = vers[-1]
        if args.major:
            nv = f"{maj + 1}.0.0"
        elif args.minor:
            nv = f"{maj}.{minr + 1}.0"
        else:
            nv = f"{maj}.{minr}.{pat + 1}"

    if args.only_print:
        print(nv)
        return

    for rel, (pat_re, repl) in SURFACES.items():
        p = ROOT / rel
        text = p.read_text(encoding="utf-8")
        new_text, n = re.subn(pat_re, repl.format(v=nv), text, count=1)
        if n != 1:
            sys.exit(f"ERROR: version pattern not found in {rel}")
        p.write_text(new_text, encoding="utf-8")
        print(f"  {rel}: -> {nv}")

    print(f"latest pushed tag: v{latest}  ->  new version: {nv}")


if __name__ == "__main__":
    main()
