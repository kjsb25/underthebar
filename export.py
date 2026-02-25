#!/usr/bin/env python3
"""
Export script for Under the Bar.

Verifies the repo is on the main branch (clean), then builds a single-file
executable using PyInstaller.

Usage:
    python export.py [--skip-branch-check]

The output will be in the dist/ directory.
"""

import argparse
import os
import subprocess
import sys


def run(cmd, **kwargs):
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    return result


def check_git_branch():
    result = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if result.returncode != 0:
        print("ERROR: Could not determine current git branch.")
        sys.exit(1)
    branch = result.stdout.strip()
    if branch != "main":
        print(f"ERROR: Must be on 'main' branch to export. Currently on '{branch}'.")
        print("       Switch with: git checkout main")
        sys.exit(1)
    print(f"Branch check passed: on '{branch}'")


def check_git_clean():
    result = run(["git", "status", "--porcelain"])
    if result.returncode != 0:
        print("ERROR: Could not check git status.")
        sys.exit(1)
    if result.stdout.strip():
        print("ERROR: Working directory is not clean. Commit or stash changes before exporting.")
        print(result.stdout)
        sys.exit(1)
    print("Clean working directory check passed.")


def check_pyinstaller():
    result = run([sys.executable, "-m", "PyInstaller", "--version"])
    if result.returncode != 0:
        print("ERROR: PyInstaller not found. Install it with:")
        print("       pip install pyinstaller")
        sys.exit(1)
    print(f"PyInstaller found: {result.stdout.strip()}")


def build():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    icon = os.path.join(script_dir, "icons", "dumbbell-solid.ico")
    # --add-data separator is ':' on Linux/Mac, ';' on Windows
    sep = ";" if sys.platform == "win32" else ":"
    icons_data = f"icons{sep}icons"

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        f"--icon={icon}",
        f"--add-data={icons_data}",
        "--name=underthebar",
        "--clean",
        "underthebar.py",
    ]

    print("\nRunning PyInstaller:")
    print(" ".join(cmd))
    print()

    result = subprocess.run(cmd, cwd=script_dir)
    if result.returncode != 0:
        print("\nERROR: PyInstaller build failed.")
        sys.exit(1)

    exe = "underthebar.exe" if sys.platform == "win32" else "underthebar"
    output = os.path.join(script_dir, "dist", exe)
    print(f"\nBuild complete: {output}")


def main():
    parser = argparse.ArgumentParser(description="Export Under the Bar as a single executable.")
    parser.add_argument(
        "--skip-branch-check",
        action="store_true",
        help="Skip the git branch and clean checks (for development builds)",
    )
    args = parser.parse_args()

    if not args.skip_branch_check:
        check_git_branch()
        check_git_clean()
    else:
        print("Skipping branch/clean checks.")

    check_pyinstaller()
    build()


if __name__ == "__main__":
    main()
