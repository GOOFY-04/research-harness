#!/usr/bin/env python3
"""Install, update, verify, and optionally start Research Harness."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parent
OPENCODE_DIRNAME = "opencode-fork"
DEFAULT_OPENCODE_URL = "https://github.com/GOOFY-04/opencode.git"
DEFAULT_OPENCODE_BRANCH = "research-harness"


class DeploymentError(RuntimeError):
    """A deployment precondition or command failed."""


def command_text(command: Sequence[os.PathLike[str] | str]) -> str:
    return subprocess.list2cmdline([os.fspath(item) for item in command])


def run(
    command: Sequence[os.PathLike[str] | str],
    *,
    cwd: Path = ROOT,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    rendered = command_text(command)
    print(f"\n> {rendered}", flush=True)
    try:
        return subprocess.run(
            [os.fspath(item) for item in command],
            cwd=cwd,
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
        )
    except FileNotFoundError as exc:
        raise DeploymentError(f"Command is unavailable: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        if capture:
            if exc.stdout:
                print(exc.stdout.rstrip())
            if exc.stderr:
                print(exc.stderr.rstrip(), file=sys.stderr)
        raise DeploymentError(
            f"Command failed with exit code {exc.returncode}: {rendered}"
        ) from exc


def require_supported_python() -> None:
    if sys.version_info < (3, 10):
        version = ".".join(map(str, sys.version_info[:3]))
        raise DeploymentError(f"Python 3.10+ is required; found {version}")


def require_tool(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise DeploymentError(
            f"Required command '{name}' was not found on PATH. Install it and rerun."
        )
    return executable


def venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def ensure_virtualenv(root: Path) -> Path:
    interpreter = venv_python(root)
    if not interpreter.is_file():
        print("\n[1/6] Creating Python virtual environment")
        run([sys.executable, "-m", "venv", root / ".venv"], cwd=root)
    else:
        print("\n[1/6] Reusing Python virtual environment")
    return interpreter


def ensure_env_file(root: Path) -> str:
    destination = root / ".env"
    if destination.exists():
        return "preserved"
    template = root / ".env.example"
    if not template.is_file():
        raise DeploymentError(f"Missing environment template: {template}")
    shutil.copy2(template, destination)
    return "created"


def env_has_api_key(root: Path) -> bool:
    if os.environ.get("AGNES_API_KEY", "").strip():
        return True
    env_file = root / ".env"
    if not env_file.is_file():
        return False
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != "AGNES_API_KEY":
            continue
        normalized = value.strip().strip("'\"")
        return bool(normalized and normalized != "your_agnes_api_key")
    return False


def normalized_repository(url: str) -> str:
    value = url.strip().lower().replace("\\", "/")
    if value.endswith(".git"):
        value = value[:-4]
    return value.rstrip("/")


def remote_matches(actual: str, expected: str) -> bool:
    actual_value = normalized_repository(actual)
    expected_value = normalized_repository(expected)
    if actual_value == expected_value:
        return True
    expected_path = expected_value.split("github.com", 1)[-1].lstrip("/: ")
    return actual_value.endswith(f"github.com/{expected_path}") or actual_value.endswith(
        f"github.com:{expected_path}"
    )


def prepare_opencode(
    root: Path,
    git: str,
    *,
    repository: str,
    branch: str,
    update: bool,
) -> Path:
    target = root / OPENCODE_DIRNAME
    if not target.exists():
        print("\n[2/6] Cloning the OpenCode research fork")
        run(
            [
                git,
                "clone",
                "--filter=blob:none",
                "--single-branch",
                "--branch",
                branch,
                repository,
                target,
            ],
            cwd=root,
        )
        return target

    if not (target / ".git").exists():
        raise DeploymentError(f"{target} exists but is not a Git checkout")

    print("\n[2/6] Checking the OpenCode research fork")
    actual_remote = run(
        [git, "remote", "get-url", "origin"], cwd=target, capture=True
    ).stdout.strip()
    if not remote_matches(actual_remote, repository):
        raise DeploymentError(
            f"Refusing to update unexpected OpenCode origin: {actual_remote}"
        )

    changes = run([git, "status", "--porcelain"], cwd=target, capture=True).stdout
    if changes.strip():
        raise DeploymentError(
            f"{target} has local changes. Commit or stash them before deployment."
        )

    if not update:
        print("OpenCode update skipped by --no-update.")
        return target

    run([git, "fetch", "origin", branch], cwd=target)
    current_branch = run(
        [git, "branch", "--show-current"], cwd=target, capture=True
    ).stdout.strip()
    if current_branch != branch:
        local_branch = run(
            [git, "branch", "--list", branch], cwd=target, capture=True
        ).stdout.strip()
        if local_branch:
            run([git, "checkout", branch], cwd=target)
        else:
            run([git, "checkout", "--track", "-b", branch, f"origin/{branch}"], cwd=target)
    run([git, "merge", "--ff-only", f"origin/{branch}"], cwd=target)
    return target


def install(root: Path, python: Path, opencode: Path, bun: str) -> None:
    print("\n[3/6] Installing Python dependencies")
    run(
        [
            python,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            "requirements-dev.txt",
        ],
        cwd=root,
    )

    state = ensure_env_file(root)
    print(f"\n[4/6] Environment file {state}: {root / '.env'}")
    if not env_has_api_key(root):
        print(
            "AGNES_API_KEY is still unset. Deployment checks can run, but model "
            "requests require a key in .env or the process environment."
        )

    print("\n[5/6] Installing OpenCode dependencies")
    run([bun, "install", "--frozen-lockfile"], cwd=opencode)


def verify(root: Path, python: Path, opencode: Path, bun: str) -> None:
    print("\n[6/6] Verifying the integrated deployment")
    run(
        [
            python,
            "-c",
            (
                "from main import load_config; "
                "load_config(); "
                "print('Harness configuration: OK')"
            ),
        ],
        cwd=root,
    )
    run([python, "-m", "pytest", "-q"], cwd=root)
    run([bun, "run", "--cwd", "packages/research", "typecheck"], cwd=opencode)
    run([bun, "run", "research", "--help"], cwd=opencode)
    run([bun, "run", "--cwd", "packages/opencode", "script/research-runtime-check.ts"], cwd=opencode)
    run([bun, "run", "dev:harness", "--help"], cwd=opencode)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deploy the Python harness and its OpenCode research CLI."
    )
    parser.add_argument(
        "--start",
        action="store_true",
        help="start the OpenCode research CLI after a successful deployment",
    )
    parser.add_argument(
        "--skip-checks",
        action="store_true",
        help="skip the Python tests and TypeScript checks",
    )
    parser.add_argument(
        "--no-update",
        action="store_true",
        help="do not fetch or fast-forward an existing OpenCode checkout",
    )
    parser.add_argument(
        "--opencode-url",
        default=DEFAULT_OPENCODE_URL,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--opencode-branch",
        default=DEFAULT_OPENCODE_BRANCH,
        help=argparse.SUPPRESS,
    )
    return parser


def deploy(args: argparse.Namespace, *, root: Path = ROOT) -> int:
    require_supported_python()
    git = require_tool("git")
    bun = require_tool("bun")

    print(f"Research Harness deployment\nWorkspace: {root}")
    python = ensure_virtualenv(root)
    opencode = prepare_opencode(
        root,
        git,
        repository=args.opencode_url,
        branch=args.opencode_branch,
        update=not args.no_update,
    )
    install(root, python, opencode, bun)
    if args.skip_checks:
        print("\n[6/6] Verification skipped by --skip-checks")
    else:
        verify(root, python, opencode, bun)

    print("\nDeployment complete.")
    if args.start:
        print("Starting the Research Harness CLI. Press Ctrl+C to stop it.\n")
        try:
            completed = subprocess.run([bun, "run", "dev:harness"], cwd=opencode)
            return completed.returncode
        except KeyboardInterrupt:
            return 130

    print("Start it with: python deploy.py --start")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return deploy(args)
    except DeploymentError as exc:
        print(f"\nDeployment failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
