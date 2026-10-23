#!/usr/bin/env python3
"""NodeSeek 美西 VPS 情报监控。

数据源是 NodeSeek 的公开 RSS（主站与 API 都在 Cloudflare 后面，只有 RSS 可用）。
RSS 每次只返回最新 20 条，正常轮询间隔建议 60 秒。

用法见 README.md，或执行 python3 watch.py --help。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path
from typing import Optional

__version__ = "1.0.0"

BASE = Path(os.environ.get("NODESEEK_WATCH_HOME") or Path(__file__).resolve().parent)
STATE_PATH = BASE / "state.json"
HITS_PATH = BASE / "hits.jsonl"
LOCK_PATH = BASE / ".watch.lock"
LOG_PATH = BASE / "logs" / "watch.log"

FEED_URL = os.environ.get("NODESEEK_FEED_URL", "https://rss.nodeseek.com/")
POST_URL = "https://www.nodeseek.com/post-{id}-1"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
CST = timezone(timedelta(hours=8))
LOCK_STALE_SECONDS = 300

DEFAULT_CONFIG = {
    "threshold": 6,
    "instant_threshold": 9,
    "important_threshold": 13,
    "history_limit": 20000,
    "gap_report_threshold": 3,
    "digest_even_if_empty": True,
    "ai": {
        "enabled": False,
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "",
        "api_key": "",
        "user_profile": "",
        "timeout": 60,
    },
    "smtp": {
        "enabled": False,
        "host": "smtp.qq.com",
        "port": 465,
        "security": "ssl",
        "username": "",
        "password": "",
        "from": "",
        "to": [],
        "subject_prefix": "[NodeSeek 情报]",
    },
    "rules": {
        "brands": [
            "vmiss", "dmit", "搬瓦工", "bandwagonhost", "bwh", "斯巴达", "spartanhost",
            "hostdare", "cloudcone", "racknerd", "阿里云", "腾讯云", "cloudflare",
            "akile", "zgo", "buyvm", "ethernetservers", "frantech", "绿云", "greencloud",
            "ccs", "colocrossing", "kdatacenter", "xtom", "vmrack", "justhost",
            "丽萨", "lisahost", "raksmart", "onevps",
        ],
        "routes": [
            "cn2 gia", "cn2gia", "cn2", "gia", "cmin2", "9929", "4837", "cmi",
            "cuii", "10099", "as4809", "三网优化", "优化线路", "回国优化", "直连",
            "premium", "线路机", "精品网", "回程", "dc6", "dc9", "dc3",
            "cn2 gt", "cn2gt", "cmcc", "电信优化", "联通优化", "移动优化",
        ],
        "regions": [
            "美西", "洛杉矶", "los angeles", "圣何塞", "sjc", "san jose",
            "西雅图", "seattle", "波特兰", "portland", "硅谷", "santa clara",
            "弗里蒙特", "fremont", "圣克拉拉", "us west", "uswest", "美国西",
        ],
        # needs：用户画像相关（联通友好、IP 质量、AI 解锁），只加分，不单独构成告警
        "needs": [
            "解锁", "原生", "ip干净", "ip 干净", "新ip段", "新段", "ip质量", "纯净",
            "chatgpt", "claude", "gemini", "openai", "netflix", "disney", "流媒体",
            "联通", "家宽", "tri",
        ],
        "deal_actions": [
            "抢购", "补货", "上架", "开抢", "开售", "放货", "到货", "库存", "限量",
            "上线", "发售", "预售", "优惠", "促销", "活动", "特价", "折扣", "年付",
            "上新", "补货通知", "开卖", "秒杀", "预告", "预约", "即将", "提醒",
            "补货预告", "开售时间", "放售",
        ],
        # strong_actions：只有这些「真的能下手」的动作才够格发即时提醒。
        # 年付、优惠、活动这类词只参与打分，单独出现不会打扰用户。
        "strong_actions": [
            "补货", "抢购", "开抢", "开售", "放货", "秒杀", "补货通知", "补货预告",
            "开售时间", "放售", "限量补货", "开卖", "上新", "上架", "到货",
        ],
        # targets：用户明确想买的机型。有人出二手时立刻通知，不看分数。
        "targets": [
            "lax.eb.wee", "lax eb wee", "laxebwee", "eb.wee", "eb wee",
        ],
        # sell_markers：出现这些才算「有人在出货」。
        "sell_markers": [
            "出", "转", "转让", "出售", "甩", "出手", "明盘", "溢价", "带价",
        ],
        # buy_markers：求购口吻，优先级高于 sell_markers。
        # 像「[收] LAX.EB.WEE 有人出吗」同时含「收」和「出」，必须先判成求购。
        "buy_markers": [
            "收", "求", "蹲", "想入", "想买", "想收", "有没有人出", "有人出吗",
            "带价来", "收购", "问一下", "哪里能买",
        ],
        # exclude：标题命中就整条丢掉（求购、拼车这类不是抢购信息）
        "exclude": [
            "收个", "收一", "收1", "收台", "收", "换", "求购", "求", "拼车",
            "出租", "招租", "代购",
        ],
        # penalty：标题命中扣 5 分但仍然可见（二手、溢价转让之类）
        "penalty": [
            "溢价", "转让", "出个", "出一", "出台", "二手", "原油账单",
        ],
    },
}

def log(msg: str) -> None:
    line = f"{datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 2_000_000:
            LOG_PATH.write_text("", encoding="utf-8")
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


class SingleInstance:
    """避免上一轮还没跑完就开始下一轮（launchd / cron 每分钟触发一次）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: Optional[int] = None

    def __enter__(self) -> "SingleInstance":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            age = time.time() - self.path.stat().st_mtime
            if age <= LOCK_STALE_SECONDS:
                raise SystemExit(0)
            log(f"清理超时锁文件（{int(age)} 秒）")
            self.path.unlink()
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(self.fd, str(os.getpid()).encode())
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def deep_merge(base: dict, override: dict) -> dict:
    """深拷贝后合并，避免调用方修改结果时污染 DEFAULT_CONFIG。"""
    out = {}
    for key, value in (base or {}).items():
        out[key] = deep_merge(value, {}) if isinstance(value, dict) else (
            list(value) if isinstance(value, list) else value
        )
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = deep_merge(value, {}) if isinstance(value, dict) else (
                list(value) if isinstance(value, list) else value
            )
    return out


