#!/usr/bin/env python3
"""VPS 选型 Agent：判断一批帖子（或单台机器）值不值得让用户出手。

设计原则：
- Python 只负责抓取、传递、投递；值不值得买的判断全部交给模型。
- 模型可以调用 search / fetch 自己查证（第三方评测、商家口碑、价格）。
- 任何失败都返回 None，调用方保持安静，不误报。

用法：
    python3 agent/agent.py judge --input posts.jsonl        # 判断一批帖子
    python3 agent/agent.py evaluate --query "DMIT LAX.EB.WEE"  # 评估一台机器
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools import TOOL_SCHEMAS, ToolError, WebTools, execute_tool  # noqa: E402

ROOT = Path(__file__).resolve().parent
PROMPT_PATH = ROOT / "prompt.md"
PROFILE_PATH = ROOT / "profile.json"
PROFILE_EXAMPLE = ROOT / "profile.example.json"

DEFAULT_BASE = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEFAULT_MODEL = os.environ.get("AGENT_MODEL", "deepseek-flash")
DEFAULT_PROXY = os.environ.get("NODESEEK_PROXY", "")
MAX_TOOL_ROUNDS = int(os.environ.get("AGENT_MAX_TOOL_ROUNDS", "4"))
TIMEOUT = int(os.environ.get("AGENT_TIMEOUT", "180"))


def log(msg: str) -> None:
    print(f"[agent] {msg}", flush=True)


def load_system_prompt(profile: Optional[dict] = None) -> str:
    """把 prompt.md 和画像合并成 system prompt。"""
    text = PROMPT_PATH.read_text(encoding="utf-8")
    if profile:
        text += "\n\n## 本用户的结构化画像\n\n```json\n"
        text += json.dumps(profile, ensure_ascii=False, indent=2)
        text += "\n```\n"
    return text


def load_profile(path: Optional[str] = None) -> dict:
    """加载用户画像，优先级从高到低：

    1. 显式路径（命令行 --profile / 测试）
    2. 环境变量 AGENT_PROFILE_JSON：整份 JSON 放 Secret，云端用这个，
       真实偏好不进公开仓库
    3. agent/profile.json：本地开发用，已 gitignore
    4. agent/profile.example.json：脱敏模板，仓库里唯一提交的画像
    """
    if path:
        p = Path(path)
        if p.exists():
            return _read_profile(p)

    raw = (os.environ.get("AGENT_PROFILE_JSON") or "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            log(f"AGENT_PROFILE_JSON 不是合法 JSON，忽略：{exc}")
        else:
            if isinstance(data, dict):
                return data
            log("AGENT_PROFILE_JSON 不是 JSON 对象，忽略")

    for candidate in (PROFILE_PATH, PROFILE_EXAMPLE):
        if candidate.exists():
            return _read_profile(candidate)
    return {}


def _read_profile(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log(f"画像文件 {p.name} 不是合法 JSON，忽略：{exc}")
        return {}


def _chat(payload: dict, api_key: str, base_url: str, proxy: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
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
            return exc.code, {"raw": body[:400]}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, {"error": {"message": f"{type(exc).__name__}: {exc}"}}


def parse_json_block(raw: str) -> Optional[dict]:
    """容忍模型包代码块或加前后说明。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def run_agent(
    user_content: str,
    api_key: str,
    profile: dict,
    base_url: str = DEFAULT_BASE,
    model: str = DEFAULT_MODEL,
    proxy: str = DEFAULT_PROXY,
    max_tool_rounds: int = MAX_TOOL_ROUNDS,
) -> Optional[dict]:
    """跑一次 agent：允许模型调工具查证，最后要求输出 JSON。"""
    tools = WebTools(api_key=api_key, base_url=base_url, proxy=proxy)
    messages = [
        {"role": "system", "content": load_system_prompt(profile)},
        {"role": "user", "content": user_content},
    ]

    for round_index in range(max_tool_rounds + 1):
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "tools": TOOL_SCHEMAS,
            "tool_choice": "auto" if round_index < max_tool_rounds else "none",
        }
        status, data = _chat(payload, api_key, base_url, proxy)
        if status != 200:
            msg = (data.get("error") or {}).get("message") or str(data)[:200]
            log(f"模型调用失败（HTTP {status}）：{msg}")
            return None

        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            log(f"返回结构异常：{str(data)[:200]}")
            return None

        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            messages.append(message)
            for call in tool_calls:
                fn = call.get("function") or {}
                name = fn.get("name") or ""
                result = execute_tool(tools, name, fn.get("arguments") or "{}")
                log(f"工具调用 {name}: {str(result)[:120]}")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or name,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
            continue

        content = (message.get("content") or "").strip()
        verdict = parse_json_block(content)
        if verdict is None:
            log(f"最终输出不是 JSON：{content[:200]}")
            return None
        verdict["_tool_calls"] = tools.calls
        verdict["_tool_trace"] = tools.trace
        return verdict

    log("工具调用轮次用尽，仍未得到结论")
    return None


