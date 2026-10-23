#!/usr/bin/env python3
"""Agent 可用的外部工具：DeepSeek Search / Fetch。

官方文档：https://api-docs.deepseek.com/guides/search
- Search  POST /v1/tools/search  {query, count, need_content}
- Fetch   POST /v1/tools/fetch   {url}
计费：每 1000 次成功请求 0.75 美元；失败和空结果不计费。

NodeSeek 本身既搜不到也抓不到（搜索引擎没索引、Fetch 被 Cloudflare 挡 404），
所以这两个工具用于查第三方评测、商家口碑、价格信息。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional

DEFAULT_BASE = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEFAULT_PROXY = os.environ.get("NODESEEK_PROXY", "")
TIMEOUT = int(os.environ.get("AGENT_TOOL_TIMEOUT", "90"))


class ToolError(RuntimeError):
    """工具调用失败，调用方按「查不到」处理，不要中断整个流程。"""


def normalize_base(url: str) -> str:
    """Search / Fetch 挂在 /v1/tools/* 下，而聊天接口的 base 常写成 .../v1。

    两种写法都要能用：https://api.deepseek.com 和 https://api.deepseek.com/v1
    """
    base = (url or DEFAULT_BASE).rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


def _post(path: str, payload: dict, api_key: str, base_url: str, proxy: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{normalize_base(base_url)}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "nodeseek-vps-watch-agent/1.0",
        },
        method="POST",
    )
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"error": {"message": body[:300]}}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, {"error": {"message": f"{type(exc).__name__}: {exc}"}}


class WebTools:
    """搜索与抓取，带调用计数，方便限制 agent 的查证预算。"""

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE, proxy: str = DEFAULT_PROXY):
        self.api_key = api_key
        self.base_url = base_url
        self.proxy = proxy
        self.calls = 0
        self.trace: list[dict] = []

    def search(self, query: str, count: int = 10, need_content: bool = False) -> list[dict]:
        self.calls += 1
        status, data = _post(
            "/v1/tools/search",
            {"query": query, "count": max(1, min(int(count or 10), 50)), "need_content": need_content},
            self.api_key,
            self.base_url,
            self.proxy,
        )
        if status != 200 or "error" in data:
            msg = (data.get("error") or {}).get("message") or str(data)[:200]
            self.trace.append({"tool": "search", "query": query, "error": msg})
            raise ToolError(f"search 失败（HTTP {status}）：{msg}")
        results = data.get("results", [])
        self.trace.append({"tool": "search", "query": query, "results": len(results)})
        return results

    def fetch(self, url: str) -> dict:
        self.calls += 1
        status, data = _post(
            "/v1/tools/fetch", {"url": url}, self.api_key, self.base_url, self.proxy
        )
        if status != 200 or "error" in data:
            msg = (data.get("error") or {}).get("message") or str(data)[:200]
            self.trace.append({"tool": "fetch", "url": url, "error": msg})
            raise ToolError(f"fetch 失败（HTTP {status}）：{msg}")
        self.trace.append({"tool": "fetch", "url": url, "length": len(data.get("content") or "")})
        return data


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": (
                "搜索互联网，用于查证 VPS 的第三方评测、商家口碑、当前价格、线路质量。"
                "注意搜不到 NodeSeek 站内内容。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索词，支持 site:/lang:/after: 等运算符"},
                    "count": {"type": "integer", "description": "返回条数 1-50，默认 10"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch",
            "description": "抓取指定网页并返回 Markdown 正文，用于精读某篇评测或商家页面。",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "完整 http/https URL"}},
                "required": ["url"],
            },
        },
    },
]


def execute_tool(tools: WebTools, name: str, arguments: str) -> dict:
    """执行模型请求的工具调用，返回可 JSON 序列化的结果。"""
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {"error": "参数不是合法 JSON"}
    try:
        if name == "search":
            results = tools.search(str(args.get("query", "")), int(args.get("count", 10)))
            return {
                "results": [
                    {
                        "title": r.get("title"),
                        "url": r.get("url"),
                        "snippet": r.get("snippet"),
                        "summary": r.get("summary"),
                        "date": r.get("date"),
                        "score": r.get("rerank_score"),
                    }
                    for r in results[:12]
                ]
            }
        if name == "fetch":
            page = tools.fetch(str(args.get("url", "")))
            content = (page.get("content") or "")[:6000]
            return {"title": page.get("title"), "content": content}
        return {"error": f"未知工具 {name}"}
    except ToolError as exc:
        return {"error": str(exc)}
