"""规范来源加载：v1 支持仓库内本地文件，GitLab 源留待下一版接入。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .config import Config

RULE_ID_RE = re.compile(r"<!--\s*rule:\s*([A-Za-z0-9_\-\.]+)\s*-->")
DEFAULT_RULES_PATH = ".ai-review/rules.md"


class ConventionsUnavailable(Exception):
    """规范全部加载失败。"""

    def __init__(self, problems: List[str]):
        super().__init__("；".join(problems))
        self.problems = problems


@dataclass
class SourceDoc:
    uri: str
    name: str
    text: str
    rule_ids: Set[str] = field(default_factory=set)


@dataclass
class ConventionSet:
    docs: List[SourceDoc] = field(default_factory=list)
    text: str = ""
    rule_ids: Set[str] = field(default_factory=set)
    rule_sources: Dict[str, str] = field(default_factory=dict)
    fingerprint: str = ""
    notices: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.text.strip())


def load_conventions(repo_root: Path, cfg: Config, extensions: Set[str]) -> ConventionSet:
    problems: List[str] = []
    docs: List[SourceDoc] = []

    for uri in _resolve_sources(repo_root, cfg):
        try:
            doc = _load_one(repo_root, uri, extensions)
        except FileNotFoundError:
            problems.append(f"{uri} 文件不存在")
            continue
        except ValueError as exc:
            problems.append(f"{uri}: {exc}")
            continue
        if doc is not None:
            docs.append(doc)

    if not docs and problems:
        raise ConventionsUnavailable(problems)

    docs, notices = _trim_by_budget(docs, cfg.conventions.max_tokens)
    return _build(docs, notices)


def _resolve_sources(repo_root: Path, cfg: Config) -> List[str]:
    """未显式配置时，自动使用仓库内的 .ai-review/rules.md。"""
    if cfg.conventions.sources:
        return cfg.conventions.sources
    if (repo_root / DEFAULT_RULES_PATH).is_file():
        return [DEFAULT_RULES_PATH]
    return []


def _load_one(repo_root: Path, uri: str, extensions: Set[str]) -> Optional[SourceDoc]:
    if uri.startswith("gitlab://"):
        raise ValueError("当前版本仅支持本地规范文件，gitlab:// 源尚未接入")

    path = _resolve_local(repo_root, uri)
    if not path.is_file():
        raise FileNotFoundError(uri)

    text = path.read_text(encoding="utf-8", errors="replace")
    meta, body, applies_to = _split_front_matter(text)
    if applies_to and extensions and not _applies_to(applies_to, extensions):
        return None

    return SourceDoc(uri=uri, name=path.name, text=body, rule_ids=set(RULE_ID_RE.findall(text)))


def _build(docs: List[SourceDoc], notices: List[str]) -> ConventionSet:
    blocks: List[str] = []
    rule_ids: Set[str] = set()
    rule_sources: Dict[str, str] = {}
    digest = hashlib.sha256()

    for doc in docs:
        blocks.append(f"### 规范源: {doc.name} ({doc.uri})\n\n{doc.text.strip()}")
        digest.update(doc.uri.encode("utf-8"))
        digest.update(doc.text.encode("utf-8"))
        for rule_id in doc.rule_ids:
            rule_ids.add(rule_id)
            rule_sources[rule_id] = doc.uri

    return ConventionSet(
        docs=docs,
        text="\n\n".join(blocks),
        rule_ids=rule_ids,
        rule_sources=rule_sources,
        fingerprint=digest.hexdigest()[:8],
        notices=notices,
    )


def _trim_by_budget(docs: List[SourceDoc], max_tokens: int) -> Tuple[List[SourceDoc], List[str]]:
    """按字符数粗估 token 预算，超出时按顺序保留并在报告里提示。"""
    if max_tokens <= 0:
        return docs, []
    budget = max_tokens * 2
    kept: List[SourceDoc] = []
    used = 0
    for doc in docs:
        size = len(doc.text)
        if kept and used + size > budget:
            break
        kept.append(doc)
        used += size
    if len(kept) < len(docs):
        dropped = [doc.uri for doc in docs[len(kept):]]
        return kept, [f"规范超出 token 预算，未加载: {', '.join(dropped)}"]
    return kept, []


def _resolve_local(repo_root: Path, uri: str) -> Path:
    raw = uri[len("file://"):] if uri.startswith("file://") else uri
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (repo_root / path)


def _split_front_matter(text: str) -> Tuple[Dict[str, str], str, List[str]]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text, []

    meta: Dict[str, str] = {}
    for index in range(1, len(lines)):
        line = lines[index].strip()
        if line == "---":
            return meta, "\n".join(lines[index + 1:]), _parse_list(meta.get("applies_to", ""))
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return {}, text, []


def _parse_list(value: str) -> List[str]:
    items = value.strip().strip("[]")
    if not items:
        return []
    return [item.strip().strip("\"'") for item in items.split(",") if item.strip().strip("\"'")]


def _applies_to(patterns: List[str], extensions: Set[str]) -> bool:
    normalized: Set[str] = set()
    for pattern in patterns:
        pattern = pattern.strip()
        if pattern.startswith("*"):
            pattern = pattern[1:]
        if not pattern.startswith("."):
            pattern = "." + pattern
        normalized.add(pattern.lower())
    return any(extension.lower() in normalized for extension in extensions)
