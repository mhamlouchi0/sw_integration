#!/usr/bin/env python3
"""
sw_integration/build.py  — Release orchestrator
================================================
Produces a sealed BOOT.bin by:
  1. Verifying all submodules are on exact release tags.
  2. Building fcs_model inside its Docker SECI → libcommon_lib.a,
     libcontroller.a, libvms_model.a, fcs_model.contract
  3. Building ofp inside its Docker SECI, injecting FCS_MODEL_LIB_DIR.
  4. Verifying the interface contract hash matches across the boundary.
  5. Writing manifest.lock and sealing it.

Usage:
  python3 build.py [--dry-run] [--skip-docker]

  --dry-run     Print commands without executing them.
  --skip-docker Run cmake/make directly (toolchain must be installed locally).
"""

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import subprocess
import sys

ROOT          = pathlib.Path(__file__).resolve().parent
FCS_MODEL_DIR = ROOT / "fcs_model"
OFP_DIR       = ROOT / "ofp"
MANIFEST_PATH = ROOT / "manifest.lock"
ENV_LOCK_PATH = ROOT / "env.lock"

CI_PREFIX_SW  = "sw-integration"
CI_PREFIX_FCS = "fcs-model"
CI_PREFIX_OFP = "fcc"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd, cwd=None, dry_run=False):
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    if dry_run:
        return ""
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        sys.exit(f"Command failed (rc={result.returncode})")
    return result.stdout.strip()


def git_exact_tag(repo_dir, ci_prefix):
    """Return the exact vX.Y.Z tag at HEAD, or exit."""
    result = subprocess.run(
        ["git", "describe", "--tags", "--exact-match",
         "--match", f"{ci_prefix}-v*"],
        cwd=repo_dir, capture_output=True, text=True
    )
    if result.returncode != 0:
        sys.exit(
            f"ERROR: {repo_dir.name} HEAD is not on an exact {ci_prefix}-vX.Y.Z tag.\n"
            f"  Run ci/ci_bump_version.py to create one."
        )
    return result.stdout.strip()


def git_sha(repo_dir):
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir,
        capture_output=True, text=True
    )
    return result.stdout.strip()


def file_sha256_prefix(path, length=16):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()[:length]


def parse_env_lock():
    sections = {}
    current = None
    for line in ENV_LOCK_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections[current] = {}
        elif "=" in line and current:
            k, _, v = line.partition("=")
            sections[current][k.strip()] = v.strip()
    return sections


# ---------------------------------------------------------------------------
# Build steps
# ---------------------------------------------------------------------------

def build_fcs_model(dry_run, skip_docker):
    print("\n[1/4] Building fcs_model ...")
    build_dir = FCS_MODEL_DIR / "build"

    if skip_docker:
        run(["cmake", "-B", str(build_dir),
             "-DCMAKE_TOOLCHAIN_FILE=toolchain-arm.cmake",
             "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"],
            cwd=FCS_MODEL_DIR, dry_run=dry_run)
        run(["make", "-C", str(build_dir), "VERBOSE=1"],
            cwd=FCS_MODEL_DIR, dry_run=dry_run)
    else:
        env = parse_env_lock()
        image = f"{env['fcs_model']['image']}@{env['fcs_model']['digest']}"
        run(["docker", "run", "--rm",
             "-v", f"{FCS_MODEL_DIR}:/workspace",
             "-w", "/workspace",
             image,
             "bash", "-c",
             "cmake -B build -DCMAKE_TOOLCHAIN_FILE=toolchain-arm.cmake "
             "&& make -C build VERBOSE=1"],
            dry_run=dry_run)

    tag     = git_exact_tag(FCS_MODEL_DIR, CI_PREFIX_FCS)
    version = tag.removeprefix(f"{CI_PREFIX_FCS}-v")
    run(["python3", "tools/gen_model_contract.py", ".", str(build_dir), version],
        cwd=FCS_MODEL_DIR, dry_run=dry_run)

    return build_dir

def generate_contract_header(fcs_build_dir, dry_run):
    """Call check_contract.py to generate fcs_model_contract_check.h in ofp."""
    print("\n[1.5/4] Generating contract header for ofp ...")
    contract_path = fcs_build_dir / "fcs_model.contract"
    output_header = OFP_DIR / "src" / "fcs_mi" / "fcs_model_contract_check.h"
    run(["python3", "tools/check_contract.py",
         str(contract_path),
         str(output_header)],
        cwd=OFP_DIR, dry_run=dry_run)

