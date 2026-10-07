from __future__ import annotations

"""Path policy for any coding agent. The check is prefix-based and fails closed."""

PROTECTED_PATHS = (
    "apps/engine/app/risk/",
    "apps/engine/app/execution/binance_live.py",
    "apps/engine/app/execution/live/",
    "core/risk/",
    "core/execution/live/",
    "security/",
    "secrets/",
    "kill_switch/",
    "capital_limits/",
    ".env",
    "apps/web/.env",
)

ALLOWED_PATHS = (
    "apps/engine/app/strategies/",
    "apps/engine/app/features/",
    "apps/engine/app/signals/",
    "apps/engine/app/filters/",
    "apps/engine/app/regime/",
    "apps/engine/app/providers/openai/prompts.py",
    "apps/engine/app/providers/jev/prompts/",
    "packages/configs/baseline_weights.yaml",
    "packages/configs/strategies.yaml",
    "strategies/",
    "features/",
    "signals/",
    "filters/",
    "regime/",
    "prompts/openai/",
    "prompts/jev/",
)


def _norm(path: str) -> str:
    name = path.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    return name.lstrip("/")


def is_protected(path: str) -> bool:
    name = _norm(path)
    if name == ".env" or name.endswith("/.env") or name.endswith(".env.local"):
        return True
    return any(name == item.rstrip("/") or name.startswith(item) for item in PROTECTED_PATHS)


def is_allowed(path: str) -> bool:
    if is_protected(path):
        return False
    name = _norm(path)
    return any(name == item.rstrip("/") or name.startswith(item) for item in ALLOWED_PATHS)


def protected_changes(paths: list[str]) -> list[str]:
    return [path for path in paths if is_protected(path)]


def main() -> None:
    import subprocess
    import sys

    base = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    try:
        diff = subprocess.check_output(["git", "diff", "--name-only", f"{base}...HEAD"], text=True, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("VALIDATION ERROR: no diff base")
        raise SystemExit(1)
    blocked = protected_changes([line for line in diff.splitlines() if line.strip()])
    if blocked:
        print("PROTECTED_CODE_CHANGED")
        for path in blocked:
            print(path)
        raise SystemExit(1)
    print("protected paths clean")


if __name__ == "__main__":
    main()
