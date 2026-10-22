"""Git 交互：读取暂存区 diff、解析 hunk、读取暂存区文件内容。"""

from __future__ import annotations

import codecs
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set, Tuple

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DIFF_HEADER_RE = re.compile(r"^diff --git (.+)$")
_QUOTED_PAIR_RE = re.compile(r'^("(?:[^"\\]|\\.)*")\s+("(?:[^"\\]|\\.)*")$')


class GitError(Exception):
    """Git 命令执行失败。"""


@dataclass
class DiffLine:
    kind: str  # " " 上下文 / "+" 新增 / "-" 删除
    old_no: Optional[int]
    new_no: Optional[int]
    text: str


@dataclass
class Hunk:
    header: str
    new_start: int
    new_count: int
    lines: List[DiffLine] = field(default_factory=list)

    def new_range(self) -> Tuple[int, int]:
        return self.new_start, self.new_start + max(self.new_count - 1, 0)


@dataclass
class ChangedFile:
    path: str
    old_path: Optional[str] = None
    status: str = "M"  # A 新增 / M 修改 / D 删除 / R 重命名
    binary: bool = False
    hunks: List[Hunk] = field(default_factory=list)
    raw_lines: List[str] = field(default_factory=list)

    @property
    def changed_lines(self) -> int:
        return sum(1 for hunk in self.hunks for line in hunk.lines if line.kind in "+-")

    def added_lines(self) -> List[DiffLine]:
        return [line for hunk in self.hunks for line in hunk.lines if line.kind == "+"]

    def touched_new_lines(self) -> Set[int]:
        return {line.new_no for line in self.added_lines() if line.new_no is not None}

    def hunk_ranges(self) -> List[Tuple[int, int]]:
        return [hunk.new_range() for hunk in self.hunks]

    def diff_text(self) -> str:
        return "\n".join(self.raw_lines) + "\n"


def run_git(repo_root: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo_root), capture_output=True)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)} 执行失败: {stderr}")
    return proc.stdout.decode("utf-8", "replace")


def repo_root(cwd: Path) -> Path:
    return Path(run_git(cwd, "rev-parse", "--show-toplevel").strip())


def staged_diff(root: Path) -> str:
    return run_git(
        root,
        "diff",
        "--cached",
        "--unified=3",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
    )


def staged_content(root: Path, path: str) -> Optional[str]:
    """读取暂存区里的文件内容；二进制或读取失败返回 None。"""
    proc = subprocess.run(["git", "show", f":{path}"], cwd=str(root), capture_output=True)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def parse_diff(text: str) -> List[ChangedFile]:
    files: List[ChangedFile] = []
    current: Optional[ChangedFile] = None
    hunk: Optional[Hunk] = None
    old_no = 0
    new_no = 0

    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            current = _start_file(raw)
            files.append(current)
            hunk = None
            continue
        if current is None:
            continue

        current.raw_lines.append(raw)

        if raw.startswith("@@"):
            matched = _HUNK_RE.match(raw)
            if not matched:
                hunk = None
                continue
            hunk = Hunk(
                header=raw,
                new_start=int(matched.group(3)),
                new_count=int(matched.group(4) or 1),
            )
            current.hunks.append(hunk)
            old_no = int(matched.group(1))
            new_no = hunk.new_start
            continue

        if hunk is None:
            _apply_header(current, raw)
            continue

        if raw.startswith("+"):
            hunk.lines.append(DiffLine("+", None, new_no, raw[1:]))
            new_no += 1
        elif raw.startswith("-"):
            hunk.lines.append(DiffLine("-", old_no, None, raw[1:]))
            old_no += 1
        elif raw.startswith(" "):
            hunk.lines.append(DiffLine(" ", old_no, new_no, raw[1:]))
            old_no += 1
            new_no += 1
        # "\ No newline at end of file" 等标记行直接跳过

    return files


def _start_file(raw: str) -> ChangedFile:
    matched = _DIFF_HEADER_RE.match(raw)
    old_path, new_path = _split_pair(matched.group(1) if matched else "")
    return ChangedFile(
        path=new_path or old_path,
        old_path=old_path or None,
        raw_lines=[raw],
    )


def _apply_header(file: ChangedFile, raw: str) -> None:
    if raw.startswith("new file mode"):
        file.status = "A"
    elif raw.startswith("deleted file mode"):
        file.status = "D"
    elif raw.startswith("rename from "):
        file.old_path = _unquote(raw[len("rename from "):])
        file.status = "R"
    elif raw.startswith("rename to "):
        file.path = _unquote(raw[len("rename to "):])
        file.status = "R"
    elif raw.startswith("Binary files "):
        file.binary = True
    elif raw.startswith("--- "):
        path = _strip_prefix(_unquote(raw[4:]))
        if path and path != "/dev/null" and not file.old_path:
            file.old_path = path
    elif raw.startswith("+++ "):
        path = _strip_prefix(_unquote(raw[4:]))
        if path and path != "/dev/null":
            file.path = path


def _split_pair(rest: str) -> Tuple[str, str]:
    quoted = _QUOTED_PAIR_RE.match(rest)
    if quoted:
        return (
            _strip_prefix(_unquote(quoted.group(1))),
            _strip_prefix(_unquote(quoted.group(2))),
        )
    index = rest.find(" b/")
    if index <= 0:
        return "", ""
    return _strip_prefix(rest[:index]), _strip_prefix(rest[index + 1:])


def _strip_prefix(path: str) -> str:
    return path[2:] if path[:2] in ("a/", "b/") else path


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            decoded = codecs.escape_decode(value[1:-1].encode("utf-8"))[0]
            return decoded.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return value[1:-1]
    return value
