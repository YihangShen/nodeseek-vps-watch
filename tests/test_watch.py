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
        }
        watch.BASE = base
        watch.STATE_PATH = base / "state.json"
        watch.HITS_PATH = base / "hits.jsonl"
        watch.LOCK_PATH = base / ".watch.lock"
        watch.LOG_PATH = base / "logs" / "watch.log"
        self.cfg = watch.deep_merge(watch.DEFAULT_CONFIG, {})

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

    def test_why_line_labels_groups(self):
        item = {
            "title": "VMISS 美西 CN2 GIA 补货",
            "groups": {"routes": ["cn2 gia"], "regions": ["洛杉矶"], "penalty": ["-溢价"]},
        }
        line = watch.why_line(item)
        self.assertIn("线路 cn2 gia", line)
        self.assertIn("地区 洛杉矶", line)
        self.assertIn("注意 溢价", line)

    def test_group_hits_collects_keywords_by_group(self):
        item = {
            "title": "VMISS 美西 CN2 GIA 补货 新IP段",
            "description": "三网优化，1C1G 500GB，能解锁 ChatGPT",
        }
        groups = watch.group_hits(item, self.cfg["rules"])
        self.assertIn("vmiss", groups["brands"])
        self.assertIn("cn2 gia", groups["routes"])
        self.assertIn("补货", groups["deal_actions"])
        self.assertTrue(any(word.startswith("解锁") for word in groups["needs"]))

    def test_score_prefers_title_and_caps_group(self):
        item = {"title": "VMISS 美西 CN2 GIA 补货", "description": "", "category": "daily"}
        score, matched, topical = watch.score_item(item, self.cfg["rules"])
        # 品牌 6 + 线路 3 + 地区 2 + 动作 4
        self.assertEqual(score, 15)
        self.assertTrue(topical)
        self.assertIn("vmiss", matched)
        self.assertEqual(len([m for m in matched if m == "cn2"]), 1)

    def test_body_match_scores_lower_than_title(self):
        in_title = {"title": "DMIT 洛杉矶", "description": "", "category": "daily"}
        in_body = {"title": "求推荐一个机器", "description": "听说 DMIT 洛杉矶不错", "category": "daily"}
        self.assertGreater(
            watch.score_item(in_title, self.cfg["rules"])[0],
            watch.score_item(in_body, self.cfg["rules"])[0],
        )

    def test_penalty_keywords_reduce_score(self):
        item = {"title": "溢价收一台搬瓦工 CN2 GIA", "description": "", "category": "daily"}
        score, matched, _ = watch.score_item(item, self.cfg["rules"])
        self.assertTrue(any(m.startswith("-") for m in matched))
        self.assertLess(score, 10)

    def test_buy_request_is_vetoed(self):
        rules = self.cfg["rules"]
        for title in [
            "[收1台]月付5的vmiss tri",
            "【收】+100 收Vmiss Tri Basic",
            "[换] Vmiss Tri 5CAD 换干杯云",
            "求购搬瓦工 DC6",
            "IPLC 拼车 美西洛杉矶",
        ]:
            self.assertTrue(watch.is_vetoed({"title": title}, rules), title)
        self.assertFalse(watch.is_vetoed({"title": "VMISS 美西 CN2 GIA 补货"}, rules))

    def test_buy_request_never_becomes_hit(self):
        self.feed([NOISE_POST])
        watch.command_run(self.cfg)
        self.feed([NOISE_POST, (900010, "[收1台]月付5的vmiss美西cn2 gia tri", "带价来", "trade")])
        watch.command_run(self.cfg)
        self.assertEqual(watch.read_hits_since(None), [])

    def test_noise_post_does_not_alert(self):
        item = {"title": NOISE_POST[1], "description": NOISE_POST[2], "category": "daily"}
        score, _, topical = watch.score_item(item, self.cfg["rules"])
        self.assertFalse(topical)
        self.assertLess(score, self.cfg["threshold"])

    def test_instant_requires_route_keyword_in_title(self):
        route_item = {"title": "VMISS 美西 CN2 GIA 补货", "description": "", "category": "info"}
        no_route_item = {"title": "RackNerd 黑五年付优惠上架", "description": "洛杉矶机房", "category": "info"}
        score = watch.score_item(route_item, self.cfg["rules"])[0]
        other_score = watch.score_item(no_route_item, self.cfg["rules"])[0]
        self.assertTrue(watch.is_instant(route_item, score, self.cfg))
        self.assertFalse(watch.is_instant(no_route_item, other_score, self.cfg))

    def test_resale_post_is_not_instant(self):
        item = {"title": RESALE_POST[1], "description": RESALE_POST[2], "category": "trade"}
        score = watch.score_item(item, self.cfg["rules"])[0]
        self.assertFalse(watch.is_instant(item, score, self.cfg))

    def test_first_run_seeds_baseline_without_hits(self):
        self.feed([DEAL_POST, NOISE_POST])
        self.assertEqual(watch.command_run(self.cfg), 0)
        self.assertEqual(watch.read_hits_since(None), [])
        state = json.loads(watch.STATE_PATH.read_text(encoding="utf-8"))
        self.assertFalse(state["first_run"])
        self.assertEqual(state["max_id"], 900003)

    def test_run_detects_new_deal_and_skips_duplicates(self):
        self.feed([NOISE_POST])
        watch.command_run(self.cfg)
        self.feed([NOISE_POST, DEAL_POST, RESALE_POST])
        watch.command_run(self.cfg)
        hits = watch.read_hits_since(None)
        titles = [hit["title"] for hit in hits]
        self.assertEqual(len(hits), 1)
        self.assertIn("VMISS", titles[0])
        # 再跑一次同一份 feed，不应重复命中
        watch.command_run(self.cfg)
        self.assertEqual(len(watch.read_hits_since(None)), 1)

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

    def test_empty_digest_keeps_last_digest_when_mail_disabled(self):
        state = {"last_digest": None, "seen": [], "max_id": 0}
        self.assertEqual(watch.command_digest(self.cfg, state=state), 0)
        self.assertIsNone(state["last_digest"])

    def test_ai_summary_disabled_by_default(self):
        self.assertIsNone(watch.ai_summary(self.cfg, [{"title": "x"}], "10-21"))

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
