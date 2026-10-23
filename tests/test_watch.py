"""watch.py 的单元测试：python3 -m unittest discover -s tests -v"""

import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location("nodeseek_watch", ROOT / "watch.py")
assert spec and spec.loader
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


def make_feed(items):
    """items: (post_id, title, description, category)"""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0"><channel>',
        "<title>NodeSeek</title>",
    ]
    for post_id, title, desc, category in items:
        parts.append(
            f"<item><title><![CDATA[{title}]]></title>"
            f"<description><![CDATA[{desc}]]></description>"
            f'<link>https://www.nodeseek.com/post-{post_id}-1</link>'
            f'<guid isPermaLink="false">{post_id}</guid>'
            f"<category><![CDATA[{category}]]></category>"
            f"<dc:creator><![CDATA[tester]]></dc:creator>"
            f"<pubDate>Wed, 21 Oct 2026 06:20:00 GMT</pubDate></item>"
        )
    parts.append("</channel></rss>")
    return "".join(parts).encode("utf-8")


class FakeResponse:
    """模拟 urllib 的返回对象，用于测试 AI 摘要解析。"""

    status = 200

    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


DEAL_POST = (
    900001,
    "[VMISS] 美西洛杉矶 CN2 GIA 补货通知 今晚 8 点开抢",
    "三网优化线路，库存有限。",
    "info",
)
RESALE_POST = (
    900002,
    "溢价80出vmiss US.LA.TRI.Basic 月付账单",
    "原价转让，非诚勿扰。",
    "trade",
)
NOISE_POST = (900003, "今天午饭吃什么呢", "随便水一贴", "daily")


class WatchTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._original = {
            "BASE": watch.BASE,
            "STATE_PATH": watch.STATE_PATH,
            "HITS_PATH": watch.HITS_PATH,
            "LOCK_PATH": watch.LOCK_PATH,
            "LOG_PATH": watch.LOG_PATH,
            "fetch_feed": watch.fetch_feed,
            "ai_judge": watch.ai_judge,
        }
        watch.BASE = base
        watch.STATE_PATH = base / "state.json"
        watch.HITS_PATH = base / "hits.jsonl"
        watch.LOCK_PATH = base / ".watch.lock"
        watch.LOG_PATH = base / "logs" / "watch.log"
        self.cfg = watch.deep_merge(watch.DEFAULT_CONFIG, {})
        # 默认让 AI 判定返回「没有重要情报」，测试不会真的打网络；
        # 需要验证通知行为的用例自己覆盖 ai_judge。
        watch.ai_judge = lambda _cfg, _items: {"headline": "", "items": []}

    def tearDown(self):
        for key, value in self._original.items():
            setattr(watch, key, value)
        self._tmp.cleanup()

    def feed(self, items):
        watch.fetch_feed = lambda retries=3: make_feed(items)

    def test_parse_feed_reads_fields(self):
        items = watch.parse_feed(make_feed([DEAL_POST]))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], 900001)
        self.assertEqual(items[0]["category"], "info")
        self.assertEqual(items[0]["author"], "tester")
        self.assertIn("VMISS", items[0]["title"])
        self.assertTrue(items[0]["published"].startswith("2026-10-21"))

    def test_strip_html_removes_markup(self):
        self.assertEqual(watch.strip_html("<p>hello   <b>world</b></p>"), "hello world")

    def test_clean_text_removes_ansi_and_artifacts(self):
        raw = "\x1b[31;1m未检测基本信息\x1b[0m [33;0m \\\\///''' " + "." * 20
        cleaned = watch.clean_text(raw)
        self.assertNotIn("\x1b", cleaned)
        self.assertNotIn("[31;1m", cleaned)
        self.assertNotIn("[33;0m", cleaned)
        self.assertIn("未检测基本信息", cleaned)
        self.assertNotIn("\\\\", cleaned)

    def test_is_meaningful_filters_junk(self):
        self.assertFalse(watch.is_meaningful("rt"))
        self.assertFalse(watch.is_meaningful(""))
        self.assertFalse(watch.is_meaningful("\\\\///'''.,,,'"))
        self.assertTrue(watch.is_meaningful("洛杉矶 9929 线路机，原生 IP 可以解锁 AI 服务"))

    def test_split_tiers_puts_instant_and_high_score_first(self):
        hits = [
            {"title": "a", "instant": True, "score": 9},
            {"title": "b", "instant": False, "score": 12},
            {"title": "c", "instant": False, "score": 7},
        ]
        hot, warm = watch.split_tiers(hits)
        self.assertEqual([h["title"] for h in hot], ["a", "b"])
        self.assertEqual([h["title"] for h in warm], ["c"])

    def test_why_line_prefers_agent_reason(self):
        item = {"title": "VMISS 补货", "ai_reason": "目标机型有人出货"}
        self.assertEqual(watch.why_line(item), "目标机型有人出货")
        # 没有 Agent 理由时退回命中词，且不报错
        self.assertEqual(watch.why_line({"title": "x", "matched": ["vmiss"]}), "vmiss")
        self.assertEqual(watch.why_line({"title": "x"}), "")

    def test_buy_request_never_becomes_hit(self):
        self.feed([NOISE_POST])
        watch.command_run(self.cfg)
        self.feed([NOISE_POST, (900010, "[收1台]月付5的vmiss美西cn2 gia tri", "带价来", "trade")])
        watch.command_run(self.cfg)
        self.assertEqual(watch.read_hits_since(None), [])

    def test_first_run_seeds_baseline_without_hits(self):
        self.feed([DEAL_POST, NOISE_POST])
        self.assertEqual(watch.command_run(self.cfg), 0)
        self.assertEqual(watch.read_hits_since(None), [])
        state = json.loads(watch.STATE_PATH.read_text(encoding="utf-8"))
        self.assertFalse(state["first_run"])
        self.assertEqual(state["max_id"], 900003)

    def test_run_detects_new_deal_and_skips_duplicates(self):
        """判断权在 AI：命中的新帖要原样交给 AI，重复帖子不再送审。"""
        cfg = watch.deep_merge(self.cfg, {"ai": {"enabled": True, "model": "test", "api_key": "sk-test"}})
        reviewed = []

        def fake_judge(_cfg, items):
            reviewed.append([item["id"] for item in items])
            return {
                "headline": "VMISS 补货",
                "items": [{"id": DEAL_POST[0], "important": True, "urgency": "high", "reason": "官方补货"}],
            }

        original = watch.ai_judge
        watch.ai_judge = fake_judge
        try:
            self.feed([NOISE_POST])
            watch.command_run(cfg)
            self.feed([NOISE_POST, DEAL_POST, RESALE_POST])
            watch.command_run(cfg)
            # 同一份 feed 再跑一次，不应把已见过的帖子重新送审
            watch.command_run(cfg)
        finally:
            watch.ai_judge = original

        self.assertEqual(reviewed, [[DEAL_POST[0], RESALE_POST[0]]])
        hits = watch.read_hits_since(None)
        self.assertEqual(len(hits), 1)
        self.assertIn("VMISS", hits[0]["title"])

    def test_digest_print_and_json(self):
        self.feed([NOISE_POST])
        watch.command_run(self.cfg)
        self.feed([NOISE_POST, DEAL_POST])
        watch.command_run(self.cfg)
        state = watch.load_state()
        self.assertEqual(watch.command_digest(self.cfg, print_only=True, state=state), 0)
        self.assertEqual(watch.command_digest(self.cfg, as_json=True, state=state), 0)

    def test_gap_events_recorded_only_above_threshold(self):
        self.feed([(900001, "水贴一", "", "daily")])
        watch.command_run(self.cfg)
        # 中间漏掉 5 个 id，超过 gap_report_threshold
        self.feed([(900007, "水贴七", "", "daily")])
        watch.command_run(self.cfg)
        state = watch.load_state()
        gaps = watch.recent_gaps(state, None, int(self.cfg["gap_report_threshold"]))
        self.assertEqual(gaps, [900002, 900003, 900004, 900005, 900006])

    def test_small_gap_is_not_reported(self):
        self.feed([(900001, "水贴一", "", "daily")])
        watch.command_run(self.cfg)
        self.feed([(900003, "水贴三", "", "daily")])
        watch.command_run(self.cfg)
        state = watch.load_state()
        self.assertEqual(watch.recent_gaps(state, None, int(self.cfg["gap_report_threshold"])), [])

    def test_send_mail_skips_when_disabled(self):
        self.assertFalse(watch.send_mail(self.cfg, "标题", "<p>hi</p>", "hi"))

    def test_env_overrides_smtp_and_ai(self):
        env = {
            "SMTP_HOST": "smtp.qq.com",
            "SMTP_PORT": "465",
            "SMTP_EMAIL": "sender@qq.com",
            "SMTP_PASSWORD": "authcode",
            "RECEIVER_EMAIL": "receiver@example.com",
            "AI_PROVIDER": "github-models",
            "GITHUB_TOKEN": "ghs_test_token",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = watch.apply_env_overrides(watch.deep_merge(watch.DEFAULT_CONFIG, {}))
        self.assertTrue(cfg["smtp"]["enabled"])
        self.assertEqual(cfg["smtp"]["port"], 465)
        self.assertEqual(cfg["smtp"]["to"], ["receiver@example.com"])
        self.assertEqual(cfg["smtp"]["from"], "sender@qq.com")
        self.assertTrue(cfg["ai"]["enabled"])
        self.assertEqual(cfg["ai"]["provider"], "github-models")
        self.assertEqual(cfg["ai"]["base_url"], "https://models.github.ai/inference")
        self.assertEqual(cfg["ai"]["model"], "openai/gpt-4o-mini")
        self.assertEqual(cfg["ai"]["api_key"], "ghs_test_token")

    def test_mail_to_accepts_multiple_addresses(self):
        with mock.patch.dict(os.environ, {"MAIL_TO": "a@example.com; b@example.com"}, clear=False):
            cfg = watch.apply_env_overrides(watch.deep_merge(watch.DEFAULT_CONFIG, {}))
        self.assertEqual(cfg["smtp"]["to"], ["a@example.com", "b@example.com"])

    def test_digest_even_if_empty_env(self):
        with mock.patch.dict(os.environ, {"DIGEST_EVEN_IF_EMPTY": "true"}, clear=False):
            cfg = watch.apply_env_overrides(watch.deep_merge(watch.DEFAULT_CONFIG, {}))
        self.assertTrue(cfg["digest_even_if_empty"])

    def test_mark_digested_advances_watermark(self):
        watch.STATE_PATH.write_text(json.dumps({"last_digest": None, "seen": []}), encoding="utf-8")
        self.assertEqual(watch.command_mark_digested(self.cfg), 0)
        state = json.loads(watch.STATE_PATH.read_text(encoding="utf-8"))
        self.assertIsNotNone(state["last_digest"])
        self.assertEqual(watch.read_hits_since(state["last_digest"]), [])

    def test_empty_digest_keeps_last_digest_when_mail_disabled(self):
        state = {"last_digest": None, "seen": [], "max_id": 0}
        self.assertEqual(watch.command_digest(self.cfg, state=state), 0)
        self.assertIsNone(state["last_digest"])

    def test_ai_summary_disabled_by_default(self):
        self.assertIsNone(watch.ai_summary(self.cfg, [{"title": "x"}], "10-21"))

    def test_system_prompt_uses_profile_override(self):
        with mock.patch.dict(os.environ, {"AI_USER_PROFILE": "某地运营商家宽"}, clear=False):
            prompt = watch.build_system_prompt(self.cfg)
        self.assertIn("某地运营商家宽", prompt)
        self.assertNotIn(watch.DEFAULT_USER_PROFILE, prompt)
        default_prompt = watch.build_system_prompt(self.cfg)
        self.assertIn(watch.DEFAULT_USER_PROFILE, default_prompt)

    def test_ai_summary_skips_without_api_key(self):
        cfg = watch.deep_merge(self.cfg, {"ai": {"enabled": True, "model": "test"}})
        with mock.patch.dict(os.environ, {"NODESEEK_AI_KEY": ""}):
            self.assertIsNone(watch.ai_summary(cfg, [{"title": "x"}], "10-21"))

    def test_ai_summary_parses_chat_completion(self):
        cfg = watch.deep_merge(self.cfg, {"ai": {"enabled": True, "model": "test-model", "api_key": "sk-test"}})
        calls = {}

        def fake_urlopen(request, timeout=None):
            calls["url"] = request.full_url
            calls["auth"] = request.headers.get("Authorization")
            calls["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse({"choices": [{"message": {"content": "  今天有值得抢的机器  "}}]})

        original = watch.urllib.request.urlopen
        watch.urllib.request.urlopen = fake_urlopen
        try:
            text = watch.ai_summary(
                cfg,
                [{"title": "VMISS 美西 CN2 GIA 补货", "link": "https://example.com", "score": 16}],
                "10-21 14:00",
            )
        finally:
            watch.urllib.request.urlopen = original
        self.assertEqual(text, "今天有值得抢的机器")
        self.assertEqual(calls["url"], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(calls["auth"], "Bearer sk-test")
        self.assertEqual(calls["body"]["model"], "test-model")
        self.assertIn("VMISS", calls["body"]["messages"][1]["content"])

    def test_ai_summary_falls_back_on_error(self):
        cfg = watch.deep_merge(self.cfg, {"ai": {"enabled": True, "model": "test", "api_key": "sk-test"}})

        def boom(request, timeout=None):
            raise OSError("network down")

        original = watch.urllib.request.urlopen
        watch.urllib.request.urlopen = boom
        try:
            self.assertIsNone(watch.ai_summary(cfg, [{"title": "x"}], "10-21"))
        finally:
            watch.urllib.request.urlopen = original

    def test_ai_summary_falls_back_to_responses_endpoint(self):
        cfg = watch.deep_merge(self.cfg, {"ai": {"enabled": True, "model": "test", "api_key": "sk-test"}})
        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append(request.full_url)
            if request.full_url.endswith("/chat/completions"):
                raise urllib.error.HTTPError(
                    request.full_url, 404, "Not Found", {}, io.BytesIO(b'{"error":"no such route"}')
                )
            return FakeResponse({"output": [{"content": [{"type": "output_text", "text": "  来自 Responses 的摘要  "}]}]})

        original = watch.urllib.request.urlopen
        watch.urllib.request.urlopen = fake_urlopen
        try:
            text = watch.ai_summary(cfg, [{"title": "VMISS 补货"}], "10-21")
        finally:
            watch.urllib.request.urlopen = original
        self.assertEqual(text, "来自 Responses 的摘要")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].endswith("/chat/completions"))
        self.assertTrue(calls[1].endswith("/responses"))

    def test_extract_responses_text_variants(self):
        self.assertEqual(watch.extract_responses_text({"output_text": " 直接文本 "}), "直接文本")
        self.assertEqual(
            watch.extract_responses_text({"output": [{"content": [{"text": "第一段"}, {"text": "第二段"}]}]}),
            "第一段\n第二段",
        )
        self.assertEqual(watch.extract_responses_text({}), "")

    def test_digest_uses_ai_summary_and_marks_state(self):
        hit = {
            "title": "VMISS 美西 CN2 GIA 补货",
            "link": "https://www.nodeseek.com/post-1-1",
            "category": "info",
            "description": "三网优化，库存有限",
            "matched": ["vmiss"],
            "score": 16,
            "important": True,
            "detected_at": "2026-10-21T06:00:00+00:00",
        }
        watch.HITS_PATH.write_text(json.dumps(hit, ensure_ascii=False) + "\n", encoding="utf-8")
        cfg = watch.deep_merge(self.cfg, {"ai": {"enabled": True, "model": "test", "api_key": "sk-test"}})
        captured = {}

        def fake_send(_cfg, subject, html_body, text_body):
            captured.update(subject=subject, html=html_body, text=text_body)
            return True

        original_send, original_ai = watch.send_mail, watch.ai_summary
        watch.send_mail = fake_send
        watch.ai_summary = lambda _cfg, _hits, _window: "今天没有特别值得抢的机器。"
        try:
            state = {"last_digest": None, "seen": [], "max_id": 0}
            self.assertEqual(watch.command_digest(cfg, state=state, use_ai=True), 0)
        finally:
            watch.send_mail, watch.ai_summary = original_send, original_ai
        self.assertIn("今天没有特别值得抢的机器", captured["text"])
        self.assertIn("VMISS", captured["text"])
        self.assertIsNotNone(state["last_digest"])

    def test_digest_marks_ai_failure_in_intro(self):
        hit = {
            "title": "VMISS 美西 CN2 GIA 补货",
            "link": "https://www.nodeseek.com/post-1-1",
            "category": "info",
            "description": "三网优化，库存有限",
            "matched": ["vmiss"],
            "score": 16,
            "important": True,
            "detected_at": "2026-10-21T06:00:00+00:00",
        }
        watch.HITS_PATH.write_text(json.dumps(hit, ensure_ascii=False) + "\n", encoding="utf-8")
        cfg = watch.deep_merge(self.cfg, {"ai": {"enabled": True, "model": "test", "api_key": "sk-test"}})
        captured = {}

        def fake_send(_cfg, subject, html_body, text_body):
            captured.update(subject=subject, html=html_body, text=text_body)
            return True

        original_send, original_ai = watch.send_mail, watch.ai_summary
        watch.send_mail = fake_send
        watch.ai_summary = lambda _cfg, _hits, _window: None  # 模拟调用失败
        try:
            state = {"last_digest": None, "seen": [], "max_id": 0}
            self.assertEqual(watch.command_digest(cfg, state=state, use_ai=True), 0)
        finally:
            watch.send_mail, watch.ai_summary = original_send, original_ai
        self.assertIn("AI 摘要本轮不可用", captured["text"])
        self.assertIn("VMISS", captured["text"])

    def test_render_html_escapes_content(self):
        item = {
            "title": "<script>alert(1)</script>",
            "link": "https://www.nodeseek.com/post-1-1",
            "description": "a & b",
            "category": "info",
            "matched": ["vmiss"],
            "score": 9,
        }
        rendered = watch.render_html([item], "标题", "说明")
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)

    def test_single_instance_blocks_second_run(self):
        with watch.SingleInstance(watch.LOCK_PATH):
            with self.assertRaises(SystemExit):
                with watch.SingleInstance(watch.LOCK_PATH):
                    pass
        self.assertFalse(watch.LOCK_PATH.exists())


if __name__ == "__main__":
    unittest.main()
