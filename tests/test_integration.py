"""Integration tests against the real AstrBot framework package.

Needs `astrbot` installed:

    <venv>/Scripts/python tests/test_integration.py -v

Without astrbot the whole module is skipped, so `test_core.py` stays
dependency-free. Uses REAL AstrMessageEvent / JSONResponse objects with a
stubbed Context (fake KB manager, cron manager and scripted LLM) plus a
scripted Bilibili client: covers plugin load, all /bilearn commands, both
LLM tools, panel APIs, quota/deferral/consolidation/audit flows.
Real-network Bilibili probes live in LiveBiliProbeTest (BILI_LIVE=1 only).
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# AstrBot import has a side effect: it creates ./data in CWD.
# Keep the repo clean by running from a scratch dir (all paths here are absolute).
import os
import tempfile

_IT_WORKDIR = os.path.join(tempfile.gettempdir(), "astrbot-it-workdir")
os.makedirs(_IT_WORKDIR, exist_ok=True)
os.chdir(_IT_WORKDIR)

try:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.message_components import Plain
    from astrbot.core.platform.astrbot_message import (
        AstrBotMessage,
        MessageMember,
        MessageType,
    )
    from astrbot.core.platform.platform_metadata import PlatformMetadata

    import importlib
    import types

    # AstrBot loads plugins as data.plugins.<dir>.main (a real package import),
    # so relative imports inside main.py work. Mirror that here.
    _pkg = types.ModuleType("astrbot_plugin_bili_learn")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["astrbot_plugin_bili_learn"] = _pkg
    plugin_main = importlib.import_module("astrbot_plugin_bili_learn.main")

    HAS_ASTRBOT = True
except Exception:  # noqa: BLE001
    HAS_ASTRBOT = False


VIDEOS = {
    "BVTEST000001": {
        "title": "科技前沿：AI 加速发展",
        "desc": "介绍人工智能的最新进展",
        "tname": "科技",
        "author": "科技君",
        "duration": 600,
        "subtitle": "人工智能 发展 迅速，科技 改变 生活。" * 20,
    },
    "BVTEST000002": {
        "title": "科技新品发布速览",
        "desc": "新手机新电脑一起看",
        "tname": "数码",
        "author": "数码君",
        "duration": 300,
        "subtitle": "",
    },
    "BVTEST000003": {
        "title": "深夜美食探店",
        "desc": "好吃的火锅推荐",
        "tname": "美食",
        "author": "吃货君",
        "duration": 400,
        "subtitle": "火锅 好吃 推荐",
    },
    "BVTEST000004": {
        "title": "科技马拉松：十小时超长直播",
        "desc": "超长科技直播",
        "tname": "科技",
        "author": "科技君",
        "duration": 8000,
        "subtitle": "科技 直播 内容",
    },
    "BVTEST000005": {
        "title": "数码新品开箱",
        "desc": "新手机开箱体验",
        "tname": "数码",
        "author": "数码君",
        "duration": 500,
        "subtitle": "数码 开箱 新手机 体验",
    },
}


class FakeKB:
    def __init__(self, kb_id="kb1"):
        self.kb = SimpleNamespace(kb_id=kb_id)
        self.docs: dict[str, dict] = {}
        self._n = 0

    async def upload_document(self, file_name, file_content, file_type):
        self._n += 1
        doc_id = f"doc-{self._n}"
        content = file_content.decode("utf-8") if isinstance(file_content, bytes) else str(file_content)
        self.docs[doc_id] = {"doc_name": file_name, "content": content}
        return SimpleNamespace(doc_id=doc_id)

    async def list_documents(self, offset=0, limit=100, search=None):
        out = []
        for doc_id, doc in self.docs.items():
            if search and search not in doc["doc_name"]:
                continue
            out.append(SimpleNamespace(doc_id=doc_id, doc_name=doc["doc_name"]))
        return out[offset : offset + limit]

    async def get_chunks_by_doc_id(self, doc_id, offset=0, limit=100):
        doc = self.docs.get(doc_id, {})
        return [{"chunk_index": 0, "content": doc.get("content", "")}]

    async def delete_document(self, doc_id):
        self.docs.pop(doc_id, None)


class FakeKBManager:
    def __init__(self, mode="ok"):
        self.mode = mode
        self.helpers: dict[str, FakeKB] = {}

    async def get_kb_by_name(self, name):
        return self.helpers.get(name)

    async def create_kb(self, kb_name, **kwargs):
        helper = FakeKB()
        self.helpers[kb_name] = helper
        return helper

    async def get_kb(self, kb_id):
        for helper in self.helpers.values():
            if helper.kb.kb_id == kb_id:
                return helper
        return None

    async def retrieve(self, query="", kb_names=None, **kwargs):
        if self.mode == "fail":
            raise RuntimeError("kb down")
        if self.mode == "empty":
            return {"results": []}
        items = []
        for helper in self.helpers.values():
            for doc_id, doc in helper.docs.items():
                if query and query not in doc["content"] and query not in doc["doc_name"]:
                    continue
                items.append(
                    {
                        "doc_name": doc["doc_name"], "doc_id": doc_id,
                        "score": 0.9, "content": doc["content"][:500],
                    }
                )
        return {"results": items}


class FakeCron:
    def __init__(self):
        self.jobs: dict[str, object] = {}
        self._n = 0

    async def list_jobs(self, job_type=None):
        return list(self.jobs.values())

    async def delete_job(self, job_id):
        self.jobs.pop(job_id, None)

    async def add_basic_job(self, **kwargs):
        self._n += 1
        job = SimpleNamespace(job_id=f"job-{self._n}", name=kwargs.get("name", ""))
        self.jobs[job.job_id] = job
        return job

    async def get_next_run_time(self, job_id):
        return None


class FakeBili:
    """Scripted stand-in for BiliClient (no network)."""

    def __init__(self, search_map=None):
        self.search_map = search_map or {}
        self.calls: list = []
        self.sessdata = ""
        self.throttle = SimpleNamespace(cooldown_remaining=lambda: 0.0)

    async def search_videos(self, keyword, page=1):
        self.calls.append(("search", keyword, page))
        return [dict(v) for v in self.search_map.get((keyword, page), [])]

    async def view(self, bvid="", aid=None):
        self.calls.append(("view", bvid or aid))
        key = bvid or f"av{aid}"
        if key not in VIDEOS:
            raise RuntimeError("no such video")
        meta = dict(VIDEOS[key])
        return {
            "bvid": key, "aid": 123, "cid": 456,
            "pages": [{"cid": 456, "page": 1, "part": ""}],
            "title": meta["title"], "desc": meta["desc"], "tname": meta["tname"],
            "author": meta["author"], "mid": "789", "pic": "",
            "duration": meta["duration"], "view_count": 1, "like_count": 1,
            "url": f"https://www.bilibili.com/video/{key}",
        }

    async def subtitles_for_pages(self, meta, page_limit=3, verify=True):
        bvid = str(meta.get("bvid") or "")
        for key in (bvid, (meta.get("pages") or [{}])[0].get("bvid", "")):
            if key in VIDEOS:
                return VIDEOS[key]["subtitle"]
        return ""

    async def resolve_bvid(self, reference):
        text = str(reference or "").strip()
        if text in VIDEOS:
            return text
        if text.startswith("BV") and len(text) >= 12:
            return text[:12]
        return ""


class FakeContext:
    def __init__(self, kb_mode="ok"):
        self.routes: dict[str, tuple] = {}
        self.sent: list = []
        self.tools: list = []
        self.kb_manager = FakeKBManager(kb_mode)
        self.cron_manager = FakeCron()
        self.llm_calls: list[str] = []
        self.llm_providers: list[str] = []
        self.refuse_providers: set[str] = set()

    def get_all_stars(self):
        return []

    def get_all_providers(self):
        return []

    def get_all_embedding_providers(self):
        return []

    @property
    def provider_manager(self):
        return SimpleNamespace(inst_map={})

    def get_provider_by_id(self, _pid):
        return None

    def get_config(self):
        return {}

    def register_web_api(self, route, handler, methods, *args, **kwargs):
        self.routes[route] = (handler, methods)

    def add_llm_tools(self, *tools):
        self.tools.extend(tools)

    async def get_current_chat_provider_id(self, _session):
        return "fake"

    async def llm_generate(self, chat_provider_id="", prompt=""):
        self.llm_calls.append(prompt)
        self.llm_providers.append(str(chat_provider_id or ""))
        if chat_provider_id in self.refuse_providers:
            return SimpleNamespace(
                completion_text="抱歉，我无法提供该内容。", usage=None,
            )
        if "你是知识整理员" in prompt:
            return SimpleNamespace(completion_text="- 新要点A\n- 新要点B", usage=None)
        if "你是审校员" in prompt or "你是事实核查员" in prompt:
            return SimpleNamespace(completion_text="结论：通过\n问题：无", usage=None)
        if "你是视频摘要员" in prompt:
            if "OFFTOPIC" in prompt or "美食探店" in prompt:
                return SimpleNamespace(
                    completion_text="标题：美食探店\n分区：其他\n相关度：10\n\n- 要点：好吃",
                    usage=None,
                )
            if "数码新品" in prompt:
                return SimpleNamespace(
                    completion_text=(
                        "标题：数码新品开箱\n分区：数码\n相关度：88\n\n"
                        "- 要点一：视频摘要，非亲历\n- 要点二：开箱内容\n- 要点三：值得一看"
                    ),
                    usage=None,
                )
            return SimpleNamespace(
                completion_text=(
                    "标题：测试视频摘要\n分区：科技\n相关度：90\n\n"
                    "- 要点一：视频摘要，非亲历\n- 要点二：内容真实\n- 要点三：值得学习"
                ),
                usage=None,
            )
        return SimpleNamespace(completion_text="", usage=None)

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))


class FakeRequest:
    class Query(dict):
        def get(self, key, default=None, type=None):  # noqa: A002
            value = super().get(key, default)
            if type is not None and value is not default:
                try:
                    return type(value)
                except (TypeError, ValueError):
                    return default
            return value

    def __init__(self, query=None, body=None):
        self.query = FakeRequest.Query(query or {})
        self._body = body or {}

    async def json(self, default=None):
        return self._body if self._body is not None else (default or {})


def make_event(text, sid="u1", name="阿U", group="1", platform="aiocqhttp", role="member"):
    msg = AstrBotMessage()
    msg.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    msg.self_id = "bot1"
    msg.sender = MessageMember(user_id=sid, nickname=name)
    msg.message = [Plain(text)]
    msg.message_str = text
    if group:
        msg.group_id = group
    meta = PlatformMetadata(name=platform, description="test", id=platform)
    event = AstrMessageEvent(text, msg, meta, group or sid)
    event.role = role
    return event


async def _collect(gen):
    return [r async for r in gen]


class ConfigDict(dict):
    """Mimics AstrBotConfig for save paths (records persistence calls)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = 0

    def save_config(self):
        self.saved += 1


