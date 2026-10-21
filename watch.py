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
    "history_limit": 20000,
    "gap_report_threshold": 3,
    "digest_even_if_empty": True,
    "ai": {
        "enabled": False,
        "base_url": "https://api.openai.com/v1",
        "model": "",
        "api_key": "",
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

# 标题命中权重高于正文摘要：同一组关键词只取最高权重，避免同义词堆叠虚高。
GROUP_WEIGHTS = {
    "brands": (6, 2),
    "deal_actions": (4, 1),
    "routes": (3, 1),
    "regions": (2, 1),
    "needs": (2, 1),
}

# 只有这几组命中才算「跟美西线路机有关」，needs 只用来加权
TOPICAL_GROUPS = ("brands", "routes", "regions")


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
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
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
    env_pass = os.environ.get("NODESEEK_SMTP_PASS")
    if env_pass:
        cfg["smtp"]["password"] = env_pass
    env_to = os.environ.get("NODESEEK_MAIL_TO")
    if env_to:
        cfg["smtp"]["to"] = [addr.strip() for addr in env_to.split(",") if addr.strip()]
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


def score_item(item: dict, rules: dict) -> tuple[int, list[str], bool]:
    """返回 (分数, 命中关键词, 是否命中品牌/线路/地区)。"""
    title = item["title"].lower()
    body = (item.get("description") or "").lower()
    score = 0
    matched: list[str] = []
    topical = False

    for group in GROUP_WEIGHTS:
        title_weight, body_weight = GROUP_WEIGHTS[group]
        best = 0
        for keyword in rules.get(group, []):
            kw = keyword.lower()
            if kw in title:
                best = max(best, title_weight)
                matched.append(keyword)
            elif kw in body:
                best = max(best, body_weight)
                matched.append(f"{keyword}(正文)")
            if best and group in TOPICAL_GROUPS:
                topical = True
        score += best

    for keyword in rules.get("penalty", []):
        if keyword.lower() in title:
            score -= 5
            matched.append(f"-{keyword}")

    if item.get("category") == "info":
        score += 1
    if "优惠" in item["title"] or "促销" in item["title"]:
        score += 1
    return score, matched, topical


def is_instant(item: dict, score: int, cfg: dict) -> bool:
    """什么值得立刻打扰：线路 + 动作，或者品牌 + 抢购/补货/预告类动作。

    光有品牌没有动作（例如「vmiss 又被 D 了」）不进即时通道，只进日报。
    """
    if score < cfg["instant_threshold"]:
        return False
    title = item["title"].lower()
    rules = cfg["rules"]
    if any(kw.lower() in title for kw in rules.get("penalty", [])):
        return False
    route = any(kw.lower() in title for kw in rules["routes"])
    brand = any(kw.lower() in title for kw in rules["brands"])
    action = any(kw.lower() in title for kw in rules["deal_actions"])
    if route and (brand or action):
        return True
    return brand and action


def is_vetoed(item: dict, rules: dict) -> bool:
    """求购、拼车、出租这类帖子直接丢弃。"""
    title = item["title"].lower()
    return any(kw.lower() in title for kw in rules.get("exclude", []))


def format_time(value: Optional[str]) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value).astimezone(CST).strftime("%m-%d %H:%M")
    except ValueError:
        return value


GROUP_LABELS = {
    "brands": "厂商",
    "routes": "线路",
    "regions": "地区",
    "deal_actions": "动作",
    "needs": "你的需求",
    "penalty": "注意",
}


def group_hits(item: dict, rules: dict) -> dict:
    """按组收集命中关键词，邮件里就能写成「线路 CN2 GIA · 地区 洛杉矶」。"""
    title = (item.get("title") or "").lower()
    body = (item.get("description") or "").lower()
    out = {}
    for group in list(GROUP_WEIGHTS) + ["penalty"]:
        words = []
        for keyword in rules.get(group, []):
            kw = keyword.lower()
            if kw in title:
                words.append(keyword)
            elif kw in body:
                words.append(f"{keyword}(摘要)")
        if words:
            out[group] = words[:5]
    return out


def why_line(item: dict) -> str:
    groups = item.get("groups") or {}
    if groups:
        parts = []
        for group, words in groups.items():
            label = GROUP_LABELS.get(group, group)
            if group == "penalty":
                parts.append("注意 " + "/".join(w.lstrip("-") for w in words))
            else:
                parts.append(f"{label} " + " / ".join(words))
        return " · ".join(parts)
    parts = list(item.get("matched", [])[:8])
    negatives = item.get("negatives") or []
    if negatives:
        parts.append("疑似非新品：" + "/".join(n.lstrip("-") for n in negatives))
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