def build_judge_input(items: list[dict]) -> str:
    """把一批帖子整理成模型易读的输入。"""
    lines = [
        f"下面是最新的 {len(items)} 条 NodeSeek 帖子。逐条判断哪些值得立刻通知用户，",
        "按 prompt 里的 JSON 格式输出。只把 truly important 的放进 items。",
        "",
    ]
    for item in items:
        lines.append(f"帖子编号：{item.get('id')}")
        lines.append(f"标题：{item.get('title', '')}")
        lines.append(f"分类：{item.get('category', '')}  作者：{item.get('author', '')}")
        body = (item.get("description") or "").strip()
        if body:
            lines.append(f"摘要：{body[:400]}")
        lines.append("")
    return "\n".join(lines)


def command_judge(args: argparse.Namespace) -> int:
    api_key = args.api_key or os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        log("缺少 API key（--api-key 或 DEEPSEEK_API_KEY）")
        return 2

    items = []
    path = Path(args.input)
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not items:
        log("输入文件里没有可用帖子")
        return 1

    profile = load_profile(args.profile)
    verdict = run_agent(
        build_judge_input(items),
        api_key,
        profile,
        base_url=args.base_url,
        model=args.model,
        proxy=args.proxy,
    )
    if verdict is None:
        log("判定失败，按「没有重要情报」处理")
        return 1
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return 0


def command_evaluate(args: argparse.Namespace) -> int:
    api_key = args.api_key or os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        log("缺少 API key（--api-key 或 DEEPSEEK_API_KEY）")
        return 2

    profile = load_profile(args.profile)
    question = args.query or (
        "评估当前最值得买的性价比线路机，给出每月真实成本、线路与解锁判断、风险和建议动作。"
    )
    verdict = run_agent(
        question
        + "\n\n你可以用 search / fetch 查证最新价格与口碑。最后按 prompt 的结构输出 JSON。",
        api_key,
        profile,
        base_url=args.base_url,
        model=args.model,
        proxy=args.proxy,
    )
    if verdict is None:
        log("评估失败")
        return 1
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="VPS 选型 Agent")
    parser.add_argument("--api-key", help="DeepSeek API key，默认读 DEEPSEEK_API_KEY")
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--proxy", default=DEFAULT_PROXY, help="可选 HTTP 代理")
    parser.add_argument("--profile", help="画像 JSON 路径，默认 agent/profile.json")
    sub = parser.add_subparsers(dest="command", required=True)

    judge = sub.add_parser("judge", help="判断一批帖子值不值得通知")
    judge.add_argument("--input", required=True, help="每行一个帖子的 JSONL 文件")
    judge.set_defaults(func=command_judge)

    evaluate = sub.add_parser("evaluate", help="评估当前最值得买的机器")
    evaluate.add_argument("--query", help="自定义问题，默认给推荐")
    evaluate.set_defaults(func=command_evaluate)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