def load_config(explicit: Optional[str] = None) -> dict:
    path = Path(explicit).expanduser() if explicit else BASE / "config.json"
    raw = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"配置文件 {path} 不是合法 JSON: {exc}")
    cfg = deep_merge(DEFAULT_CONFIG, raw)
    return apply_env_overrides(cfg)


def apply_env_overrides(cfg: dict) -> dict:
    """环境变量优先于 config.json，便于在 GitHub Actions 里用 Secrets 注入凭据。"""
    smtp = cfg["smtp"]
    ai = cfg["ai"]

    def env(*names: str) -> Optional[str]:
        for name in names:
            value = os.environ.get(name)
            if value:
                return value
        return None

    text_map = {
        "host": env("SMTP_HOST"),
        "security": env("SMTP_SECURITY"),
        "username": env("SMTP_EMAIL", "SMTP_USER", "NODESEEK_SMTP_USER"),
        "password": env("SMTP_PASSWORD", "NODESEEK_SMTP_PASS"),
        "from": env("SMTP_FROM", "MAIL_FROM"),
    }
    smtp_from_env = any(text_map.values())
    for key, value in text_map.items():
        if value:
            smtp[key] = value
    port = env("SMTP_PORT")
    if port:
        smtp["port"] = int(port)
        smtp_from_env = True
    to = env("MAIL_TO", "RECEIVER_EMAIL", "NODESEEK_MAIL_TO")
    if to:
        smtp["to"] = [addr.strip() for addr in to.replace(";", ",").split(",") if addr.strip()]
        smtp_from_env = True
    if not smtp.get("from") and smtp.get("username"):
        smtp["from"] = smtp["username"]
    if not smtp.get("enabled") and env("SMTP_ENABLED"):
        smtp["enabled"] = str(env("SMTP_ENABLED")).lower() in ("1", "true", "yes", "on")
    # Actions 里只给了 Secrets，不再要求额外配一个开关
    if smtp_from_env and smtp.get("host") and smtp.get("to") and (smtp.get("from") or smtp.get("username")):
        smtp["enabled"] = True

    provider = env("AI_PROVIDER")
    if provider:
        ai["provider"] = provider
    api_key = env("AI_API_KEY")
    if api_key:
        ai["api_key"] = api_key
    base_url = env("AI_BASE_URL")
    if base_url:
        ai["base_url"] = base_url
    model = env("AI_MODEL")
    if model:
        ai["model"] = model
    if env("AI_ENABLED"):
        ai["enabled"] = str(env("AI_ENABLED")).lower() in ("1", "true", "yes", "on")

    provider_name = (ai.get("provider") or "openai").lower()
    if provider_name in ("github", "github-models", "github_models"):
        ai["provider"] = "github-models"
        if not ai.get("base_url") or ai.get("base_url") == DEFAULT_CONFIG["ai"]["base_url"]:
            ai["base_url"] = "https://models.github.ai/inference"
        ai["api_key"] = ai.get("api_key") or env("GITHUB_TOKEN") or ""
        ai["model"] = ai.get("model") or "openai/gpt-4o-mini"
        ai["enabled"] = True
    elif (api_key or model or base_url) and ai.get("api_key") and ai.get("model"):
        ai["enabled"] = True

    if env("DIGEST_EVEN_IF_EMPTY"):
        cfg["digest_even_if_empty"] = str(env("DIGEST_EVEN_IF_EMPTY")).lower() in ("1", "true", "yes", "on")
    return cfg


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("state.json 损坏，重新初始化")
    return {"seen": [], "max_id": 0, "last_run": None, "last_digest": None, "first_run": True, "gap_events": []}


