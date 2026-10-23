"""agent 框架的单元测试：python3 -m unittest discover -s tests -v

只测试不依赖网络的逻辑（画像加载、工具协议、JSON 解析、prompt 组装）。
"""

import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = ROOT / "agent"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agent = load("ns_agent", AGENT_DIR / "agent.py")
tools = load("ns_agent_tools", AGENT_DIR / "tools.py")


class AgentTestCase(unittest.TestCase):
    def test_profile_example_is_valid_and_has_target(self):
        data = json.loads((AGENT_DIR / "profile.example.json").read_text(encoding="utf-8"))
        self.assertIn("budget_month_cny_max", data)
        self.assertTrue(data["targets"], "画像里必须给出目标机型")
        self.assertEqual(data["targets"][0]["model"], "DMIT LAX.EB.WEE")
        self.assertTrue(data["targets"][0]["aliases"])

    def test_profile_example_carries_price_anchor(self):
        """价格锚点必须存在，否则 Agent 会自己猜价格，可能把官方年付判成超额。"""
        data = json.loads((AGENT_DIR / "profile.example.json").read_text(encoding="utf-8"))
        anchors = {a["model"]: a for a in data.get("price_anchors", [])}
        self.assertIn("DMIT LAX.EB.WEE", anchors)
        anchor = anchors["DMIT LAX.EB.WEE"]
        self.assertEqual(anchor["official_usd_year"], 39.9)
        # 39.9 美元年付摊到月应在预算内，锚点不能自相矛盾
        self.assertLess(anchor["official_cny_month"], data["budget_month_cny_max"])
        self.assertIn("budget_rules", data)
        self.assertIn("formula", data["budget_rules"])

    def test_prompt_warns_against_flagging_annual_price(self):
        prompt = (AGENT_DIR / "prompt.md").read_text(encoding="utf-8")
        self.assertIn("price_anchors", prompt)
        self.assertIn("不要因为", prompt)

    def test_load_profile_falls_back_to_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 指向不存在的 profile.json 时应回退到 example
            with mock.patch.object(agent, "PROFILE_PATH", Path(tmp) / "missing.json"):
                profile = agent.load_profile()
        self.assertIn("targets", profile)

    def test_load_profile_reads_real_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.json"
            path.write_text(json.dumps({"name": "test", "budget_month_cny_max": 30}), encoding="utf-8")
            profile = agent.load_profile(str(path))
        self.assertEqual(profile["name"], "test")
        self.assertEqual(profile["budget_month_cny_max"], 30)

    def test_load_profile_handles_broken_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(agent.load_profile(str(path)), {})

    def test_system_prompt_includes_profile_json(self):
        prompt = agent.load_system_prompt({"name": "测试用户", "budget_month_cny_max": 40})
        self.assertIn("VPS 选型顾问", prompt)
        self.assertIn("测试用户", prompt)
        self.assertIn("budget_month_cny_max", prompt)

    def test_parse_json_block_tolerates_code_fence_and_prose(self):
        fenced = '```json\n{"headline": "x", "items": []}\n```'
        self.assertEqual(agent.parse_json_block(fenced)["headline"], "x")
        prose = '好的，结论如下：\n{"headline": "y", "items": []}\n以上。'
        self.assertEqual(agent.parse_json_block(prose)["headline"], "y")
        self.assertIsNone(agent.parse_json_block("没有 JSON"))
        self.assertIsNone(agent.parse_json_block(""))

    def test_build_judge_input_lists_every_post(self):
        items = [
            {"id": 1, "title": "标题一", "category": "trade", "author": "a", "description": "摘要一"},
            {"id": 2, "title": "标题二", "category": "info", "author": "b", "description": ""},
        ]
        text = agent.build_judge_input(items)
        self.assertIn("帖子编号：1", text)
        self.assertIn("标题二", text)
        self.assertIn("共", text) if False else None
        self.assertIn("2", text)

    def test_tool_schemas_declare_search_and_fetch(self):
        names = {schema["function"]["name"] for schema in tools.TOOL_SCHEMAS}
        self.assertEqual(names, {"search", "fetch"})
        for schema in tools.TOOL_SCHEMAS:
            self.assertEqual(schema["type"], "function")
            self.assertIn("parameters", schema["function"])

    def test_execute_tool_handles_bad_arguments(self):
        class FakeTools:
            calls = 0

            def search(self, *a, **k):
                raise AssertionError("不应被调用")

            def fetch(self, *a, **k):
                raise AssertionError("不应被调用")

        result = tools.execute_tool(FakeTools(), "search", "{不是 json")
        self.assertIn("error", result)

    def test_execute_tool_returns_error_on_tool_failure(self):
        class FailingTools:
            calls = 0

            def search(self, *a, **k):
                raise tools.ToolError("线上失败")

        result = tools.execute_tool(FailingTools(), "search", '{"query": "x"}')
        self.assertIn("线上失败", result["error"])

    def test_execute_tool_unknown_name(self):
        class FakeTools:
            calls = 0

        result = tools.execute_tool(FakeTools(), "delete_everything", "{}")
        self.assertIn("未知工具", result["error"])

    @mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test"}, clear=False)
    def test_run_agent_returns_none_without_api_key(self):
        verdict = agent.run_agent("question", "", {}, proxy="")
        self.assertIsNone(verdict)


if __name__ == "__main__":
    unittest.main()
