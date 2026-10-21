"""watch.py 的单元测试：python3 -m unittest discover -s tests -v"""

import importlib.util
import json
import tempfile
import unittest
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