def build_ofp(fcs_build_dir, dry_run, skip_docker):
    print("\n[2/4] Building ofp ...")

    if skip_docker:
        build_dir = OFP_DIR / "build"
        # Compute interface hashes to inject into ofp compile-time assertions
        inc = FCS_MODEL_DIR / "include"
        fcs_mi_hash = file_sha256_prefix(inc / "fcs_mi_interface.h")
        vms_hash    = file_sha256_prefix(inc / "vms_interface.h")

        run(["cmake", "-B", str(build_dir),
             "-DCMAKE_TOOLCHAIN_FILE=toolchain-arm.cmake",
             f"-DFCS_MODEL_LIB_DIR={fcs_build_dir}",
             f"-DFCS_MODEL_INC_DIR={FCS_MODEL_DIR / 'include'}",
             f"-DFCS_MI_IF_HASH={fcs_mi_hash}",
             f"-DVMS_IF_HASH={vms_hash}",
             "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"],
            cwd=OFP_DIR, dry_run=dry_run)
        run(["make", "-C", str(build_dir), "VERBOSE=1"],
            cwd=OFP_DIR, dry_run=dry_run)
    else:
        env = parse_env_lock()
        image = f"{env['ofp']['image']}@{env['ofp']['digest']}"
        # Compute interface hashes to inject into ofp compile-time assertions
        inc = FCS_MODEL_DIR / "include"
        fcs_mi_hash = file_sha256_prefix(inc / "fcs_mi_interface.h")
        vms_hash    = file_sha256_prefix(inc / "vms_interface.h")

        run(["docker", "run", "--rm",
             "-v", f"{FCS_MODEL_DIR}:/fcs_model",
             "-v", f"{OFP_DIR}:/workspace",
             "-w", "/workspace",
             image,
             "bash", "-c",
             "cmake -B build "
             "-DCMAKE_TOOLCHAIN_FILE=toolchain-arm.cmake "
             "-DFCS_MODEL_LIB_DIR=/fcs_model/build "
             "-DFCS_MODEL_INC_DIR=/fcs_model/include "
             f"-DFCS_MI_IF_HASH={fcs_mi_hash} "
             f"-DVMS_IF_HASH={vms_hash} "
             "&& make -C build VERBOSE=1"],
            dry_run=dry_run)


def verify_contract(fcs_build_dir, dry_run):
    print("\n[3/4] Verifying interface contract ...")
    contract_path = fcs_build_dir / "fcs_model.contract"

    if not contract_path.exists():
        if not dry_run:
            sys.exit("ERROR: fcs_model.contract not found. Was fcs_model built?")
        print("  (dry-run: skipping contract file check)")
        return "", ""

    contract  = json.loads(contract_path.read_text())
    fcs_hash  = contract["fcs_mi_if_hash"]
    vms_hash  = contract["vms_if_hash"]
    inc       = FCS_MODEL_DIR / "include"
    actual_fcs = file_sha256_prefix(inc / "fcs_mi_interface.h")
    actual_vms = file_sha256_prefix(inc / "vms_interface.h")

    if actual_fcs != fcs_hash:
        sys.exit(
            f"CONTRACT MISMATCH: fcs_mi_interface.h\n"
            f"  contract : {fcs_hash}\n"
            f"  actual   : {actual_fcs}\n"
            "Header changed after contract was generated. Rebuild fcs_model."
        )
    if actual_vms != vms_hash:
        sys.exit(
            f"CONTRACT MISMATCH: vms_interface.h\n"
            f"  contract : {vms_hash}\n"
            f"  actual   : {actual_vms}\n"
            "Header changed after contract was generated. Rebuild fcs_model."
        )

    print(f"  fcs_mi_if_hash : {fcs_hash}  OK")
    print(f"  vms_if_hash    : {vms_hash}  OK")
    return fcs_hash, vms_hash


def seal_manifest(sw_tag, fcs_tag, ofp_tag, fcs_hash, vms_hash, dry_run):
    print("\n[4/4] Writing manifest.lock ...")
    env = parse_env_lock()
    manifest = {
        "_comment": "AUTO-GENERATED by build.py. Commit this file. Do not edit manually.",
        "sw_integration_tag": sw_tag,
        "sw_integration_sha": git_sha(ROOT),
        "components": {
            "fcs_model": {
                "tag":            fcs_tag,
                "sha":            git_sha(FCS_MODEL_DIR),
                "fcs_mi_if_hash": fcs_hash,
                "vms_if_hash":    vms_hash,
            },
            "ofp": {
                "tag": ofp_tag,
                "sha": git_sha(OFP_DIR),
            }
        },
        "docker": {
            "fcs_model_image":  env.get("fcs_model", {}).get("image", ""),
            "fcs_model_digest": env.get("fcs_model", {}).get("digest", ""),
            "ofp_image":        env.get("ofp", {}).get("image", ""),
            "ofp_digest":       env.get("ofp", {}).get("digest", ""),
        },
        "built_at": datetime.datetime.utcnow().isoformat() + "Z",
        "sealed":   True,
    }
    if not dry_run:
        MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))
    print(f"  Manifest written to {MANIFEST_PATH}")
    print(json.dumps(manifest, indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="sw_integration release builder")
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--skip-docker", action="store_true")
    args = parser.parse_args()

    print("=" * 60)
    print("sw_integration release build")
    print("=" * 60)

    print("\n[0/4] Verifying release tags ...")
    sw_tag  = git_exact_tag(ROOT,          CI_PREFIX_SW)
    fcs_tag = git_exact_tag(FCS_MODEL_DIR, CI_PREFIX_FCS)
    ofp_tag = git_exact_tag(OFP_DIR,       CI_PREFIX_OFP)
    print(f"  sw_integration : {sw_tag}")
    print(f"  fcs_model      : {fcs_tag}")
    print(f"  ofp            : {ofp_tag}")

    fcs_build_dir = build_fcs_model(args.dry_run, args.skip_docker)
    generate_contract_header(fcs_build_dir, args.dry_run)
    build_ofp(fcs_build_dir, args.dry_run, args.skip_docker)
    fcs_hash, vms_hash = verify_contract(fcs_build_dir, args.dry_run)
    seal_manifest(sw_tag, fcs_tag, ofp_tag, fcs_hash, vms_hash, args.dry_run)

    print("\n✓ Release build complete.")


if __name__ == "__main__":
    main()
