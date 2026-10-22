"""结果校验、行号回显、拦截决策与终端渲染。"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import Config
from .filters import IgnoreRules, SkippedFile
from .gitio import ChangedFile
from .sources import ConventionSet


@dataclass
class Finding:
    rule_id: str
    file: str
    line: int
    severity: str
    confidence: float
    message: str
    suggestion: str
    verified: bool = True
    snippet: str = ""


@dataclass
class ReviewOutcome:
    findings: List[Finding] = field(default_factory=list)
    dropped: int = 0
    downgraded: int = 0
    ignored: int = 0

    @property
    def blocking(self) -> List[Finding]:
        return [item for item in self.findings if item.severity == "blocking"]

    @property
    def warnings(self) -> List[Finding]:
        return [item for item in self.findings if item.severity != "blocking"]


@dataclass
class RenderMeta:
    model: str
    conventions: ConventionSet
    file_count: int
    elapsed: float = 0.0
    usage: Dict[str, int] = field(default_factory=dict)
    notices: List[str] = field(default_factory=list)


def evaluate(
    raw_findings: List[Any],
    conventions: ConventionSet,
    files: List[ChangedFile],
    contents: Dict[str, str],
    ignores: IgnoreRules,
    cfg: Config,
) -> ReviewOutcome:
    """把模型输出校验成可信的 Finding：文件必须存在、行号必须在改动范围内、规则必须可查。"""
    outcome = ReviewOutcome()
    file_map = {item.path: item for item in files}
    seen = set()

    for raw in raw_findings:
        if not isinstance(raw, dict):
            outcome.dropped += 1
            continue

        path = str(raw.get("file") or "").strip()
        changed = file_map.get(path)
        if changed is None:
            outcome.dropped += 1
            continue

        line = _as_int(raw.get("line"))
        if line is None or line <= 0:
            outcome.dropped += 1
            continue

        rule_id = str(raw.get("rule_id") or "general").strip() or "general"
        if rule_id != "general" and conventions.rule_ids and rule_id not in conventions.rule_ids:
            outcome.dropped += 1
            continue

        severity = "blocking" if str(raw.get("severity", "")).lower() == "blocking" else "warning"
        confidence = _as_float(raw.get("confidence"))

        if severity == "blocking" and rule_id == "general":
            severity = "warning"
            outcome.downgraded += 1
        if severity == "blocking" and confidence < cfg.policy.min_confidence:
            severity = "warning"
            outcome.downgraded += 1

        if ignores.is_ignored(path, line, rule_id):
            outcome.ignored += 1
            continue

        key = (path, line, rule_id)
        if key in seen:
            continue
        seen.add(key)

        outcome.findings.append(
            Finding(
                rule_id=rule_id,
                file=path,
                line=line,
                severity=severity,
                confidence=confidence,
                message=str(raw.get("message") or "").strip(),
                suggestion=str(raw.get("suggestion") or "").strip(),
                verified=_line_is_touched(changed, line),
                snippet=_snippet(contents.get(path), line, cfg.review.context_lines),
            )
        )

    outcome.findings.sort(key=lambda item: (item.severity != "blocking", item.file, item.line))
    return outcome


def render(
    outcome: ReviewOutcome,
    meta: RenderMeta,
    cfg: Config,
    skipped: Optional[List[SkippedFile]] = None,
) -> str:
    palette = Palette(_color_enabled(cfg))
    blocking_total = len(outcome.blocking)
    warning_total = len(outcome.warnings)
    lines: List[str] = []

    if blocking_total:
        lines.append(palette.red(f"✗ AI Review 未通过：{blocking_total} 处阻断 / {warning_total} 处警告"))
    elif warning_total:
        lines.append(palette.yellow(f"✓ AI Review 通过，但有 {warning_total} 处警告"))
    else:
        lines.append(palette.green(f"✓ AI Review 通过（{meta.file_count} 个文件，耗时 {meta.elapsed:.1f}s）"))

    lines.append(palette.dim(_describe_meta(meta)))
    lines.extend(palette.dim(notice) for notice in meta.notices)

    counters = {"blocking": 0, "warning": 0}
    for finding in outcome.findings:
        counters[finding.severity] += 1
        lines.extend(
            _render_finding(
                finding=finding,
                index=counters[finding.severity],
                blocking_total=blocking_total,
                warning_total=warning_total,
                rule_sources=meta.conventions.rule_sources,
                palette=palette,
                cfg=cfg,
            )
        )

    if outcome.findings:
        lines.append("")
        lines.append(palette.bold(f"共 {blocking_total} 处阻断 / {warning_total} 处警告"))
        stats = _describe_stats(outcome)
        if stats:
            lines.append(palette.dim(stats))
        if blocking_total:
            lines.append("  修复后重新 git add 并提交；确需跳过本次审查时使用 git commit --no-verify")
        else:
            lines.append("  没有阻断项，可以正常提交")

    if skipped and outcome.findings:
        lines.append(palette.dim(_describe_skipped(skipped)))

    return "\n".join(lines)


def _render_finding(
    finding: Finding,
    index: int,
    blocking_total: int,
    warning_total: int,
    rule_sources: Dict[str, str],
    palette: "Palette",
    cfg: Config,
) -> List[str]:
    label = "阻断" if finding.severity == "blocking" else "警告"
    color = palette.red if finding.severity == "blocking" else palette.yellow
    total = blocking_total if finding.severity == "blocking" else warning_total

    lines = [
        "",
        "─" * 60,
        color(f"[{label} {index}/{total}]  {finding.file}:{finding.line}"),
    ]

    rule_text = f"  规则: {finding.rule_id}"
    source = rule_sources.get(finding.rule_id)
    if source:
        rule_text += f"  ({source})"
    lines.append(palette.cyan(rule_text))

    if cfg.report.show_snippet and finding.snippet:
        lines.append("")
        lines.extend("    " + row for row in finding.snippet.splitlines())

    if not finding.verified:
        lines.append(palette.yellow("    ↑ 该行号不在本次改动范围内，位置待人工确认"))
    if finding.message:
        lines.append(palette.dim(f"    ↑ {finding.message}"))
    if finding.suggestion:
        lines.append(f"      建议: {finding.suggestion}")

    return lines


def _describe_meta(meta: RenderMeta) -> str:
    if meta.conventions.docs:
        names = ", ".join(doc.name for doc in meta.conventions.docs)
        text = f"规范: {names} @{meta.conventions.fingerprint}"
    else:
        text = "规范: 未加载"
    text += f" · 模型: {meta.model}"
    if meta.elapsed:
        text += f" · 耗时 {meta.elapsed:.1f}s"
    prompt_tokens = meta.usage.get("prompt_tokens")
    completion_tokens = meta.usage.get("completion_tokens")
    if prompt_tokens is not None and completion_tokens is not None:
        text += f" · token {prompt_tokens}/{completion_tokens}"
    return text


def _describe_stats(outcome: ReviewOutcome) -> str:
    parts = []
    if outcome.ignored:
        parts.append(f"按忽略指令放过 {outcome.ignored} 条")
    if outcome.downgraded:
        parts.append(f"降级为警告 {outcome.downgraded} 条")
    if outcome.dropped:
        parts.append(f"丢弃不可信结论 {outcome.dropped} 条")
    return " · ".join(parts)


def _describe_skipped(skipped: List[SkippedFile]) -> str:
    shown = skipped[:5]
    text = "未审查: " + ", ".join(f"{item.path}({item.reason})" for item in shown)
    if len(skipped) > len(shown):
        text += f" 等 {len(skipped)} 个文件"
    return text


def _line_is_touched(file: ChangedFile, line: int) -> bool:
    if line in file.touched_new_lines():
        return True
    return any(start <= line <= end for start, end in file.hunk_ranges())


def _snippet(content: Optional[str], line: int, context: int) -> str:
    if not content or line <= 0:
        return ""
    rows = content.splitlines()
    if line > len(rows):
        return ""
    start = max(1, line - context)
    end = min(len(rows), line + context)
    width = len(str(end))
    output = []
    for number in range(start, end + 1):
        marker = ">" if number == line else " "
        output.append(f"{marker} {str(number).rjust(width)} | {rows[number - 1]}")
    return "\n".join(output)


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(max(number, 0.0), 1.0)


def _color_enabled(cfg: Config) -> bool:
    if not cfg.report.color or os.environ.get("NO_COLOR"):
        return False
    try:
        return sys.stdout.isatty()
    except (ValueError, AttributeError):
        return False


class Palette:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def red(self, text: str) -> str:
        return self._wrap("31;1", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def green(self, text: str) -> str:
        return self._wrap("32;1", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)