def save_state(state: dict, limit: int) -> None:
    seen = list(dict.fromkeys(state.get("seen", [])))
    if len(seen) > limit:
        seen = seen[-limit:]
    state["seen"] = seen
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def fetch_feed(retries: int = 3) -> bytes:
    last_error: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                FEED_URL,
                headers={
                    "User-Agent": UA,
                    "Accept": "application/rss+xml, application/xml, text/xml, */*",
                },
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(2 + attempt * 3)
    raise RuntimeError(f"拉取 RSS 失败: {last_error}")


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\[[0-9]{1,2}(?:;[0-9]{1,2})*m")
JUNK_RE = re.compile(r"[\\/`^_~|]{3,}")


def clean_text(text: str) -> str:
    """清掉终端转义码和测速报告的乱码，RSS 摘要里经常混着这些。"""
    text = ANSI_RE.sub(" ", text or "")
    text = JUNK_RE.sub(" ", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_meaningful(text: str) -> bool:
    """摘要太短或基本是符号的就不要放进邮件。"""
    text = (text or "").strip()
    if len(text) < 8:
        return False
    useful = sum(1 for ch in text if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    return useful / len(text) >= 0.5


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return clean_text(html.unescape(text))


def parse_feed(payload: bytes) -> list[dict]:
    root = ET.fromstring(payload)
    items = []
    for node in root.findall("./channel/item"):
        guid = (node.findtext("guid") or "").strip()
        link = (node.findtext("link") or "").strip()
        creator = node.find("{http://purl.org/dc/elements/1.1/}creator")
        post_id = int(guid) if guid.isdigit() else 0
        if not post_id:
            match = re.search(r"post-(\d+)", link)
            post_id = int(match.group(1)) if match else 0
        published = None
        raw_date = node.findtext("pubDate")
        if raw_date:
            try:
                published = parsedate_to_datetime(raw_date)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                published = None
        items.append(
            {
                "id": post_id,
                "guid": guid or str(post_id),
                "title": strip_html(node.findtext("title") or ""),
                "link": link or (POST_URL.format(id=post_id) if post_id else ""),
                "description": strip_html(node.findtext("description") or ""),
                "category": (node.findtext("category") or "").strip(),
                "author": (creator.text or "").strip() if creator is not None else "",
                "published": published.isoformat() if published else None,
            }
        )
    return items


def format_time(value: Optional[str]) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value).astimezone(CST).strftime("%m-%d %H:%M")
    except ValueError:
        return value


def why_line(item: dict) -> str:
    """一句话说明这条为什么被选中。Agent 的判断理由优先。"""
    if item.get("ai_reason"):
        return str(item["ai_reason"])
    parts = [str(m) for m in (item.get("matched") or [])[:8]]
    return " ".join(parts)


def split_tiers(items: list[dict]) -> tuple[list[dict], list[dict]]:
    hot, warm = [], []
    for item in items:
        if item.get("instant") or int(item.get("score") or 0) >= 12:
            hot.append(item)
        else:
            warm.append(item)
    return hot, warm


def readable_summary(item: dict, limit: int = 180) -> str:
    text = clean_text(item.get("description") or "")
    if not is_meaningful(text):
        return ""
    return text[:limit]


def render_rows(items: list[dict]) -> str:
    rows = []
    for item in items:
        meta = " · ".join(
            part
            for part in [
                format_time(item.get("published")),
                item.get("category", ""),
                item.get("author", ""),
                f"评分 {item.get('score')}" if item.get("score") is not None else "",
            ]
            if part
        )
        summary = readable_summary(item)
        summary_html = (
            f'<div style="color:#374151;font-size:13px;margin-top:6px">{html.escape(summary)}</div>' if summary else ""
        )
        rows.append(
            f"""<tr>
  <td style="padding:12px;border-bottom:1px solid #e5e7eb;vertical-align:top">
    <a href="{html.escape(item['link'])}" style="color:#1d4ed8;text-decoration:none;font-weight:600;font-size:14px">{html.escape(item['title'])}</a>
    <div style="color:#b45309;font-size:12px;margin-top:5px">{html.escape(why_line(item))}</div>
    {summary_html}
    <div style="color:#9ca3af;font-size:12px;margin-top:6px">{html.escape(meta)}</div>
  </td>
</tr>"""
        )
    return "".join(rows)


def render_section(title: str, items: list[dict], accent: str) -> str:
    if not items:
        return ""
    return (
        f'<h3 style="font-size:14px;color:{accent};margin:18px 0 8px">{html.escape(title)}（{len(items)} 条）</h3>'
        f'<table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e7eb">{render_rows(items)}</table>'
    )


def render_html(items: list[dict], title: str, intro: str = "") -> str:
    hot, warm = split_tiers(items)
    return f"""<html><body style="margin:0;background:#f9fafb;font-family:-apple-system,'PingFang SC','Helvetica Neue',Arial,sans-serif">
<div style="max-width:680px;margin:0 auto;padding:20px">
  <h2 style="font-size:17px;color:#111827;margin:0 0 6px">{html.escape(title)}</h2>
  <p style="color:#6b7280;font-size:13px;margin:0 0 14px">{html.escape(intro)}</p>
  {render_section("值得立刻看", hot, "#b91c1c")}
  {render_section("可以慢慢看", warm, "#1d4ed8")}
  <p style="color:#9ca3af;font-size:12px;margin-top:14px">来源：NodeSeek 公开 RSS · nodeseek-vps-watch</p>
</div></body></html>"""


def render_text(items: list[dict], title: str, intro: str = "") -> str:
    hot, warm = split_tiers(items)
    lines = [title, intro, ""]
    for section, group in (("值得立刻看", hot), ("可以慢慢看", warm)):
        if not group:
            continue
        lines.append(f"== {section}（{len(group)} 条）==")
        for item in group:
            meta = " · ".join(
                part
                for part in [
                    format_time(item.get("published")),
                    item.get("category", ""),
                    item.get("author", ""),
                    f"评分 {item.get('score')}",
                ]
                if part
            )
            lines.append(f"- {item['title']}")
            lines.append(f"  {why_line(item)}")
            summary = readable_summary(item)
            if summary:
                lines.append(f"  {summary}")
            lines.append(f"  {meta}")
            lines.append(f"  {item['link']}")
            lines.append("")
    return "\n".join(lines)


def send_mail(cfg: dict, subject: str, html_body: str, text_body: str) -> bool:
    smtp = cfg["smtp"]
    if not smtp.get("enabled"):
        log("邮件未启用，跳过发送（把 config.json 的 smtp.enabled 改成 true）")
        return False
    recipients = [addr for addr in smtp.get("to", []) if addr]
    sender = smtp.get("from") or smtp.get("username")
    if not smtp.get("host") or not recipients or not sender:
        log("SMTP 配置不完整（host / from / to），跳过发送")
        return False

    msg = EmailMessage()
    msg["Subject"] = f"{smtp.get('subject_prefix', '')} {subject}".strip()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    security = (smtp.get("security") or "ssl").lower()
    try:
        if security == "ssl":
            with smtplib.SMTP_SSL(
                smtp["host"], int(smtp.get("port", 465)), timeout=30, context=ssl.create_default_context()
            ) as server:
                if smtp.get("username"):
                    server.login(smtp["username"], smtp.get("password", ""))
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp["host"], int(smtp.get("port", 587)), timeout=30) as server:
                server.ehlo()
                if security in ("starttls", "tls"):
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                if smtp.get("username"):
                    server.login(smtp["username"], smtp.get("password", ""))
                server.send_message(msg)
    except Exception as exc:  # noqa: BLE001 - 发信失败不应该中断监控
        log(f"发送邮件失败: {type(exc).__name__}: {exc}")
        return False
    log(f"已发送邮件: {msg['Subject']} -> {', '.join(recipients)}")
    return True


def append_hits(hits: list[dict]) -> None:
    if not hits:
        return
    with HITS_PATH.open("a", encoding="utf-8") as fh:
        for item in hits:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")


def read_hits_since(iso_time: Optional[str]) -> list[dict]:
    if not HITS_PATH.exists():
        return []
    hits = []
    for line in HITS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if iso_time and (item.get("detected_at") or "") <= iso_time:
            continue
        hits.append(item)
    return hits


def recent_gaps(state: dict, since: Optional[str], threshold: int) -> list[int]:
    ids: list[int] = []
    for event in state.get("gap_events", []):
        if since and (event.get("at") or "") <= since:
            continue
        if len(event.get("ids", [])) < threshold:
            continue
        ids.extend(event["ids"])
    return sorted(set(ids))


# 默认画像保持通用（仓库是公开的），个人偏好通过 agent/profile.json、
# 环境变量 AGENT_PROFILE_JSON / AI_USER_PROFILE 注入。
DEFAULT_USER_PROFILE = (
    "中国大陆家宽用户，回程对常用运营商友好；美西优先，亚太也可接受；"
    "看重 IP 质量，希望原生或干净 IP，能解锁 ChatGPT / Claude / Gemini 等主流服务；"
    "带宽和流量不用很大但不能是小口子；不追高溢价的普通二手机型。"
)

AI_SYSTEM_PROMPT_TEMPLATE = (
    "你在帮一位用户盯美西 VPS 情报，产出每天一封的中文分析邮件。\n"
    "用户画像：{profile}\n"
    "输出结构：\n"
    "1) 第一句话直接给结论：今天有没有值得下手的东西，值不值得现在行动。\n"
    "2) 分三段写：「值得立刻看」「可以慢慢看」「可以忽略」，没有内容就写「无」。\n"
    "3) 每条写：厂商、机房或地区、线路、配置或价格（原文有才写）、为什么适合这位用户、风险或不确定点、链接。\n"
    "4) 结尾单独一段「需要你点开确认」：IP 是否原生、AI 服务解锁实测、当前库存和价格，"
    "这些 RSS 摘要里看不到。\n"
    "5) 如果整天只有讨论帖、没有可下单或补货的信息，就直接说明「今天没有可直接购买的机会」。\n"
    "硬性要求：只使用给你的信息，绝不编造价格、库存、优惠码、机房或解锁结论；"
    "摘要里的测速报告、终端乱码可以忽略。中文，不要客套话，全文 600 字以内。"
)


def build_system_prompt(cfg: dict) -> str:
    ai = cfg.get("ai") or {}
    profile = (ai.get("user_profile") or os.environ.get("AI_USER_PROFILE") or "").strip()
    return AI_SYSTEM_PROMPT_TEMPLATE.format(profile=profile or DEFAULT_USER_PROFILE)


def build_ai_prompt(hits: list[dict], window: str) -> str:
    lines = [
        f"统计区间：{window}",
        f"命中条数：{len(hits)}",
        "说明：摘要来自 NodeSeek RSS，可能夹带测速报告和乱码，仅供参考；价格、库存、IP 解锁情况一律以帖子为准。",
        "",
    ]
    for index, hit in enumerate(hits, 1):
        lines.append(f"{index}. {hit.get('title', '')}")
        lines.append(
            "   分类：{} 时间：{} 作者：{} 评分：{} 命中：{}".format(
                hit.get("category", ""),
                format_time(hit.get("published")),
                hit.get("author", ""),
                hit.get("score", ""),
                why_line(hit),
            )
        )
        summary = readable_summary(hit, 300)
        if summary:
            lines.append(f"   摘要：{summary}")
        lines.append(f"   链接：{hit.get('link', '')}")
    return "\n".join(lines)


def http_post_json(url: str, payload: dict, headers: dict, timeout: int) -> tuple[int, str]:
    """POST JSON，返回 (状态码, 响应文本)。HTTP 错误也把响应体带回来，方便定位。"""
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read().decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001 - 读错误体失败不影响回退
            return exc.code, ""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def ai_summary(cfg: dict, hits: list[dict], window: str) -> Optional[str]:
    """调用 OpenAI 兼容接口写日报。

    先试 /chat/completions，端点不存在时回退到 /responses（部分网关只开了 Responses API）。
    任何失败都返回 None，由调用方回退到关键词日报。
    """
    ai = cfg.get("ai") or {}
    if not ai.get("enabled"):
        return None
    api_key = ai.get("api_key") or os.environ.get("NODESEEK_AI_KEY", "")
    model = ai.get("model")
    if not api_key or not model:
        log("AI 摘要已启用但缺少 api_key 或 model，回退到普通日报")
        return None
    base_url = (ai.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    timeout = int(ai.get("timeout", 60))
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": UA,
    }
    system_prompt = build_system_prompt(cfg)
    user_prompt = build_ai_prompt(hits, window)
    chat_payload = {
        "model": model,
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    status, raw = http_post_json(f"{base_url}/chat/completions", chat_payload, headers, timeout)
    content = ""
    if status == 200:
        try:
            content = (json.loads(raw)["choices"][0]["message"]["content"] or "").strip()
        except Exception:  # noqa: BLE001 - 结构不符时统一走回退
            content = ""
    if content:
        return content
    log(f"/chat/completions 不可用（HTTP {status}）：{raw[:300]}")

    responses_payload = {"model": model, "instructions": system_prompt, "input": user_prompt}
    status, raw = http_post_json(f"{base_url}/responses", responses_payload, headers, timeout)
    content = ""
    if status == 200:
        try:
            content = extract_responses_text(json.loads(raw))
        except Exception:  # noqa: BLE001 - 同上
            content = ""
    if not content:
        log(f"/responses 也不可用（HTTP {status}）：{raw[:300]}，回退到关键词日报")
        return None
    return content


def extract_responses_text(data: dict) -> str:
    """从 Responses API 的返回里取正文，兼容 output_text 与 output[].content[].text。"""
    direct = data.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    chunks: list[str] = []
    for item in data.get("output") or []:
        for part in item.get("content") or []:
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text.strip())
    return "\n".join(chunks).strip()


AI_JUDGE_SYSTEM_PROMPT = (
    "你是一个 VPS 抢购情报筛选助手。用户只想在「真的值得行动」时被打扰。\n"
    "用户画像：{profile}\n"
    "\n"
    "你会收到一批 NodeSeek 新帖（标题、分类、作者、摘要）。请逐条判断：这条是否值得立刻通知用户？\n"
    "\n"
    "【值得通知】\n"
    "- 有人出售用户指定机型（标题里出现目标机型，并且是真的在卖）\n"
    "- 官方补货 / 开抢 / 开售 / 放货 / 秒杀，且是美西或优质线路机型\n"
    "- 轰动性的抢购消息：开售几十秒售罄、大批量放货、明显会秒没的新批次预告\n"
    "- 直接影响购买决策的突发情况：热门机型被 DDoS 打到不可用、商家跑路/封号风险\n"
    "\n"
    "【不要通知】\n"
    "- 日常讨论、吐槽、提问、评价、投票\n"
    "- 测评留档、测速报告、IP 质量晒图（除非附带明确的在售/补货信息）\n"
    "- 求购帖（用户在找货，不是有货）\n"
    "- 非美西、非优质线路的机器；VPS 无关的话题\n"
    "- 二手溢价转让，除非正是用户指定的机型\n"
    "- 优惠券、代金券、学生认证之类的羊毛\n"
    "\n"
    "宁可漏报也不要误报。拿不准的一律判为不通知。\n"
    "\n"
    "只输出 JSON，不要任何解释文字、不要 markdown 代码块。格式：\n"
    '{{"headline": "一句话概括本批最重要的情报，没有就写空字符串", '
    '"items": [{{"id": 帖子编号, "important": true, "urgency": "high", "reason": "一句话说明为什么值得通知"}}]}}\n'
    "items 里只放 important 为 true 的条目；可以返回空数组。"
)


def build_judge_prompt(items: list[dict]) -> str:
    lines = [f"共 {len(items)} 条新帖，请逐条判断：", ""]
    for index, item in enumerate(items, 1):
        lines.append(f"帖子编号：{item['id']}")
        lines.append(f"标题：{item.get('title', '')}")
        lines.append(f"分类：{item.get('category', '')} 作者：{item.get('author', '')}")
        summary = readable_summary(item, 300)
        if summary:
            lines.append(f"摘要：{summary}")
        lines.append("")
    return "\n".join(lines)


def parse_judge_json(raw: str) -> Optional[dict]:
    """容忍模型偶尔包上代码块或前后加话。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def ai_judge(cfg: dict, items: list[dict]) -> Optional[dict]:
    """把新帖交给选型 Agent 判断值不值得通知。失败返回 None，调用方保持安静。

    判断逻辑、用户画像、查证工具都在 agent/ 目录里：
    - agent/prompt.md   判断框架与输出格式
    - agent/profile.json 用户画像（预算、目标机型、线路偏好）
    - agent/tools.py    search / fetch 工具
    Python 只负责传帖子和收结论，不做关键词打分。
    """
    ai = cfg.get("ai") or {}
    if not ai.get("enabled"):
        log("AI 未启用，无法判断情报重要性，本轮不发信")
        return None
    api_key = ai.get("api_key") or os.environ.get("NODESEEK_AI_KEY", "")
    model = ai.get("model")
    if not api_key or not model:
        log("AI 缺少 api_key 或 model，本轮不发信")
        return None

    try:
        agent_dir = Path(__file__).resolve().parent / "agent"
        if str(agent_dir) not in sys.path:
            sys.path.insert(0, str(agent_dir))
        import agent as agent_module  # noqa: PLC0415 - 按需导入，本地快速路径不受影响
    except Exception as exc:  # noqa: BLE001 - 缺少 agent 时回退到内置提示词
        log(f"agent 模块不可用（{type(exc).__name__}: {exc}），回退内置判定")
        return _ai_judge_fallback(cfg, items)

    profile = agent_module.load_profile()
    # 允许用配置或环境变量补充画像，不覆盖 agent/profile.json
    extra_profile = (ai.get("user_profile") or os.environ.get("AI_USER_PROFILE") or "").strip()
    if extra_profile:
        profile = {**profile, "extra_notes": extra_profile}

    verdict = agent_module.run_agent(
        agent_module.build_judge_input(items),
        api_key,
        profile,
        base_url=ai.get("base_url") or agent_module.DEFAULT_BASE,
        model=model,
        proxy=os.environ.get("NODESEEK_PROXY", ""),
    )
    if verdict is None:
        log("Agent 判定失败，本轮保持安静")
    return verdict


def _ai_judge_fallback(cfg: dict, items: list[dict]) -> Optional[dict]:
    """agent 目录不可用时的内置判定，保证监控还能工作。"""
    ai = cfg.get("ai") or {}
    api_key = ai.get("api_key") or os.environ.get("NODESEEK_AI_KEY", "")
    profile = (ai.get("user_profile") or os.environ.get("AI_USER_PROFILE") or "").strip()
    system_prompt = AI_JUDGE_SYSTEM_PROMPT.format(profile=profile or DEFAULT_USER_PROFILE)
    payload = {
        "model": ai.get("model"),
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": build_judge_prompt(items)},
        ],
    }
    status, raw = http_post_json(
        f"{(ai.get('base_url') or 'https://api.openai.com/v1').rstrip('/')}/chat/completions",
        payload,
        {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}", "User-Agent": UA},
        int(ai.get("timeout", 60)),
    )
    if status != 200:
        log(f"AI 判定失败（HTTP {status}）：{raw[:300]}")
        return None
    try:
        content = (json.loads(raw)["choices"][0]["message"]["content"] or "").strip()
    except Exception:  # noqa: BLE001
        log(f"AI 判定返回结构异常：{raw[:300]}")
        return None
    return parse_judge_json(content)


def render_ai_report(items: list[dict], headline: str, when: str) -> tuple[str, str]:
    """按 Agent 的结论渲染邮件正文：值不值得买、每月成本、线路、风险。"""
    title = headline or "NodeSeek 重要情报"
    text_lines = [title, f"{when} · 共 {len(items)} 条", ""]
    rows = []
    for item in items:
        reason = item.get("ai_reason") or ""
        verdict = item.get("ai_verdict") or ""
        line = item.get("ai_line") or ""
        price = item.get("ai_price")
        risks = item.get("ai_risks") or []
        summary = readable_summary(item, 200)

        # 结论行：值得买 / 值得看 / 观察 + 每月成本
        badges = [b for b in [verdict, line] if b]
        if isinstance(price, (int, float)) and price > 0:
            badges.append(f"约 {price:.0f} 元/月")
        header = f"- {item['title']}"
        if badges:
            header += f"\n  结论：{' · '.join(badges)}"
        text_lines.append(header)
        if reason:
            text_lines.append(f"  为什么值得看：{reason}")
        if summary:
            text_lines.append(f"  摘要：{summary}")
        for risk in risks:
            text_lines.append(f"  ⚠ 风险：{risk}")
        text_lines.append(f"  {item['link']}")
        text_lines.append("")

        badge_html = "".join(
            f'<span style="display:inline-block;background:#eef2ff;color:#3730a3;'
            f'border-radius:4px;padding:2px 6px;font-size:12px;margin:0 4px 4px 0">{html.escape(b)}</span>'
            for b in badges
        )
        risk_html = ""
        if risks:
            risk_html = (
                '<ul style="margin:6px 0 0;padding-left:18px;color:#b45309;font-size:13px">'
                + "".join(f"<li>{html.escape(r)}</li>" for r in risks)
                + "</ul>"
            )
        rows.append(
            f"""<tr><td style="padding:12px;border-bottom:1px solid #e5e7eb">
  <a href="{html.escape(item['link'])}" style="color:#1d4ed8;font-weight:600;text-decoration:none;font-size:14px">{html.escape(item['title'])}</a>
  <div style="margin-top:6px">{badge_html}</div>
  <div style="color:#b45309;font-size:13px;margin-top:6px">{html.escape(reason)}</div>
  {f'<div style="color:#374151;font-size:13px;margin-top:6px">{html.escape(summary)}</div>' if summary else ''}
  {risk_html}
  <div style="color:#9ca3af;font-size:12px;margin-top:6px">{html.escape(format_time(item.get('published')))} · {html.escape(item.get('category',''))} · {html.escape(item.get('author',''))}</div>
</td></tr>"""
        )
    html_body = (
        "<html><body style=\"margin:0;background:#f9fafb;font-family:-apple-system,'PingFang SC',Arial,sans-serif\">"
        "<div style=\"max-width:680px;margin:0 auto;padding:20px\">"
        f"<h2 style=\"font-size:17px;color:#111827;margin:0 0 6px\">{html.escape(title)}</h2>"
        f"<p style=\"color:#6b7280;font-size:13px;margin:0 0 14px\">{html.escape(when)} · 共 {len(items)} 条</p>"
        f"<table style=\"width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e7eb\">{''.join(rows)}</table>"
        "<p style=\"color:#9ca3af;font-size:12px;margin-top:14px\">AI 判定 · 来源：NodeSeek 公开 RSS</p>"
        "</div></body></html>"
    )
    return "\n".join(text_lines), html_body


def command_run(cfg: dict, dry_run: bool = False) -> int:
    state = load_state()
    seen = set(state.get("seen", []))
    first_run = bool(state.get("first_run", not seen))
    max_id = int(state.get("max_id") or 0)

    items = parse_feed(fetch_feed())
    if not items:
        log("RSS 未返回任何条目")
        return 1

    new_items = [it for it in items if it["guid"] not in seen and str(it["id"]) not in seen]
    new_items.sort(key=lambda it: it["id"])

    current_max = max((it["id"] for it in items), default=max_id)
    gaps: list[int] = []
    if max_id and current_max > max_id:
        visible = {it["id"] for it in items}
        gaps = [pid for pid in range(max_id + 1, current_max) if pid not in visible]

    now_iso = datetime.now(timezone.utc).isoformat()
    state["seen"] = list(dict.fromkeys(list(seen) + [it["guid"] for it in items] + [str(it["id"]) for it in items]))
    state["max_id"] = max(max_id, current_max)
    state["last_run"] = now_iso
    state["first_run"] = False
    if len(gaps) >= int(cfg.get("gap_report_threshold", 3)):
        state.setdefault("gap_events", []).append({"at": now_iso, "ids": gaps})
        state["gap_events"] = state["gap_events"][-20:]

    if first_run:
        if not dry_run:
            save_state(state, cfg["history_limit"])
        log(f"首次运行，已建立基线（{len(items)} 条），不发送历史告警")
        return 0

    log(f"本轮新增 {len(new_items)} 条，交给 AI 判断重要性")
    if gaps:
        log(f"检测到可能的遗漏区间（共 {len(gaps)} 条）: {', '.join(str(pid) for pid in gaps[:20])}")

    if not new_items:
        if not dry_run:
            save_state(state, cfg["history_limit"])
        return 0

    if dry_run:
        for item in new_items:
            print(f"[{item['id']}] {item['title']} -> {item['link']}")
        return 0

    # 判断权完全在 AI：把本批新帖整批发过去，只有它说重要才通知用户。
    verdict = ai_judge(cfg, new_items)
    if verdict is None:
        save_state(state, cfg["history_limit"])
        log("AI 判定不可用，本轮保持安静")
        return 0

    by_id = {str(item["id"]): item for item in new_items}
    important: list[dict] = []
    for entry in verdict.get("items") or []:
        if not isinstance(entry, dict) or not entry.get("important"):
            continue
        item = by_id.get(str(entry.get("id")))
        if not item:
            continue
        important.append(
            {
                **item,
                "ai_reason": str(entry.get("reason") or ""),
                "ai_urgency": str(entry.get("urgency") or ""),
                "ai_verdict": str(entry.get("verdict") or ""),
                "ai_line": str(entry.get("line") or ""),
                "ai_price": entry.get("price_month_cny"),
                "ai_risks": [str(r) for r in (entry.get("risks") or []) if r][:3],
            }
        )

    append_hits([{**item, "detected_at": now_iso, "score": 0, "matched": ["ai"], "important": True} for item in important])
    save_state(state, cfg["history_limit"])

    if not important:
        log(f"AI 判定：本批 {len(new_items)} 条都不值得通知，保持安静")
        return 0

    important.sort(key=lambda it: (it.get("ai_urgency") != "high", -int(it["id"])))
    headline = str(verdict.get("headline") or "").strip()
    subject = headline[:60] if headline else f"NodeSeek 重要情报：{important[0]['title'][:40]}"
    text_body, html_body = render_ai_report(important, headline, format_time(now_iso))
    log(f"AI 判定 {len(important)} 条重要情报，发送邮件")
    send_mail(cfg, subject, html_body, text_body)
    return 0


def command_digest(
    cfg: dict,
    print_only: bool = False,
    as_json: bool = False,
    hours: Optional[int] = None,
    state: Optional[dict] = None,
    use_ai: bool = False,
) -> int:
    state = state if state is not None else load_state()
    since = state.get("last_digest")
    if hours:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    hits = read_hits_since(since)
    hits.sort(key=lambda hit: (hit.get("score", 0), hit.get("detected_at", "")), reverse=True)
    gaps = recent_gaps(state, since, int(cfg.get("gap_report_threshold", 3)))
    gap_links = [POST_URL.format(id=pid) for pid in gaps]

    if as_json:
        print(json.dumps({"since": since, "hits": hits, "missed": gap_links}, ensure_ascii=False, indent=2))
        return 0

    window = format_time(since) if since else "最早记录"
    if print_only:
        if not hits and not gap_links:
            print("无新的命中记录")
            return 0
        if hits:
            print(render_text(hits, f"统计区间起于 {window}", f"共 {len(hits)} 条命中").strip())
        if gap_links:
            print("\n可能漏掉的帖子：")
            for link in gap_links[:20]:
                print(f"  {link}")
        return 0

    if not hits and not gap_links and not cfg.get("digest_even_if_empty", True):
        print("无新的命中记录")
        return 0

    # 日报只谈重要情报：日常讨论、测评留档、被 D 之类不进邮件。
    # 历史命中没有 important 字段，按当时的标准视为重要，保持向后兼容。
    important = [hit for hit in hits if hit.get("important", True)]
    ignored = [hit for hit in hits if not hit.get("important", True)]
    if hits and not important and not cfg.get("digest_even_if_empty", True):
        print(f"本轮 {len(hits)} 条命中都不重要，跳过发信")
        return 0

    if not hits and not gap_links:
        subject = "NodeSeek 美西 VPS 日报（今日无命中）"
        intro = f"统计区间起于 {window} · 期间没有出现美西优质线路相关的抢购或补货信息"
        text_body = f"{subject}\n\n{intro}\n\n监控仍在正常运行。"
        html_body = (
            "<html><body style=\"font-family:-apple-system,'PingFang SC',Arial,sans-serif\">"
            f"<h2 style=\"font-size:17px;color:#111827\">{html.escape(subject)}</h2>"
            f"<p style=\"color:#6b7280;font-size:13px\">{html.escape(intro)}</p>"
            "<p style=\"color:#9ca3af;font-size:12px\">来源：NodeSeek 公开 RSS · nodeseek-vps-watch</p>"
            "</body></html>"
        )
    else:
        subject = f"NodeSeek 美西 VPS 情报汇总（{len(important)} 条）"
        intro = f"统计区间起于 {window} · 共 {len(important)} 条值得关注"
        if ignored:
            intro += f"，另有 {len(ignored)} 条闲聊已略过"
        if gap_links:
            intro += f" · 另有 {len(gap_links)} 条帖子可能落在 RSS 窗口之外"
        summary = ai_summary(cfg, important, window) if use_ai else None
        if summary is None and use_ai and (cfg.get("ai") or {}).get("enabled") and important:
            intro += " · ⚠️ AI 摘要本轮不可用，下面这封是关键词版（详见日志）"
        if summary:
            subject = f"NodeSeek 美西 VPS 日报（{len(important)} 条）"
            raw_list = render_text(important, "", "").strip()
            text_body = f"{summary}\n\n---- 原始命中 ----\n{raw_list}"
            if gap_links:
                text_body += "\n\n可能漏掉的帖子：\n" + "\n".join(gap_links)
            hot, warm = split_tiers(important)
            sections = render_section("值得立刻看", hot, "#b91c1c") + render_section("可以慢慢看", warm, "#1d4ed8")
            gap_block = ""
            if gap_links:
                links_html = " ".join(f'<a href="{html.escape(link)}">{html.escape(link)}</a>' for link in gap_links[:20])
                gap_block = f'<p style="color:#6b7280;font-size:13px;margin-top:14px">可能漏掉的帖子：{links_html}</p>'
            html_body = (
                "<html><body style=\"margin:0;background:#f9fafb;font-family:-apple-system,'PingFang SC',Arial,sans-serif;color:#111827\">"
                "<div style=\"max-width:680px;margin:0 auto;padding:20px\">"
                f"<h2 style=\"font-size:17px;margin:0 0 6px\">{html.escape(subject)}</h2>"
                f"<p style=\"color:#6b7280;font-size:13px;margin:0 0 14px\">{html.escape(intro)}</p>"
                f"<div style=\"font-size:14px;line-height:1.7;white-space:pre-wrap\">{html.escape(summary)}</div>"
                f"{sections}"
                f"{gap_block}"
                "<p style=\"color:#9ca3af;font-size:12px;margin-top:14px\">来源：NodeSeek 公开 RSS · nodeseek-vps-watch</p>"
                "</div></body></html>"
            )
        else:
            text_body = render_text(important, subject, intro)
            if gap_links:
                text_body += "\n\n可能漏掉的帖子：\n" + "\n".join(gap_links)
            html_body = render_html(important, subject, intro)

    ok = send_mail(cfg, subject, html_body, text_body)
    if ok:
        state["last_digest"] = datetime.now(timezone.utc).isoformat()
        save_state(state, cfg["history_limit"])
    return 0


def command_send(cfg: dict, subject: str, body_file: Optional[str], text: Optional[str], html_file: Optional[str]) -> int:
    text_body = ""
    if body_file:
        text_body = Path(body_file).read_text(encoding="utf-8")
    elif text:
        text_body = text
    html_body = Path(html_file).read_text(encoding="utf-8") if html_file else ""
    if not html_body:
        html_body = (
            "<html><body style=\"font-family:-apple-system,'PingFang SC',Arial,sans-serif;white-space:pre-wrap\">"
            f"{html.escape(text_body)}</body></html>"
        )
    return 0 if send_mail(cfg, subject, html_body, text_body) else 1


def command_status(cfg: dict) -> int:
    state = load_state()
    hits = read_hits_since(None)
    smtp = cfg["smtp"]
    print(f"运行目录: {BASE}")
    print(f"数据源: {FEED_URL}")
    print(f"邮件启用: {smtp.get('enabled')}  收件人: {', '.join(smtp.get('to') or ['(未配置)'])}")
    print(f"命中阈值: {cfg['threshold']}  即时阈值: {cfg['instant_threshold']}")
    print(f"已见条目: {len(state.get('seen', []))}  最大帖子 ID: {state.get('max_id')}")
    print(f"上次轮询: {state.get('last_run')}  上次汇总: {state.get('last_digest')}")
    print(f"累计命中: {len(hits)}")
    for hit in hits[-5:]:
        print(f"  [{hit.get('score')}] {hit.get('title')} {hit.get('link')}")
    return 0


def command_test_email(cfg: dict) -> int:
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    return command_send(cfg, "连通性测试", None, f"NodeSeek 监控邮件通道测试成功。\n时间：{now}\n运行目录：{BASE}", None)


def command_mark_digested(cfg: dict) -> int:
    """外部（如 AI 助手）自己写完并发出日报后，用它推进汇总水位线。"""
    state = load_state()
    state["last_digest"] = datetime.now(timezone.utc).isoformat()
    save_state(state, cfg["history_limit"])
    print(f"已标记汇总时间: {state['last_digest']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NodeSeek 美西 VPS 情报监控")
    parser.add_argument("--version", action="version", version=f"nodeseek-vps-watch {__version__}")
    parser.add_argument("--config", help="配置文件路径，默认 <运行目录>/config.json")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="轮询 RSS 并发送即时告警")
    run_parser.add_argument("--dry-run", action="store_true", help="不写状态、不发邮件")

    digest_parser = sub.add_parser("digest", help="汇总上次汇总之后的命中")
    digest_parser.add_argument("--print", dest="print_only", action="store_true", help="只打印，不发邮件")
    digest_parser.add_argument("--json", dest="as_json", action="store_true", help="输出 JSON")
    digest_parser.add_argument("--hours", type=int, help="覆盖统计窗口（小时）")
    digest_parser.add_argument("--ai", action="store_true", help="命中时调用配置的 AI 接口生成摘要")

    send_parser = sub.add_parser("send", help="发送自定义邮件")
    send_parser.add_argument("--subject", required=True)
    send_parser.add_argument("--body-file")
    send_parser.add_argument("--text")
    send_parser.add_argument("--html-file")

    sub.add_parser("status", help="查看运行状态")
    sub.add_parser("test-email", help="发送测试邮件")
    sub.add_parser("mark-digested", help="把当前时间记为已汇总（外部发信后调用）")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)

    if args.command == "run":
        with SingleInstance(LOCK_PATH):
            return command_run(cfg, dry_run=args.dry_run)
    if args.command == "digest":
        return command_digest(
            cfg, print_only=args.print_only, as_json=args.as_json, hours=args.hours, use_ai=args.ai
        )
    if args.command == "send":
        return command_send(cfg, args.subject, args.body_file, args.text, args.html_file)
    if args.command == "status":
        return command_status(cfg)
    if args.command == "test-email":
        return command_test_email(cfg)
    if args.command == "mark-digested":
        return command_mark_digested(cfg)
    return 2


if __name__ == "__main__":
    sys.exit(main())