def _text(result):
    try:
        return result.get_plain_text()
    except Exception:  # noqa: BLE001
        return str(result)


def _json(resp):
    return json.loads(resp.body.decode("utf-8"))


def _item(keyword, bvid, source="search"):
    meta = dict(VIDEOS[bvid])
    return {
        "bvid": bvid, "title": meta["title"], "author": meta["author"],
        "mid": "789", "description": meta["desc"], "typename": meta["tname"],
        "url": f"https://www.bilibili.com/video/{bvid}",
        "source": source, "keyword": keyword,
    }


@unittest.skipUnless(HAS_ASTRBOT, "astrbot package not installed")
class BiliIntegrationTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self._old_data_dir = plugin_main._data_dir
        root = Path(self.tmp.name)
        plugin_main._data_dir = lambda: root
        self.ctx = FakeContext()
        self.plugin = plugin_main.BiliLearnPlugin(self.ctx, self._config())
        self.plugin.client = FakeBili()
        self.plugin.pipeline.client = self.plugin.client
        self.ctx.kb_manager.helpers["Bili Learn"] = FakeKB("kb1")
        self.plugin._kb_id = "kb1"
        self.store = self.plugin.store

    def tearDown(self):
        plugin_main._data_dir = self._old_data_dir
        try:
            for task in list(self.plugin._bg_tasks):
                task.cancel()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass
        self.tmp.cleanup()

    def _config(self):
        return ConfigDict(
            {
                "keywords": "科技",
                "daily_per_keyword": 5,
                "daily_start_hour": 1,
                "max_duration_minutes": 120,
                "on_demand_enabled": True,
                "on_demand_ingest": True,
                "query_enabled": True,
                "consolidate_enabled": True,
                "consolidate_threshold": 10,
                "audit_enabled": True,
            }
        )

    def _search_map(self, *bvids):
        return {("科技", 1): [_item("科技", bvid) for bvid in bvids]}

    # -- load ----------------------------------------------------------

    def test_plugin_loads_and_registers(self):
        for name in (
            "status", "recent", "run", "runs", "run-events", "health",
            "interest", "interest/save", "theme", "theme/save", "unlimited",
        ):
            self.assertIn(f"/astrbot_plugin_bili_learn/{name}", self.ctx.routes)
        self.assertEqual(len(self.ctx.tools), 2)
        names = sorted(getattr(tool, "name", "") for tool in self.ctx.tools)
        self.assertEqual(names, ["bilibili_knowledge", "bilibili_read"])
        asyncio.run(self.plugin.initialize())
        self.assertTrue(self.plugin._cron_id)

    # -- full run --------------------------------------------------------

    def test_run_ingests_two_videos(self):
        self.plugin.client.search_map = self._search_map("BVTEST000001", "BVTEST000002")
        result = asyncio.run(self.plugin.pipeline.run(trigger="manual", kb_id="kb1"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["ingested"], 2)
        self.assertEqual(self.store.get("BVTEST000001")["status"], "ingested")
        self.assertEqual(self.store.quota_used("科技"), 2)

    def test_quota_deferral(self):
        # 延期只发生在“视频归类与轮次关键词不同、且该归类配额已满”时。
        # 这里 BV5 在“科技”搜索结果里，但被归类为“数码”，而数码配额已用完。
        self.plugin.config["keywords"] = "科技,数码"
        self.plugin.config["daily_per_keyword"] = 5
        self.plugin.config["interest_quotas"] = [
            {"keyword": "科技", "quota": 5},
            {"keyword": "数码", "quota": 5},
        ]
        self.store.quota_add("数码", amount=5)
        self.plugin.client.search_map = self._search_map("BVTEST000001", "BVTEST000005")
        result = asyncio.run(self.plugin.pipeline.run(trigger="manual", kb_id="kb1"))
        row = self.store.get("BVTEST000005")
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "deferred")
        self.assertEqual(row["reason"], "quota_full")

    def test_fullwidth_keyword_separators(self):
        self.plugin.config["keywords"] = "科技，数码、AI；提示词 构图\n审美"
        self.assertEqual(
            self.plugin.pipeline.keywords(),
            ["科技", "数码", "AI", "提示词", "构图", "审美"],
        )
        self.plugin.config["exclude_keywords"] = "广告，推广"
        self.assertEqual(self.plugin.pipeline.exclude(), ["广告", "推广"])

    def test_off_topic_and_too_long(self):
        self.plugin.client.search_map = self._search_map("BVTEST000003", "BVTEST000004")
        result = asyncio.run(self.plugin.pipeline.run(trigger="manual", kb_id="kb1"))
        reasons = {row["reason"] for row in
                   (self.store.get("BVTEST000003"), self.store.get("BVTEST000004"))}
        self.assertIn("off_topic", reasons)
        self.assertIn("too_long", reasons)
        self.assertEqual(result["ingested"], 0)

    def test_no_subtitle_skip_retries(self):
        self.plugin.config["missing_subtitle"] = "skip"
        self.plugin.client.search_map = self._search_map("BVTEST000002")
        result = asyncio.run(self.plugin.pipeline.run(trigger="manual", kb_id="kb1"))
        row = self.store.get("BVTEST000002")
        self.assertEqual(row["status"], "no_subtitle")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(result["ingested"], 0)

    # -- on demand ---------------------------------------------------------

    def test_read_one_caches(self):
        first = asyncio.run(self.plugin.pipeline.read_one("BVTEST000001", ingest=True))
        self.assertTrue(first["ok"])
        self.assertFalse(first["cached"])
        self.assertTrue(first["ingested"])
        second = asyncio.run(self.plugin.pipeline.read_one("BVTEST000001", ingest=True))
        self.assertTrue(second["cached"])
        bad = asyncio.run(self.plugin.pipeline.read_one("根本不是链接", ingest=True))
        self.assertFalse(bad["ok"])

    def test_read_tool_and_knowledge_tool(self):
        read_tool, knowledge_tool = self.ctx.tools[0], self.ctx.tools[1]
        out = asyncio.run(read_tool.call(None, reference="BVTEST000001"))
        self.assertIn("测试视频摘要", out)
        out = asyncio.run(knowledge_tool.call(None, query="科技"))
        self.assertIn("科技", out)
        out = asyncio.run(knowledge_tool.call(None))
        self.assertIn("概览", out)
        out = asyncio.run(knowledge_tool.call(None, arguments={"query": "科技"}))
        self.assertIn("科技", out)

    def test_knowledge_fallback_to_local(self):
        self.plugin.client.search_map = self._search_map("BVTEST000001")
        asyncio.run(self.plugin.pipeline.run(trigger="manual", kb_id="kb1"))
        self.ctx.kb_manager.mode = "fail"
        out = asyncio.run(self.plugin._query_knowledge(query="科技"))
        self.assertEqual(out["source"], "local")
        self.assertTrue(out["items"])

    # -- consolidate + audit --------------------------------------------------

    def test_consolidate_roundtrip(self):
        self.plugin.client.search_map = self._search_map("BVTEST000001", "BVTEST000002")
        self.plugin.config["daily_per_keyword"] = 20
        asyncio.run(self.plugin.pipeline.run(trigger="manual", kb_id="kb1"))
        for _ in range(4):
            asyncio.run(self.plugin.pipeline.run(trigger="manual", kb_id="kb1"))
        result = asyncio.run(self.plugin.pipeline.consolidate(keyword="科技", force=True))
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["sources"], 2)
        digest = self.store.get_digest("科技")
        self.assertTrue(digest and digest["rounds"] >= 1)
        again = asyncio.run(self.plugin.pipeline.consolidate(keyword="科技", force=True))
        self.assertEqual(again.get("reason"), "nothing")

    def test_audit_flow(self):
        self.plugin.client.search_map = self._search_map("BVTEST000001")
        result = asyncio.run(self.plugin.pipeline.run(trigger="manual", kb_id="kb1"))
        # run 结束时已自动审过入库视频。
        self.assertEqual(result["audit"]["audited"], 1)
        self.assertEqual(self.store.audit_stats()["ok"], 1)
        # 再审就没有待审的了。
        again = asyncio.run(self.plugin.pipeline.audit_due_docs(limit=5, force=True))
        self.assertEqual(again["audited"], 0)

    # -- commands ---------------------------------------------------------------

    def test_commands_status_once_search_recent(self):
        out = _text(asyncio.run(_collect(self.plugin.cmd_status(make_event("/bilearn status"))))[0])
        self.assertIn("Bili Learn", out)
        self.plugin.client.search_map = self._search_map("BVTEST000001")
        out = _text(asyncio.run(_collect(self.plugin.cmd_once(make_event("/bilearn once"))))[0])
        self.assertIn("运行完成", out)
        out = _text(asyncio.run(_collect(self.plugin.cmd_search(make_event("/bilearn search 科技"))))[0])
        self.assertIn("科技", out)
        out = _text(asyncio.run(_collect(self.plugin.cmd_search(make_event("/bilearn search"))))[0])
        self.assertIn("概览", out)
        out = _text(asyncio.run(_collect(self.plugin.cmd_recent(make_event("/bilearn recent 2"))))[0])
        self.assertIn("BVTEST", out)
        out = _text(asyncio.run(_collect(self.plugin.cmd_quota(make_event("/bilearn quota"))))[0])
        self.assertIn("今日配额", out)
        out = _text(asyncio.run(_collect(self.plugin.cmd_quota(make_event("/bilearn quota reset"))))[0])
        self.assertIn("已重置", out)

    def test_command_read_digest_audit(self):
        out = _text(asyncio.run(_collect(self.plugin.cmd_read(make_event("/bilearn read"))))[0])
        self.assertIn("用法", out)
        out = _text(asyncio.run(_collect(self.plugin.cmd_read(make_event("/bilearn read BVTEST000001"))))[0])
        self.assertIn("测试视频摘要", out)
        out = _text(asyncio.run(_collect(self.plugin.cmd_digest(make_event("/bilearn digest 科技"))))[0])
        self.assertTrue("汇总" in out or "没有" in out)
        out = _text(asyncio.run(_collect(self.plugin.cmd_audit(make_event("/bilearn audit"))))[0])
        self.assertIn("审核", out)

    # -- panel apis -----------------------------------------------------------------

    def test_page_status_health_interest(self):
        plugin_main.request = FakeRequest()
        try:
            data = _json(asyncio.run(self.plugin.page_status()))
            self.assertIn("counts", data)
            self.assertEqual(data["version"], plugin_main.PLUGIN_VERSION)
            data = _json(asyncio.run(self.plugin.page_health()))
            self.assertIn("knowledge_base", data)
            data = _json(asyncio.run(self.plugin.page_interest()))
            self.assertIn("items", data)
            data = _json(asyncio.run(self.plugin.page_runs()))
            self.assertIn("items", data)
            data = _json(asyncio.run(self.plugin.page_recent()))
            self.assertIn("items", data)
        finally:
            plugin_main.request = FakeRequest()

    def test_page_interest_save_and_theme(self):
        plugin_main.request = FakeRequest(body={"items": [{"keyword": "AI", "quota": 3}]})
        try:
            data = _json(asyncio.run(self.plugin.page_interest_save()))
            self.assertTrue(data["saved"])
            self.assertEqual(self.plugin.config["interest_quotas"][0]["keyword"], "AI")
            plugin_main.request = FakeRequest(body={"items": "nope"})
            self.assertEqual(asyncio.run(self.plugin.page_interest_save()).status_code, 400)
            plugin_main.request = FakeRequest(body={"name": "clay", "custom": {"bg": "#fff"}})
            data = _json(asyncio.run(self.plugin.page_theme_save()))
            self.assertTrue(data["saved"])
            plugin_main.request = FakeRequest(body={"enabled": True})
            data = _json(asyncio.run(self.plugin.page_unlimited()))
            self.assertTrue(data["enabled"])
            plugin_main.request = FakeRequest(body={"enabled": False})
            data = _json(asyncio.run(self.plugin.page_unlimited()))
            self.assertFalse(data["enabled"])
            plugin_main.request = FakeRequest()
            data = _json(asyncio.run(self.plugin.page_run()))
            self.assertTrue(data["started"])
            asyncio.run(asyncio.sleep(0.5))
            self.assertIsNotNone(self.store.last_run())
        finally:
            plugin_main.request = FakeRequest()

    def test_page_run_events_and_reset_paths(self):
        plugin_main.request = FakeRequest(query={"run_id": "nope"})
        try:
            data = _json(asyncio.run(self.plugin.page_run_events()))
            self.assertEqual(data["items"], [])
        finally:
            plugin_main.request = FakeRequest()

    # -- 模型调用策略 ------------------------------------------------------------------

    def test_tier_routing_and_usage_ledger(self):
        self.plugin.config["quality_provider_id"] = "p-quality"
        self.plugin.config["fast_provider_id"] = "p-fast"
        self.plugin.client.search_map = self._search_map("BVTEST000001")
        result = asyncio.run(self.plugin.pipeline.run(trigger="manual", kb_id="kb1"))
        self.assertEqual(result["ingested"], 1)
        self.assertIn("p-quality", self.ctx.llm_providers, "摘要应走精准档")
        self.assertIn("p-fast", self.ctx.llm_providers, "审核应走快速档")
        tasks = {row["task"] for row in self.store.llm_usage_today_by_task()}
        self.assertEqual(tasks, {"summary", "utility"})
        self.assertGreater(self.store.llm_tokens_today(), 0)
        plugin_main.request = FakeRequest()
        try:
            data = _json(asyncio.run(self.plugin.page_status()))
        finally:
            plugin_main.request = FakeRequest()
        self.assertGreater(data["tokens"]["used"], 0)
        self.assertEqual(data["tokens"]["hard_limit"], 0)
        self.assertTrue(data["tokens"]["by_task"])

    def test_explicit_provider_beats_tier(self):
        self.plugin.config["quality_provider_id"] = "p-quality"
        self.plugin.config["summary_provider_id"] = "p-explicit"
        self.plugin.client.search_map = self._search_map("BVTEST000001")
        asyncio.run(self.plugin.pipeline.run(trigger="manual", kb_id="kb1"))
        self.assertIn("p-explicit", self.ctx.llm_providers)
        self.assertNotIn("p-quality", self.ctx.llm_providers)

    def test_hard_budget_limit_stops_run(self):
        self.store.add_llm_usage("summary", "p-old", 500, 100)
        self.plugin.config["daily_token_limit"] = 100
        self.plugin.client.search_map = self._search_map("BVTEST000001")
        result = asyncio.run(self.plugin.pipeline.run(trigger="manual", kb_id="kb1"))
        self.assertEqual(result["aborted"], "budget")
        self.assertEqual(result["ingested"], 0)
        self.assertIsNone(self.store.get("BVTEST000001"), "被闸住的视频不应写入状态")
        skips = self.store.llm_skips_today()
        self.assertTrue(skips)
        self.assertEqual(skips[0]["reason"], "daily_token_limit")
        last = self.store.last_run()
        self.assertEqual(last["status"], "budget_limited")
        events = self.store.run_events(last["run_id"], 50)
        self.assertTrue(any("预算" in e["message"] for e in events))

    def test_soft_budget_limit_skips_only_low_priority(self):
        self.plugin.config["soft_token_limit"] = 10
        self.store.add_llm_usage("summary", "p-old", 20, 0)
        self.plugin.client.search_map = self._search_map("BVTEST000001")
        result = asyncio.run(self.plugin.pipeline.run(trigger="manual", kb_id="kb1"))
        self.assertEqual(result["ingested"], 1, "摘要属于高优先级，软限不拦")
        self.assertEqual((result["audit"] or {}).get("reason"), "budget", "审核属于低优先级，被软限拦下")
        reasons = {row["reason"] for row in self.store.llm_skips_today()}
        self.assertIn("soft_token_limit", reasons)

    def test_refusal_retry_uses_fallback_provider(self):
        self.plugin.config["fallback_provider_id"] = "p-backup"
        self.ctx.refuse_providers = {"fake"}  # 默认提供商（当前会话模型）拒答
        self.plugin.client.search_map = self._search_map("BVTEST000001")
        result = asyncio.run(self.plugin.pipeline.run(trigger="manual", kb_id="kb1"))
        self.assertEqual(result["ingested"], 1)
        self.assertIn("p-backup", self.ctx.llm_providers, "拒答后应换备用模型重试")
        self.assertEqual(self.store.get("BVTEST000001")["status"], "ingested")

    def test_single_call_cap_uses_fallback(self):
        self.plugin.config["single_call_token_cap"] = 5
        self.plugin.config["fallback_provider_id"] = "p-backup"
        self.plugin.client.search_map = self._search_map("BVTEST000001")
        asyncio.run(self.plugin.pipeline.run(trigger="manual", kb_id="kb1"))
        self.assertIn("p-backup", self.ctx.llm_providers)

    def test_terminate_waits_background_tasks(self):
        cleaned_up = False

        async def _fake_bg():
            nonlocal cleaned_up
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                cleaned_up = True
                raise

        async def _test():
            task = asyncio.create_task(_fake_bg())
            await asyncio.sleep(0)
            self.plugin._bg_tasks.add(task)
            await self.plugin.terminate()
            self.assertTrue(cleaned_up)
            self.assertEqual(len(self.plugin._bg_tasks), 0)

        asyncio.run(_test())


