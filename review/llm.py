"""DeepSeek（OpenAI 兼容接口）调用。"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from .config import ModelConfig
from .prompt import extract_json_object


class LLMError(Exception):
    """模型调用失败。kind: api（网络/HTTP）或 invalid_json（返回格式不对）。"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass
class LLMResult:
    payload: Dict[str, Any]
    usage: Dict[str, int] = field(default_factory=dict)
    elapsed: float = 0.0
    raw_content: str = ""


def request_review(model_cfg: ModelConfig, messages: List[Dict[str, str]]) -> LLMResult:
    api_key = os.environ.get(model_cfg.api_key_env, "").strip()
    if not api_key:
        raise LLMError("api", f"环境变量 {model_cfg.api_key_env} 未设置，无法调用模型")

    url = _endpoint(model_cfg.base_url)
    body = {
        "model": model_cfg.model,
        "messages": messages,
        "temperature": model_cfg.temperature,
        "max_tokens": model_cfg.max_output_tokens,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    proxies = None
    if model_cfg.proxy:
        proxies = {"http": model_cfg.proxy, "https": model_cfg.proxy}

    last_error = "调用模型失败"
    for attempt in range(model_cfg.retries + 1):
        started = time.time()
        try:
            response = requests.post(
                url,
                headers=headers,
                json=body,
                timeout=model_cfg.timeout_seconds,
                proxies=proxies,
            )
        except requests.RequestException as exc:
            last_error = f"网络异常: {exc}"
        else:
            if response.status_code >= 400:
                last_error = f"HTTP {response.status_code}: {response.text[:300]}"
            else:
                return _parse_response(response, time.time() - started)
        if attempt < model_cfg.retries:
            time.sleep(0.5)

    raise LLMError("api", last_error)


def _parse_response(response: requests.Response, elapsed: float) -> LLMResult:
    try:
        data = response.json()
    except ValueError as exc:
        raise LLMError("api", f"响应不是合法 JSON: {exc}") from exc

    try:
        content = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError("api", f"响应结构异常: {exc}") from exc

    try:
        payload = extract_json_object(content)
    except ValueError as exc:
        raise LLMError("invalid_json", f"{exc}；原始输出片段: {content[:300]}") from exc

    if not isinstance(payload.get("findings"), list):
        raise LLMError("invalid_json", "模型输出缺少 findings 数组")

    usage = data.get("usage") or {}
    return LLMResult(
        payload=payload,
        usage={key: int(value) for key, value in usage.items() if isinstance(value, int)},
        elapsed=elapsed,
        raw_content=content,
    )


def _endpoint(base_url: str) -> str:
    url = base_url.rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    return url + "/chat/completions"
