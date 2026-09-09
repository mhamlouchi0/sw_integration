#!/usr/bin/env python3
"""
ci/ci_bump_version.py — Automatic z-increment for any repo
===========================================================
Reads the highest existing tag matching <ci_prefix>-vX.Y.Z,
increments Z by 1, creates an annotated git tag, and pushes it.

Usage (called from CI on merge to dev):
  python3 ci/ci_bump_version.py --repo fcs_model --ci-prefix fcs-model
  python3 ci/ci_bump_version.py --repo ofp       --ci-prefix fcc
  python3 ci/ci_bump_version.py --repo .          --ci-prefix sw-integration

Options:
  --repo        Path to the git repo (default: .)
  --ci-prefix   Tag prefix, e.g. fcs-model, fcc, sw-integration
  --bump        Which component to increment: z (default), y, x
  --dry-run     Print the new tag without creating or pushing it
  --message     Custom annotated tag message (default: auto-generated)
  --no-push     Do not push to origin after tagging
"""

import argparse
import re
import subprocess
import sys


def run(cmd, cwd=None):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"Command failed: {' '.join(cmd)}\n{result.stderr}")
    return result.stdout.strip()


def list_tags(repo, prefix):
    raw = run(["git", "tag", "--list", f"{prefix}-v*",
               "--sort=-version:refname"], cwd=repo)
    return [t.strip() for t in raw.splitlines() if t.strip()]


def parse_version(tag, prefix):
    pattern = re.compile(rf"^{re.escape(prefix)}-v(\d+)\.(\d+)\.(\d+)$")
    m = pattern.match(tag)
    if not m:
        sys.exit(f"Cannot parse version from tag: {tag}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def bump(x, y, z, component):
    if component == "x":
        return x + 1, 0, 0
    if component == "y":
        return x, y + 1, 0
    return x, y, z + 1


def make_tag(repo, prefix, new_tag, msg, dry_run, no_push):
    print(f"New tag   : {new_tag}")
    print(f"Message   : {msg}")
    if dry_run:
        print("(dry-run: tag not created)")
        return
    run(["git", "tag", "-a", new_tag, "-m", msg], cwd=repo)
    print(f"Created   : {new_tag}")
    if not no_push:
        run(["git", "push", "origin", new_tag], cwd=repo)
        print(f"Pushed    : {new_tag}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo",      default=".")
    parser.add_argument("--ci-prefix", required=True)
    parser.add_argument("--bump",      default="z", choices=["x", "y", "z"])
    parser.add_argument("--dry-run",   action="store_true")
    parser.add_argument("--message",   default="")
    parser.add_argument("--no-push",   action="store_true")
    args = parser.parse_args()

    prefix = args.ci_prefix
    repo   = args.repo
    tags   = list_tags(repo, prefix)
    sha    = run(["git", "rev-parse", "--short", "HEAD"], cwd=repo)

    if not tags:
        new_tag = f"{prefix}-v1.0.0"
        msg     = args.message or f"Release {new_tag} — initial release (commit {sha})"
        print(f"No existing tags for '{prefix}'. Starting at v1.0.0.")
        make_tag(repo, prefix, new_tag, msg, args.dry_run, args.no_push)
        return

    current = tags[0]
    x, y, z = parse_version(current, prefix)
    print(f"Current   : {current}")
    nx, ny, nz = bump(x, y, z, args.bump)
    new_tag = f"{prefix}-v{nx}.{ny}.{nz}"
    msg     = args.message or f"Release {new_tag} (commit {sha})"
    make_tag(repo, prefix, new_tag, msg, args.dry_run, args.no_push)


if __name__ == "__main__":
    main()
