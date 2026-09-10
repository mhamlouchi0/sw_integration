#!/usr/bin/env python3
"""
sw_integration/release.py  —  EOC release orchestrator

Usage:
  python3 release.py [--bump z|y|x] [--dry-run] [--skip-docker]
  python3 release.py --verify-only [--skip-docker]
"""

import argparse, binascii, json, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT     = Path(__file__).parent.resolve()
EOC_DIR  = ROOT / "eoc"
VL_FILE  = ROOT / "versions.lock"

SUBMODULES = {
    "fcs_model":  ROOT / "fcs_model",
    "Common_lib": ROOT / "fcs_model" / "Common_lib",
    "Controller": ROOT / "fcs_model" / "src" / "Controller",
    "VMS_model":  ROOT / "fcs_model" / "src" / "VMS_model",
    "ofp":        ROOT / "ofp",
}

TAG_PREFIX = {
    "fcs_model":  "fcs-model-v",
    "Common_lib": "common-lib-v",
    "Controller": "ctrl-v",
    "VMS_model":  "vms-v",
    "ofp":        "fcc-v",
}

SW_TAG_PREFIX = "sw-integration-v"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd, cwd=None, capture=False):
    result = subprocess.run(
        cmd, cwd=cwd, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if result.returncode != 0:
        if capture:
            print(result.stderr.strip())
        sys.exit(f"ERROR: command failed: {' '.join(cmd)}")
    return result.stdout.strip() if capture else None


def crc32_file(path: Path) -> str:
    data = path.read_bytes()
    return f"{binascii.crc32(data) & 0xFFFFFFFF:08X}"


# ---------------------------------------------------------------------------
# Step 1 — snapshot: verify all submodules are on exact annotated tags
# ---------------------------------------------------------------------------

def step_snapshot() -> dict:
    print("\n[1/6] Verifying submodule tags...")
    snap = {}
    for name, path in SUBMODULES.items():
        prefix = TAG_PREFIX[name]

        # Dirty check
        status = run(["git", "status", "--porcelain"], cwd=path, capture=True)
        if status:
            sys.exit(f"ERROR: {name} has uncommitted changes — commit or stash first.")

        # Exact tag check
        try:
            tag = run(["git", "describe", "--exact-match", "--tags", "HEAD"],
                      cwd=path, capture=True)
        except SystemExit:
            sys.exit(f"ERROR: {name} is not on an exact tag. Tag it first.")

        if not tag.startswith(prefix):
            sys.exit(f"ERROR: {name} tag '{tag}' does not start with '{prefix}'.")

        sha = run(["git", "rev-parse", "HEAD"], cwd=path, capture=True)
        snap[name] = {"tag": tag, "git_sha": sha}
        print(f"  {name}: {tag} ({sha[:8]})")

    return snap


# ---------------------------------------------------------------------------
# Step 2 — write versions.lock
# ---------------------------------------------------------------------------

def step_write_versions_lock(snap: dict, sw_tag: str, dry_run: bool):
    print("\n[2/6] Writing versions.lock...")
    lock = {
        "sw_integration": sw_tag,
        "fcs_model": {
            "tag":     snap["fcs_model"]["tag"],
            "git_sha": snap["fcs_model"]["git_sha"],
            "submodules": {
                "Common_lib": snap["Common_lib"]["tag"],
                "Controller": snap["Controller"]["tag"],
                "VMS_model":  snap["VMS_model"]["tag"],
            },
        },
        "ofp": {
            "tag":     snap["ofp"]["tag"],
            "git_sha": snap["ofp"]["git_sha"],
        },
    }
    if dry_run:
        print("  [dry-run] Would write versions.lock:")
        print(json.dumps(lock, indent=2))
    else:
        VL_FILE.write_text(json.dumps(lock, indent=2) + "\n")
        print(f"  Written: {VL_FILE}")


# ---------------------------------------------------------------------------
# Step 3 — build fcs_model
# ---------------------------------------------------------------------------

def step_build_fcs_model(dry_run: bool, skip_docker: bool) -> Path:
    print("\n[3/6] Building fcs_model...")
    build_dir = ROOT / "fcs_model" / "build"
    if dry_run:
        print(f"  [dry-run] Would cmake + make in {build_dir}")
        return build_dir

    build_dir.mkdir(parents=True, exist_ok=True)
    cmake_cmd = [
        "cmake", "..",
        f"-DCMAKE_TOOLCHAIN_FILE={ROOT / 'fcs_model' / 'toolchain-arm.cmake'}",
        "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
    ]
    run(cmake_cmd, cwd=build_dir)
    run(["make", "-j4"], cwd=build_dir)
    print(f"  Build complete: {build_dir}")
    return build_dir


# ---------------------------------------------------------------------------
# Step 4 — build ofp
# ---------------------------------------------------------------------------

def step_build_ofp(fcs_build_dir: Path, dry_run: bool, skip_docker: bool) -> Path:
    print("\n[4/6] Building ofp...")
    build_dir = ROOT / "ofp" / "build"
    lib_dir   = fcs_build_dir
    vms_autogen_dir = ROOT / "fcs_model" / "src" / "VMS_model" / "vms_autogen"

    if dry_run:
        print(f"  [dry-run] Would cmake + make in {build_dir}")
        print(f"    -DFCS_MODEL_LIB_DIR={lib_dir}")
        print(f"    -DFCS_MODEL_INC_DIR={inc_dir}")
        return build_dir

    build_dir.mkdir(parents=True, exist_ok=True)
    run([
        "cmake", "..",
        f"-DCMAKE_TOOLCHAIN_FILE={ROOT / 'ofp' / 'toolchain-arm.cmake'}",
        f"-DFCS_MODEL_LIB_DIR={lib_dir}",
        f"-DFCS_MODEL_INC_DIR={vms_autogen_dir}",
        "-DFCC_TYPE=SOLAERO2",
        "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
    ], cwd=build_dir)
    run(["make", "-j4"], cwd=build_dir)
    print(f"  Build complete: {build_dir}")
    return build_dir


# ---------------------------------------------------------------------------
# Step 5 — CRC32 all artifacts
# ---------------------------------------------------------------------------

def step_crc(fcs_build_dir: Path, ofp_build_dir: Path, dry_run: bool) -> dict:
    print("\n[5/6] Computing CRC32 checksums...")
    artifacts = {
        "fcs_model": {
            "libcontroller.a": fcs_build_dir / "libcontroller.a",
            "libvms_model.a":  fcs_build_dir / "libvms_model.a",
            "libcommon_lib.a": fcs_build_dir / "libcommon_lib.a",
        },
        "ofp": {
            "output.elf": ofp_build_dir / "output.elf",
        },
    }

    crcs = {}
    for repo, files in artifacts.items():
        crcs[repo] = {}
        for name, path in files.items():
            if dry_run:
                crcs[repo][name] = {"crc32": "DRYDRYDR", "bytes": 0}
                print(f"  [dry-run] {repo}/{name}")
            else:
                if not path.exists():
                    sys.exit(f"ERROR: artifact not found: {path}")
                crc = crc32_file(path)
                size = path.stat().st_size
                crcs[repo][name] = {"crc32": crc, "bytes": size}
                print(f"  {repo}/{name}: CRC32={crc}  bytes={size}")
    return crcs


# ---------------------------------------------------------------------------
# Step 6 — write EOC file, commit, tag
# ---------------------------------------------------------------------------

def step_write_eoc(sw_tag: str, snap: dict, crcs: dict, dry_run: bool) -> Path:
    print("\n[6/6] Writing EOC record...")
    EOC_DIR.mkdir(exist_ok=True)
    eoc_path = EOC_DIR / f"{sw_tag}.eoc.json"

    toolchain_ver = run(
        ["aarch64-none-elf-gcc", "--version"], capture=True
    ) if not dry_run else "aarch64-none-elf-gcc (dry-run)"

    eoc = {
        "eoc_id":   sw_tag,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "sealed":   True,
        "components": {
            "fcs_model": {
                "tag":     snap["fcs_model"]["tag"],
                "git_sha": snap["fcs_model"]["git_sha"],
                "submodules": {
                    "Common_lib": {"tag": snap["Common_lib"]["tag"], "git_sha": snap["Common_lib"]["git_sha"]},
                    "Controller": {"tag": snap["Controller"]["tag"], "git_sha": snap["Controller"]["git_sha"]},
                    "VMS_model":  {"tag": snap["VMS_model"]["tag"],  "git_sha": snap["VMS_model"]["git_sha"]},
                },
                "artifacts": crcs["fcs_model"],
            },
            "ofp": {
                "tag":     snap["ofp"]["tag"],
                "git_sha": snap["ofp"]["git_sha"],
                "artifacts": crcs["ofp"],
            },
        },
        "toolchain": {
            "compiler": "aarch64-none-elf-gcc",
            "version":  toolchain_ver.splitlines()[0] if toolchain_ver else "unknown",
            "path":     "/opt/gcc-arm-9.2-2019.12-x86_64-aarch64-none-elf/",
        },
    }

    if dry_run:
        print(f"  [dry-run] Would write {eoc_path}:")
        print(json.dumps(eoc, indent=2))
    else:
        eoc_path.write_text(json.dumps(eoc, indent=2) + "\n")
        print(f"  Written: {eoc_path}")

    return eoc_path


def step_commit_and_tag(sw_tag: str, eoc_path: Path, dry_run: bool):
    print(f"\n  Committing and tagging as {sw_tag}...")
    cmds = [
        ["git", "add", str(VL_FILE), str(eoc_path)],
        ["git", "commit", "-m", f"EOC release {sw_tag}"],
        ["git", "tag", "-a", sw_tag, "-m", f"EOC {sw_tag}"],
        ["git", "push", "origin", "master"],
        ["git", "push", "origin", sw_tag],
    ]
    for cmd in cmds:
        if dry_run:
            print(f"  [dry-run] {' '.join(cmd)}")
        else:
            run(cmd, cwd=ROOT)


# ---------------------------------------------------------------------------
# Verify mode — replay build and compare CRCs
# ---------------------------------------------------------------------------

def verify_mode(dry_run: bool, skip_docker: bool):
    print("\n=== VERIFY MODE ===")
    if not VL_FILE.exists():
        sys.exit("ERROR: versions.lock not found.")

    lock = json.loads(VL_FILE.read_text())
    print(f"Verifying EOC: {lock['sw_integration']}")

    # Check each submodule matches the lock
    for name, path in SUBMODULES.items():
        tag = run(["git", "describe", "--exact-match", "--tags", "HEAD"],
                  cwd=path, capture=True)
        if name == "fcs_model":
            expected = lock["fcs_model"]["tag"]
        elif name == "ofp":
            expected = lock["ofp"]["tag"]
        else:
            expected = lock["fcs_model"]["submodules"][name]

        if tag != expected:
            sys.exit(f"ERROR: {name} is on '{tag}', expected '{expected}'.")
        print(f"  {name}: {tag} OK")

    # Rebuild and compare CRCs
    fcs_build = step_build_fcs_model(dry_run, skip_docker)
    ofp_build = step_build_ofp(fcs_build, dry_run, skip_docker)
    crcs      = step_crc(fcs_build, ofp_build, dry_run)

    eoc_id   = lock["sw_integration"]
    eoc_path = EOC_DIR / f"{eoc_id}.eoc.json"
    if not eoc_path.exists():
        sys.exit(f"ERROR: EOC file not found: {eoc_path}")

    eoc = json.loads(eoc_path.read_text())
    mismatches = []
    for repo in ("fcs_model", "ofp"):
        for art, info in crcs[repo].items():
            recorded = eoc["components"][repo]["artifacts"].get(art, {})
            if info["crc32"] != recorded.get("crc32"):
                mismatches.append(
                    f"  {repo}/{art}: got {info['crc32']}, recorded {recorded.get('crc32')}"
                )

    if mismatches:
        print("\nCRC MISMATCHES:")
        for m in mismatches:
            print(m)
        sys.exit("VERIFY FAILED — build is not bit-for-bit identical.")
    else:
        print("\nVERIFY PASSED — build is bit-for-bit identical to EOC record.")


# ---------------------------------------------------------------------------
# Version bump helper
# ---------------------------------------------------------------------------

def next_sw_tag(bump: str) -> str:
    result = run(
        ["git", "tag", "--list", f"{SW_TAG_PREFIX}*", "--sort=-v:refname"],
        cwd=ROOT, capture=True,
    )
    tags = [t for t in result.splitlines() if t.startswith(SW_TAG_PREFIX)]
    if not tags:
        return f"{SW_TAG_PREFIX}1.0.0"

    latest = tags[0][len(SW_TAG_PREFIX):]
    x, y, z = map(int, latest.split("."))
    if bump == "x":
        x, y, z = x + 1, 0, 0
    elif bump == "y":
        y, z = y + 1, 0
    else:
        z += 1
    return f"{SW_TAG_PREFIX}{x}.{y}.{z}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="EOC release orchestrator")
    parser.add_argument("--bump", choices=["x", "y", "z"], default="z",
                        help="Version component to increment (default: z)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without making changes")
    parser.add_argument("--skip-docker", action="store_true",
                        help="Skip Docker-based build steps")
    parser.add_argument("--verify-only", action="store_true",
                        help="Verify an existing EOC without creating a new one")
    args = parser.parse_args()

    if args.verify_only:
        verify_mode(args.dry_run, args.skip_docker)
        return

    sw_tag = next_sw_tag(args.bump)
    print(f"\n=== EOC RELEASE: {sw_tag} ===")

    snap      = step_snapshot()
    step_write_versions_lock(snap, sw_tag, args.dry_run)
    fcs_build = step_build_fcs_model(args.dry_run, args.skip_docker)
    ofp_build = step_build_ofp(fcs_build, args.dry_run, args.skip_docker)
    crcs      = step_crc(fcs_build, ofp_build, args.dry_run)
    eoc_path  = step_write_eoc(sw_tag, snap, crcs, args.dry_run)
    step_commit_and_tag(sw_tag, eoc_path, args.dry_run)

    print(f"\n=== DONE — EOC {sw_tag} sealed ===")


if __name__ == "__main__":
    main()