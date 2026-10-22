"""pre-commit hook 的安装、卸载，以及与已有 hook 的串联。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

HOOK_MARK = "# >>> git-commit-review >>>"
BACKUP_NAME = "pre-commit.ai-review-backup"
HOOK_NAME = "pre-commit"


class HookError(Exception):
    """hook 安装或卸载失败。"""


def hooks_dir(root: Path) -> Path:
    configured = _git_config(root, "core.hooksPath")
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else root / path
    try:
        return Path(_rev_parse(root, "--path-format=absolute", "--git-path", "hooks"))
    except HookError:
        path = Path(_rev_parse(root, "--git-path", "hooks"))
        return path if path.is_absolute() else root / path


def install(root: Path, python_exe: str) -> Path:
    directory = hooks_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    hook = directory / HOOK_NAME
    backup = directory / BACKUP_NAME

    if hook.exists():
        content = hook.read_text(encoding="utf-8", errors="replace")
        if HOOK_MARK not in content:
            if backup.exists():
                raise HookError(f"{backup} 已存在，请先确认已有 hook 的处理方式")
            hook.rename(backup)

    hook.write_text(_script(python_exe), encoding="utf-8")
    hook.chmod(0o755)
    return hook


def uninstall(root: Path) -> Optional[Path]:
    """返回被还原的 hook 路径；原本没有其他 hook 时返回 None。"""
    directory = hooks_dir(root)
    hook = directory / HOOK_NAME
    backup = directory / BACKUP_NAME

    if not hook.exists():
        return None
    content = hook.read_text(encoding="utf-8", errors="replace")
    if HOOK_MARK not in content:
        raise HookError("现有 pre-commit 不是本工具安装的，未做任何修改")

    hook.unlink()
    if backup.exists():
        backup.rename(hook)
        return hook
    return None


def _script(python_exe: str) -> str:
    return f"""#!/bin/sh
{HOOK_MARK}
# 本文件由 aicr install 生成，请勿手工修改；卸载请执行 aicr uninstall

HOOK_DIR=$(cd "$(dirname "$0")" && pwd)
BACKUP="$HOOK_DIR/{BACKUP_NAME}"

if [ -n "$GIT_REVIEW_SKIP" ]; then
  exit 0
fi

if [ -x "$BACKUP" ]; then
  "$BACKUP" "$@" || exit $?
fi

exec "{python_exe}" -m review run
"""


def _git_config(root: Path, key: str) -> Optional[str]:
    proc = subprocess.run(["git", "config", "--get", key], cwd=str(root), capture_output=True)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").strip() or None


def _rev_parse(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", "rev-parse", *args], cwd=str(root), capture_output=True)
    if proc.returncode != 0:
        raise HookError(proc.stderr.decode("utf-8", "replace").strip() or "git rev-parse 执行失败")
    return proc.stdout.decode("utf-8", "replace").strip()