@unittest.skipUnless(HAS_ASTRBOT, "astrbot package not installed")
class LiveBiliProbeTest(unittest.TestCase):
    """Real-network probes against api.bilibili.com (read-only, a few requests).

    Run explicitly: BILI_LIVE=1 <venv>/Scripts/python tests/test_integration.py -v
    """

    def setUp(self):
        import os

        if os.environ.get("BILI_LIVE") != "1":
            self.skipTest("live network probes disabled")
        from bili.client import BiliClient

        self.client = BiliClient(sessdata="", interval=1.0)

    def test_live_search_view_subtitle(self):
        import asyncio

        from bili.client import BiliRiskError

        async def go():
            results = await self.client.search_videos("Python 编程", 1)
            self.assertTrue(results, "search returned nothing")
            first = results[0]
            self.assertTrue(first["bvid"].startswith("BV"))
            for key in ("title", "author", "description", "typename", "url"):
                self.assertTrue(first[key], key)
            try:
                meta = await self.client.view(first["bvid"])
            except BiliRiskError as exc:
                # 某些网络出口会被 view 接口风控 412：不断言成功，
                # 只断言走了风控路径（抛 BiliRiskError + 全局冷却生效）。
                self.assertGreater(self.client.throttle.cooldown_remaining(), 0)
                print(f"\nlive: view blocked by env risk control ({exc}), search OK")
                return
            self.assertEqual(meta["bvid"], first["bvid"])
            self.assertTrue(meta["title"])
            self.assertTrue(meta["cid"])
            text = await self.client.subtitle_text(
                meta.get("aid"), meta.get("cid"), first["bvid"], meta.get("title") or "",
            )
            print(f"\nlive: {first['bvid']} subtitle_chars={len(text)}")

        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