AI_SYSTEM_PROMPT = (
    "你在帮一位中国大陆用户盯美西 VPS 情报，产出每天一封的中文分析邮件。\n"
    "用户画像：中国大陆家宽；只要美西，洛杉矶优先；回程要对联通友好"
    "（9929 / AS9929、CMIN2、CN2 GIA、DC6、DC9 都算好，4837 次之）；"
    "非常看重 IP 质量，希望是原生或干净 IP，能解锁 ChatGPT、Claude、Gemini、Netflix 等主流服务；"
    "带宽和流量不用很大但不能是小口子，参考定位是 VMISS LA TRI Basic 这类 1C1G / 500GB 套餐；"
    "不追高溢价的二手转让。\n"
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
    system_prompt = AI_SYSTEM_PROMPT
    user_prompt = build_ai_prompt(hits, window)
    chat_payload = {
        "model": model,
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    try:
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(chat_payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = (data["choices"][0]["message"]["content"] or "").strip()
        if content:
            return content
        log("AI 返回空内容，回退到普通日报")
        return None
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "ignore")[:200]
        except Exception:  # noqa: BLE001 - 读错误体失败不影响回退
            detail = ""
        log(f"/chat/completions 返回 {exc.code}，尝试 /responses：{detail}")
    except Exception as exc:  # noqa: BLE001 - 摘要失败不能影响日报投递
        log(f"AI 摘要失败: {type(exc).__name__}: {exc}")
        return None

    responses_payload = {"model": model, "instructions": system_prompt, "input": user_prompt}
    try:
        request = urllib.request.Request(
            f"{base_url}/responses",
            data=json.dumps(responses_payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = extract_responses_text(data)
    except Exception as exc:  # noqa: BLE001 - 同上
        log(f"AI 摘要失败: {type(exc).__name__}: {exc}")
        return None
    if not content:
        log("AI 返回空内容，回退到普通日报")
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
    hits = []
    if not first_run:
        for item in new_items:
            if is_vetoed(item, cfg["rules"]):
                continue
            score, matched, topical = score_item(item, cfg["rules"])
            if score >= cfg["threshold"] and topical:
                hits.append(
                    {
                        **item,
                        "score": score,
                        "matched": [m for m in matched if not m.startswith("-")],
                        "negatives": [m for m in matched if m.startswith("-")],
                        "groups": group_hits(item, cfg["rules"]),
                        "detected_at": now_iso,
                        "instant": is_instant(item, score, cfg),
                    }
                )

    state["seen"] = list(dict.fromkeys(list(seen) + [it["guid"] for it in items] + [str(it["id"]) for it in items]))
    state["max_id"] = max(max_id, current_max)
    state["last_run"] = now_iso
    state["first_run"] = False
    if len(gaps) >= int(cfg.get("gap_report_threshold", 3)):
        state.setdefault("gap_events", []).append({"at": now_iso, "ids": gaps})
        state["gap_events"] = state["gap_events"][-20:]

    if not dry_run:
        append_hits(hits)
        save_state(state, cfg["history_limit"])

    if first_run:
        log(f"首次运行，已建立基线（{len(items)} 条），不发送历史告警")
        return 0

    instant_hits = [hit for hit in hits if hit["instant"]]
    log(f"本轮新增 {len(new_items)} 条，命中 {len(hits)} 条（即时 {len(instant_hits)} 条）")
    if gaps:
        log(f"检测到可能的遗漏区间（共 {len(gaps)} 条）: {', '.join(str(pid) for pid in gaps[:20])}")

    if dry_run:
        for hit in hits:
            print(f"[score={hit['score']}] {hit['title']} -> {hit['link']}")
        return 0

    if instant_hits:
        instant_hits.sort(key=lambda hit: -hit["score"])
        subject = f"⚡ 疑似抢购/补货：{instant_hits[0]['title'][:40]}"
        send_mail(
            cfg,
            subject,
            render_html(instant_hits, subject, f"共 {len(instant_hits)} 条高优先命中 · {format_time(now_iso)}"),
            render_text(instant_hits, subject, f"共 {len(instant_hits)} 条高优先命中"),
        )
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
        subject = f"NodeSeek 美西 VPS 情报汇总（{len(hits)} 条）"
        intro = f"统计区间起于 {window}"
        if gap_links:
            intro += f" · 另有 {len(gap_links)} 条帖子可能落在 RSS 窗口之外"
        summary = ai_summary(cfg, hits, window) if use_ai else None
        if summary is None and use_ai and (cfg.get("ai") or {}).get("enabled") and hits:
            intro += " · ⚠️ AI 摘要本轮不可用，下面这封是关键词版（详见日志）"
        if summary:
            subject = f"NodeSeek 美西 VPS 日报（{len(hits)} 条）"
            raw_list = render_text(hits, "", "").strip()
            text_body = f"{summary}\n\n---- 原始命中 ----\n{raw_list}"
            if gap_links:
                text_body += "\n\n可能漏掉的帖子：\n" + "\n".join(gap_links)
            hot, warm = split_tiers(hits)
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
            text_body = render_text(hits, subject, intro)
            if gap_links:
                text_body += "\n\n可能漏掉的帖子：\n" + "\n".join(gap_links)
            html_body = render_html(hits, subject, intro)

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
