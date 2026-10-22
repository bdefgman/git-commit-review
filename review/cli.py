"""命令行入口：aicr install / uninstall / run / version。"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import __version__, hooks
from .config import Config, ConfigError, load_config
from .filters import SkippedFile, collect_ignores, detect_generated, scan_secrets, select_files
from .gitio import ChangedFile, GitError, parse_diff, repo_root, staged_contents, staged_diff
from .llm import LLMError, request_review
from .prompt import build_messages
from .report import RenderMeta, evaluate, render
from .sources import ConventionSet, ConventionsUnavailable, load_conventions

DEBUG_DIR = Path.home() / ".cache" / "git-commit-review" / "debug"
_SKIP_VALUES = {"1", "true", "yes", "on"}


class ReviewAborted(Exception):
    """审查中断，携带最终退出码。"""

    def __init__(self, exit_code: int, message: str = ""):
        super().__init__(message)
        self.exit_code = exit_code


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"[ai-review] 配置错误: {exc}", file=sys.stderr)
        return 2
    except (GitError, hooks.HookError) as exc:
        print(f"[ai-review] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # 工具自身异常，不能伪装成"代码违规"
        if "--debug" in (argv if argv is not None else sys.argv):
            traceback.print_exc()
        return _handle_internal_error(exc)


def _handle_internal_error(exc: Exception) -> int:
    print(f"✗ [ai-review] 工具内部错误: {exc.__class__.__name__}: {exc}", file=sys.stderr)
    print("[ai-review] 这不是代码违规，本次审查已跳过，提交继续", file=sys.stderr)
    print("[ai-review] 复现并反馈：aicr run --debug", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aicr", description="提交前的 AI 代码规范审查")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="审查当前暂存区改动")
    run_parser.add_argument("--dry-run", action="store_true", help="只输出待审查内容与 prompt，不调用模型")
    run_parser.add_argument("--debug", action="store_true", help="把 prompt 与模型原始输出写入缓存目录")
    run_parser.add_argument("--no-color", action="store_true", help="关闭彩色输出")
    run_parser.set_defaults(func=cmd_run)

    install_parser = subparsers.add_parser("install", help="安装 pre-commit hook")
    install_parser.set_defaults(func=cmd_install)

    uninstall_parser = subparsers.add_parser("uninstall", help="卸载 pre-commit hook")
    uninstall_parser.set_defaults(func=cmd_uninstall)

    version_parser = subparsers.add_parser("version", help="显示版本")
    version_parser.set_defaults(func=cmd_version)

    return parser


def cmd_run(args: argparse.Namespace) -> int:
    if _skip_requested():
        print("[ai-review] 已通过 GIT_REVIEW_SKIP 跳过审查")
        return 0

    try:
        root = repo_root(Path.cwd())
    except GitError:
        print("[ai-review] 当前目录不在 Git 仓库内，跳过审查")
        return 0

    cfg = load_config(root)
    if args.no_color:
        cfg = dataclasses.replace(cfg, report=dataclasses.replace(cfg.report, color=False))

    started = time.time()
    parsed = parse_diff(staged_diff(root))
    if not parsed:
        print("[ai-review] 暂存区没有改动，跳过审查")
        return 0

    candidates, skipped = select_files(parsed, cfg)
    candidates, contents = _collect_candidates(root, candidates, skipped)
    if not candidates:
        print("[ai-review] 没有需要审查的代码改动，跳过审查")
        return 0

    secret_hits = scan_secrets(candidates)
    if secret_hits:
        return _report_secrets(secret_hits)

    candidates, budget_notices = _apply_budget(candidates, cfg)
    if not candidates:
        print("[ai-review] " + "；".join(budget_notices))
        return 0

    extensions = {Path(item.path).suffix.lower() for item in candidates}
    try:
        conventions = load_conventions(root, cfg, extensions)
    except ConventionsUnavailable as exc:
        return _handle_missing_conventions(exc, cfg)
    if not conventions:
        print("[ai-review] 未找到规范文件，跳过审查。请在仓库根目录创建 .ai-review/rules.md（写法见 README）")
        return 0

    batches = _split_batches(candidates, cfg)
    if args.dry_run:
        _print_dry_run(candidates, batches, conventions, budget_notices)
        return 0

    try:
        raw_findings, usage, elapsed = _review_batches(batches, conventions, cfg, args.debug)
    except ReviewAborted as exc:
        return exc.exit_code

    ignores = collect_ignores(candidates, cfg.ignore.inline_directive, contents)
    outcome = evaluate(raw_findings, conventions, candidates, contents, ignores, cfg)
    meta = RenderMeta(
        model=cfg.model.model,
        conventions=conventions,
        file_count=len(candidates),
        elapsed=elapsed or (time.time() - started),
        usage=usage,
        notices=conventions.notices + budget_notices,
    )
    print(render(outcome, meta, cfg, skipped))
    return 1 if outcome.blocking else 0


def cmd_install(args: argparse.Namespace) -> int:
    root = repo_root(Path.cwd())
    path = hooks.install(root, sys.executable)
    print(f"[ai-review] 已安装 pre-commit hook: {path}")
    print("[ai-review] 下一步：在仓库根目录创建 .ai-review/rules.md 写编码规范；可用 GIT_REVIEW_SKIP=1 临时跳过")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    root = repo_root(Path.cwd())
    restored = hooks.uninstall(root)
    if restored:
        print(f"[ai-review] 已卸载，并还原原有 hook: {restored}")
    else:
        print("[ai-review] 已卸载 pre-commit hook")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    print(f"git-commit-review {__version__}")
    return 0


def _collect_candidates(
    root: Path, files: List[ChangedFile], skipped: List[SkippedFile]
) -> Tuple[List[ChangedFile], Dict[str, str]]:
    contents = staged_contents(root, [item.path for item in files])
    kept: List[ChangedFile] = []
    kept_contents: Dict[str, str] = {}
    for item in files:
        content = contents.get(item.path)
        if content is None:
            skipped.append(SkippedFile(item.path, "无法读取暂存区内容"))
            continue
        if detect_generated(content):
            skipped.append(SkippedFile(item.path, "自动生成的代码"))
            continue
        kept.append(item)
        kept_contents[item.path] = content
    return kept, kept_contents


def _review_batches(
    batches: List[List[ChangedFile]],
    conventions: ConventionSet,
    cfg: Config,
    debug: bool,
) -> Tuple[List[Any], Dict[str, int], float]:
    """逐批调用模型；调用失败时按 policy 决定放行还是拦截。"""
    raw_findings: List[Any] = []
    usage: Dict[str, int] = {}
    elapsed = 0.0

    for index, batch in enumerate(batches, start=1):
        if len(batches) > 1:
            print(f"[ai-review] 审查第 {index}/{len(batches)} 批（{len(batch)} 个文件）...")
        messages = build_messages(conventions, "".join(item.diff_text() for item in batch))
        try:
            result = request_review(cfg.model, messages)
        except LLMError as exc:
            raise ReviewAborted(_report_llm_error(exc, cfg), str(exc)) from exc

        raw_findings.extend(result.payload.get("findings") or [])
        elapsed += result.elapsed
        for key, value in result.usage.items():
            usage[key] = usage.get(key, 0) + value
        if debug:
            _dump_debug(index, messages, result)

    return raw_findings, usage, elapsed


def _apply_budget(
    files: List[ChangedFile], cfg: Config
) -> Tuple[List[ChangedFile], List[str]]:
    limit = cfg.review.max_diff_lines
    total = sum(item.changed_lines for item in files)
    if limit <= 0 or total <= limit:
        return files, []

    mode = cfg.review.on_large_diff
    if mode == "skip":
        return [], [f"本次改动 {total} 行，超过 {limit} 行阈值，已跳过审查"]
    if mode == "new_files_only":
        kept = [item for item in files if item.status == "A"]
        return kept, [f"本次改动 {total} 行，超过 {limit} 行阈值，仅审查新增文件（{len(kept)} 个）"]

    batches = _split_batches(files, cfg)
    return files, [f"本次改动 {total} 行，超过 {limit} 行阈值，分 {len(batches)} 批审查"]


def _split_batches(files: List[ChangedFile], cfg: Config) -> List[List[ChangedFile]]:
    limit = cfg.review.max_diff_lines
    if limit <= 0 or sum(item.changed_lines for item in files) <= limit:
        return [files]

    batches: List[List[ChangedFile]] = []
    current: List[ChangedFile] = []
    size = 0
    for item in files:
        lines = item.changed_lines
        if current and size + lines > limit:
            batches.append(current)
            current, size = [], 0
        current.append(item)
        size += lines
    if current:
        batches.append(current)
    return batches


def _print_dry_run(
    files: List[ChangedFile],
    batches: List[List[ChangedFile]],
    conventions: ConventionSet,
    notices: List[str],
) -> None:
    print(f"[ai-review] 待审查 {len(files)} 个文件，{len(batches)} 批")
    for item in files:
        deleted = sum(1 for hunk in item.hunks for line in hunk.lines if line.kind == "-")
        print(f"  {item.path}  +{len(item.added_lines())} / -{deleted}")
    for notice in notices:
        print(f"[ai-review] {notice}")
    print(f"[ai-review] 规范指纹 {conventions.fingerprint}，规则 {len(conventions.rule_ids)} 条")
    for index, batch in enumerate(batches, start=1):
        messages = build_messages(conventions, "".join(item.diff_text() for item in batch))
        print(f"\n===== 第 {index} 批 system prompt =====")
        print(messages[0]["content"])
        print(f"\n===== 第 {index} 批 user prompt =====")
        print(messages[1]["content"])


def _report_secrets(hits) -> int:
    print("✗ 检测到疑似敏感信息，已阻止提交（这些内容不会发送给模型）")
    for hit in hits:
        print(f"  {hit.path}:{hit.line}  {hit.rule}  {hit.masked}")
    print("  请改用环境变量或配置中心，并从暂存区移除后重新提交")
    return 1


def _handle_missing_conventions(exc: ConventionsUnavailable, cfg: Config) -> int:
    message = "[ai-review] 规范加载失败: " + "；".join(exc.problems)
    if cfg.conventions.on_unavailable == "block":
        print(message + "（on_unavailable=block，已阻止提交）", file=sys.stderr)
        return 2
    print(message + "（on_unavailable=warn，本次跳过审查）", file=sys.stderr)
    return 0


def _report_llm_error(exc: LLMError, cfg: Config) -> int:
    """打印模型调用错误，返回最终退出码：放行 0，拦截 2。"""
    policy = cfg.policy.on_api_error if exc.kind == "api" else cfg.policy.on_invalid_json
    prefix = "模型调用失败" if exc.kind == "api" else "模型返回格式异常"
    if policy == "block":
        print(f"✗ {prefix}: {exc}", file=sys.stderr)
        print("[ai-review] 已按 policy 配置阻止提交", file=sys.stderr)
        return 2

    print(f"[ai-review] {prefix}: {exc}", file=sys.stderr)
    print("[ai-review] 已按 policy 配置放行本次提交（可改为 block 强制拦截）", file=sys.stderr)
    return 0


def _dump_debug(index: int, messages: List[Dict[str, str]], result) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path = DEBUG_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-batch{index}.json"
    payload = {"messages": messages, "response": result.raw_content, "usage": result.usage}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ai-review] debug 已写入 {path}")


def _skip_requested() -> bool:
    return os.environ.get("GIT_REVIEW_SKIP", "").strip().lower() in _SKIP_VALUES


if __name__ == "__main__":
    sys.exit(main())
