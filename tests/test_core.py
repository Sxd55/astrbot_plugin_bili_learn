"""Offline tests: document rendering, sampling, references, retry state machine, migration."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bili.client import (  # noqa: E402
    BiliClient,
    BiliHttpError,
    BiliRiskError,
    _subtitle_url,
    _track_rank,
    subtitle_mismatch,
    valid_buvid3,
)
from bili.ingest import (  # noqa: E402
    build_doc_name,
    declared_score,
    guess_category,
    is_suspect_verdict,
    match_category,
    parse_summary,
    render_digest,
    render_digest_section,
    render_document,
    review_verdict,
    sample_text,
    worth_keeping,
)
from bili.pipeline import LearnPipeline  # noqa: E402
from bili.pipeline import _as_int, _split_keywords  # noqa: E402
from bili.throttle import BiliThrottle  # noqa: E402
from bili.query import (  # noqa: E402
    format_for_chat,
    format_for_llm,
    unwrap_arguments,
)
from bili.reference import command_remainder, extract_video_reference  # noqa: E402
from bili.store import AuditStore  # noqa: E402

LEGACY_SCHEMA = """
CREATE TABLE videos (
    bvid TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    reason TEXT NOT NULL DEFAULT '',
    doc_id TEXT NOT NULL DEFAULT '',
    chars INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    keyword TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE
);
"""


class DocumentTest(unittest.TestCase):
    def test_render_document_marks_summary(self):
        text = render_document(
            {
                "bvid": "BV1xx",
                "title": "示例科普",
                "author": "某UP",
                "tname": "科学",
                "url": "https://www.bilibili.com/video/BV1xx",
            },
            "要点：\n- 这是公开视频里提到的概念。",
            "2026-09-12 12:00",
            "字幕",
        )
        self.assertIn("BV1xx", text)
        self.assertIn("不是 Bot 亲历", text)
        self.assertIn("示例科普", text)
        self.assertIn("素材: 字幕", text)

    def test_exclude_keyword(self):
        self.assertFalse(worth_keeping("震惊骗局", "abc", "", ["骗局"]))
        self.assertTrue(worth_keeping("Python 入门", "列表推导", "", ["骗局"]))

    def test_sample_text_keeps_head_middle_tail(self):
        text = "HEAD" + "x" * 5000 + "MIDDLE" + "y" * 5000 + "TAIL"
        out = sample_text(text, 200)
        self.assertLessEqual(len(out), 200)
        self.assertIn("HEAD", out)
        self.assertIn("MIDDLE", out)
        self.assertIn("TAIL", out)
        self.assertIn("（中间字幕已省略）", out)

    def test_sample_text_noop_when_short(self):
        self.assertEqual(sample_text("短字幕", 200), "短字幕")

    def test_parse_summary(self):
        raw = "标题：多Agent终端管理工具\n分区：AI\n\n- 要点一\n- 要点二"
        title, category, body = parse_summary(raw, "备选标题", ["AI", "科技"])
        self.assertEqual(title, "多Agent终端管理工具")
        self.assertEqual(category, "AI")
        self.assertIn("- 要点一", body)
        self.assertNotIn("标题：", body)
        self.assertNotIn("分区：", body)

    def test_parse_summary_fallback(self):
        title, category, body = parse_summary("随便一段正文", "视频原标题", ["AI"])
        self.assertEqual(title, "视频原标题")
        self.assertEqual(category, "其他")
        self.assertEqual(body, "随便一段正文")

    def test_build_doc_name_sanitizes(self):
        self.assertEqual(build_doc_name("AI", 'a/b:c*d?e"f<g>h|i'), "AI｜a b c d e f g h i.md")
        self.assertTrue(build_doc_name("", "").endswith(".md"))

    def test_guess_category(self):
        self.assertEqual(guess_category(["AI", "科技"], "聊聊 AI 提示词"), "AI")
        self.assertEqual(guess_category(["AI"], "无关内容"), "其他")

    def test_keyword_ascii_boundary(self):
        self.assertEqual(guess_category(["AI"], "How to train your model"), "其他")
        self.assertEqual(guess_category(["AI"], "openai 发布会"), "其他")
        self.assertEqual(guess_category(["AI"], "AI绘画教程"), "AI")
        self.assertEqual(guess_category(["聊天技巧"], "怎么聊天技巧"), "聊天技巧")

    def test_match_category_ascii_boundary(self):
        self.assertEqual(match_category("train", ["AI"]), "其他")
        self.assertEqual(match_category("AI助手", ["AI"]), "AI")
        self.assertEqual(match_category("聊天", ["聊天技巧"]), "聊天技巧")

    def test_declared_score(self):
        self.assertEqual(declared_score("分区：AI\n相关度：85"), 85)
        self.assertEqual(declared_score("相关度：120"), 100)
        self.assertIsNone(declared_score("没有分数"))

    def test_render_digest_and_section(self):
        section = render_digest_section(1, "2026-09-13", ["BV1", "BV2"], "- 要点A")
        self.assertIn("第 1 轮新增", section)
        self.assertIn("BV1、BV2", section)
        self.assertIn("- 要点A", section)
        text = render_digest("AI", section, 1, "2026-09-13", 2)
        self.assertIn("# 【汇总】AI 主题知识", text)
        self.assertIn("只增不删", text)
        self.assertIn("累计 1 轮", text)

    def test_review_verdict(self):
        self.assertEqual(review_verdict("结论：通过\n问题：无")[0], "通过")
        self.assertFalse(is_suspect_verdict("通过"))
        verdict, problems = review_verdict("结论：有问题\n问题：数字对不上")
        self.assertTrue(is_suspect_verdict(verdict))
        self.assertIn("数字对不上", problems)

    def test_review_verdict_empty_is_suspect(self):
        verdict, problems = review_verdict("")
        self.assertTrue(is_suspect_verdict(verdict))
        self.assertTrue(problems)


class ReferenceTest(unittest.TestCase):
    def test_bvid_in_text(self):
        ref = extract_video_reference("看看这个 https://www.bilibili.com/video/BV1GJ411x7h7?spm=1 不错")
        self.assertIsNotNone(ref)
        self.assertEqual(ref.kind, "bvid")
        self.assertEqual(ref.value, "BV1GJ411x7h7")

    def test_av_number(self):
        ref = extract_video_reference("av123456 挺好看")
        self.assertIsNotNone(ref)
        self.assertEqual(ref.kind, "aid")
        self.assertEqual(ref.value, "123456")

    def test_short_link(self):
        ref = extract_video_reference("https://b23.tv/abcDEF。")
        self.assertIsNotNone(ref)
        self.assertEqual(ref.kind, "url")
        self.assertEqual(ref.value, "https://b23.tv/abcDEF")

    def test_bilibili_uri(self):
        ref = extract_video_reference("bilibili://video/123456")
        self.assertIsNotNone(ref)
        self.assertEqual(ref.kind, "aid")
        self.assertEqual(ref.value, "123456")

    def test_mini_program_json(self):
        raw = '{"app":"com.tencent.miniapp","meta":{"qqdocurl":"https:\\/\\/b23.tv\\/AbCdEf"}}'
        ref = extract_video_reference(raw)
        self.assertIsNotNone(ref)
        self.assertEqual(ref.kind, "url")
        self.assertEqual(ref.value, "https://b23.tv/AbCdEf")

    def test_no_reference(self):
        self.assertIsNone(extract_video_reference("今天天气不错"))

    def test_command_remainder(self):
        url = "https://www.bilibili.com/video/BV1B7YJ6MEwc/?share_source=copy_web&vd_source=abc"
        self.assertEqual(command_remainder(f"/bilearn read {url}", "read"), url)
        self.assertEqual(command_remainder("read " + url, "read"), url)
        self.assertEqual(command_remainder("/bilearn recent 5", "recent"), "5")
        self.assertEqual(command_remainder("/bilearn recent", "recent"), "")
        self.assertEqual(command_remainder("read", "read"), "")

    def test_command_remainder_keeps_title_and_link(self):
        text = "/bilearn read 【PC】fgo街机-PVP功能测试》 https://www.bilibili.com/video/BV1B7YJ6MEwc/?a=1&b=2"
        reference = extract_video_reference(command_remainder(text, "read"))
        self.assertIsNotNone(reference)
        self.assertEqual(reference.value, "BV1B7YJ6MEwc")


class SubtitleHelperTest(unittest.TestCase):
    def test_track_rank_prefers_chinese(self):
        self.assertLess(_track_rank({"lan": "zh-Hans"}), _track_rank({"lan": "en-US"}))
        self.assertLess(_track_rank({"lan": "zh-CN"}), _track_rank({"lan": "ai-zh"}))

    def test_subtitle_url_v2_fallback(self):
        self.assertEqual(
            _subtitle_url({"lan": "ai-zh", "subtitle_url": "/", "subtitle_url_v2": "//aisubtitle.hdslb.com/x.json"}),
            "https://aisubtitle.hdslb.com/x.json",
        )
        self.assertEqual(_subtitle_url({"lan": "en", "subtitle_url": "https://evil.example/x.json"}), "")

    def test_subtitle_mismatch(self):
        self.assertTrue(subtitle_mismatch("Python 入门教程", "今天我们来讲完全无关的东西"))
        self.assertFalse(subtitle_mismatch("Python 入门教程", "大家好，这期我们聊 Python 入门"))


class ClientTest(unittest.TestCase):
    def test_valid_buvid3(self):
        self.assertTrue(valid_buvid3("4B8E0A29-1445-0B2C-9D3E-6F7A8B9C0D1Einfoc"))
        self.assertFalse(valid_buvid3(""))
        self.assertFalse(valid_buvid3("garbage"))
        self.assertFalse(valid_buvid3("4B8E0A29-1445-0B2C-9D3E-6F7A8B9C0D1E"))

    def test_buvid_fallback_when_spi_unavailable(self):
        client = BiliClient(interval=0.5)

        def fail(url, referer, method="GET"):
            raise RuntimeError("offline")

        client._fetch_sync = fail
        asyncio.run(client._ensure_buvid())
        cookies = {c.name: c.value for c in client._cookiejar}
        self.assertTrue(valid_buvid3(cookies.get("buvid3", "")))
        self.assertIn("b_nut", cookies)

    def test_buvid_uses_spi_when_available(self):
        client = BiliClient(interval=0.5)
        value = "4B8E0A29-1445-0B2C-9D3E-6F7A8B9C0D1Einfoc"

        def fake(url, referer, method="GET"):
            return 200, {"code": 0, "data": {"b_3": value, "b_4": "b4value"}}

        client._fetch_sync = fake
        asyncio.run(client._ensure_buvid())
        cookies = {c.name: c.value for c in client._cookiejar}
        self.assertEqual(cookies.get("buvid3"), value)
        self.assertEqual(cookies.get("buvid4"), "b4value")

    def test_sessdata_lives_in_cookiejar_with_buvid(self):
        client = BiliClient(sessdata="test-sessdata", interval=0.5)

        def fake(url, referer, method="GET"):
            raise RuntimeError("offline")

        client._fetch_sync = fake
        asyncio.run(client._ensure_buvid())
        cookies = {c.name: c.value for c in client._cookiejar}
        self.assertEqual(cookies.get("SESSDATA"), "test-sessdata")
        self.assertTrue(valid_buvid3(cookies.get("buvid3", "")))
        self.assertNotIn("Cookie", client._headers())

    def test_buvid_accepts_official_value(self):
        client = BiliClient(interval=0.5)
        value = "A" * 41 + "infoc"
        self.assertEqual(len(value), 46)

        def fake(url, referer, method="GET"):
            return 200, {"code": 0, "data": {"b_3": value, "b_4": "b4value"}}

        client._fetch_sync = fake
        asyncio.run(client._ensure_buvid())
        cookies = {c.name: c.value for c in client._cookiejar}
        self.assertEqual(cookies.get("buvid3"), value)

    def test_412_fails_fast_without_retry(self):
        client = BiliClient(interval=0.5)
        calls = []

        def fake(url, referer, method="GET"):
            calls.append(url)
            return 412, {}

        client._fetch_sync = fake
        with self.assertRaises(BiliRiskError):
            asyncio.run(client._raw("https://api.bilibili.com/x/test", "https://www.bilibili.com/", retries=2))
        self.assertEqual(len(calls), 1)

    def test_412_http_error_when_retry_disabled(self):
        client = BiliClient(interval=0.5)
        calls = []

        def fake(url, referer, method="GET"):
            calls.append(url)
            return 412, {}

        client._fetch_sync = fake
        with self.assertRaises(BiliHttpError):
            asyncio.run(
                client._raw(
                    "https://api.bilibili.com/x/test",
                    "https://www.bilibili.com/",
                    retries=2,
                    retry_412=False,
                )
            )
        self.assertEqual(len(calls), 1)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AuditStore(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_new_and_final_status(self):
        self.assertEqual(self.store.should_process("BV1"), (True, "new"))
        self.store.mark_ingested("BV1", title="t", doc_id="d1", summary="s")
        self.assertEqual(self.store.should_process("BV1"), (False, "ingested"))
        self.store.mark_excluded("BV2", "too_long", title="t")
        self.assertEqual(self.store.should_process("BV2"), (False, "excluded"))

    def test_failed_retry_with_backoff_and_cap(self):
        self.store.mark_retryable("BV1", "failed", "view_fail:x")
        row = self.store.get("BV1")
        self.assertEqual(row["attempts"], 1)
        self.assertGreater(row["next_retry_at"], int(time.time()))
        ok, why = self.store.should_process("BV1")
        self.assertFalse(ok)
        self.assertEqual(why, "cooldown")
        self.store.execute("UPDATE videos SET next_retry_at=0 WHERE bvid='BV1'")
        self.assertEqual(self.store.should_process("BV1"), (True, "failed"))
        for _ in range(2):
            self.store.execute("UPDATE videos SET next_retry_at=0 WHERE bvid='BV1'")
            self.store.mark_retryable("BV1", "failed", "view_fail:x")
        self.assertEqual(self.store.get("BV1")["attempts"], 3)
        self.assertEqual(self.store.should_process("BV1"), (False, "failed_given_up"))

    def test_no_subtitle_limited_retry(self):
        self.store.mark_retryable("BV1", "no_subtitle", "no_subtitle")
        self.store.execute("UPDATE videos SET next_retry_at=0 WHERE bvid='BV1'")
        self.assertEqual(self.store.should_process("BV1"), (True, "no_subtitle"))
        self.store.mark_retryable("BV1", "no_subtitle", "no_subtitle")
        self.assertEqual(self.store.should_process("BV1"), (False, "no_subtitle_given_up"))

    def test_kb_failure_always_retryable(self):
        self.store.mark_retryable("BV1", "failed", "kb_fail:unavailable")
        self.store.execute("UPDATE videos SET attempts=99, next_retry_at=0 WHERE bvid='BV1'")
        self.assertEqual(self.store.should_process("BV1"), (True, "kb_retry"))

    def test_kb_failure_respects_backoff(self):
        self.store.mark_retryable("BV1", "failed", "kb_fail:unavailable")
        row = self.store.get("BV1")
        self.assertGreater(row["next_retry_at"], int(time.time()))
        ok, why = self.store.should_process("BV1")
        self.assertFalse(ok)
        self.assertEqual(why, "cooldown")
        self.store.execute("UPDATE videos SET next_retry_at=0 WHERE bvid='BV1'")
        self.assertEqual(self.store.should_process("BV1"), (True, "kb_retry"))

    def test_summary_cache_and_counts(self):
        self.store.mark_ingested("BV1", title="t", doc_id="d1", summary="要点A")
        self.store.mark_retryable("BV2", "no_subtitle", "no_subtitle")
        counts = self.store.counts()
        self.assertEqual(counts["ingested"], 1)
        self.assertEqual(counts["no_subtitle"], 1)
        self.assertEqual(self.store.get("BV1")["summary"], "要点A")
        self.store.update_summary("BV2", "补写摘要")
        self.assertEqual(self.store.get("BV2")["summary"], "补写摘要")

    def test_daily_quota_counters(self):
        self.assertEqual(self.store.quota_used("AI"), 0)
        self.store.quota_add("AI")
        self.store.quota_add("AI")
        self.assertEqual(self.store.quota_used("AI"), 2)
        self.assertEqual(self.store.daily_counts().get("AI"), 2)
        self.assertEqual(self.store.quota_used("AI", "1970-01-01"), 0)

    def test_meta_roundtrip(self):
        self.assertEqual(self.store.get_meta("unlimited_mode_last"), "")
        self.store.set_meta("unlimited_mode_last", "1")
        self.assertEqual(self.store.get_meta("unlimited_mode_last"), "1")
        self.store.set_meta("unlimited_mode_last", "0")
        self.assertEqual(self.store.get_meta("unlimited_mode_last"), "0")

    def test_digest_store_roundtrip(self):
        self.store.save_digest("AI", doc_name="n", doc_id="d", rounds=1, sources=2, content="c")
        digest = self.store.get_digest("AI")
        self.assertEqual(digest["rounds"], 1)
        self.assertEqual(digest["sources"], 2)
        self.assertEqual(len(self.store.all_digests()), 1)
        self.store.save_digest("AI", doc_name="n", doc_id="d", rounds=2, sources=3, content="c2", error="x")
        self.assertEqual(self.store.get_digest("AI")["rounds"], 2)
        self.assertEqual(self.store.get_digest("AI")["last_error"], "x")

    def test_unmerged_and_merged(self):
        self.store.mark_ingested("BV1", title="t", category="AI", summary="s", doc_id="d1")
        self.store.mark_ingested("BV2", title="t", category="AI", summary="s", doc_id="d2")
        self.store.mark_ingested("BV3", title="t", category="科技", summary="s", doc_id="d3")
        self.assertEqual(self.store.unmerged_counts()["AI"], 2)
        self.store.mark_merged(["BV1", "BV2"], 1)
        self.assertEqual(self.store.unmerged_counts().get("AI", 0), 0)
        self.assertEqual(self.store.unmerged_counts()["科技"], 1)

    def test_audit_flow(self):
        self.store.mark_ingested("BV1", title="t", category="AI", summary="s", source_excerpt="e")
        due = self.store.audit_due_videos(7, 10)
        self.assertEqual(len(due), 1)
        self.store.mark_audit("BV1", "suspect", "编造")
        self.assertEqual(self.store.audit_stats()["suspect"], 1)
        self.assertEqual(len(self.store.recent_suspects()), 1)
        self.assertEqual(self.store.audited_since(0), 1)

    def test_brief_queries_exclude_heavy_fields(self):
        self.store.mark_ingested("BV1", title="t", summary="长摘要", source_excerpt="素材")
        brief = self.store.recent_brief(5)[0]
        self.assertNotIn("summary", brief)
        self.assertNotIn("source_excerpt", brief)
        self.assertIn("audit_status", brief)
        self.store.save_digest("AI", doc_name="n", rounds=1, content="很长的内容")
        digest_brief = self.store.all_digest_briefs()[0]
        self.assertNotIn("content", digest_brief)
        self.assertEqual(digest_brief["rounds"], 1)

    def test_search_local_finds_videos_and_digests(self):
        self.store.mark_ingested(
            "BV1",
            title="构图入门",
            category="构图",
            doc_name="构图｜构图入门.md",
            summary="三分法是最基础的构图技巧。",
        )
        self.store.mark_excluded("BV2", "excluded", title="别的", summary="不相关")
        self.store.save_digest(
            "AI",
            doc_name="【汇总】AI｜主题知识.md",
            rounds=1,
            sources=2,
            content="提示词要写清角色和输出格式。",
        )
        hits = self.store.search_local("构图")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "video")
        self.assertEqual(hits[0]["bvid"], "BV1")
        digest_hits = self.store.search_local("提示词")
        self.assertEqual(len(digest_hits), 1)
        self.assertEqual(digest_hits[0]["kind"], "digest")
        self.assertEqual(self.store.search_local("不存在的词"), [])

    def test_search_local_splits_tokens_and_escapes_wildcards(self):
        self.store.mark_ingested("BV1", title="AI 提示词", summary="写提示词的经验")
        self.store.mark_ingested("BV2", title="折扣", summary="全场100%好评")
        self.assertTrue(self.store.search_local("AI 提示词"))
        self.assertEqual([h["bvid"] for h in self.store.search_local("%")], ["BV2"])
        self.assertEqual([h["bvid"] for h in self.store.search_local("100%")], ["BV2"])
        self.assertEqual(self.store.search_local("100_"), [])
        self.assertEqual(self.store.search_local(""), [])

    def test_category_counts_and_recent_ingested(self):
        self.store.mark_ingested("BV1", title="a", category="AI", summary="s")
        self.store.mark_ingested("BV2", title="b", category="AI", summary="s")
        self.store.mark_ingested("BV3", title="c", category="科技", summary="s")
        counts = self.store.category_counts()
        self.assertEqual(counts[0], {"category": "AI", "count": 2})
        recent = self.store.recent_ingested(2)
        self.assertEqual(len(recent), 2)
        self.assertIn("title", recent[0])


class MigrationTest(unittest.TestCase):
    def test_legacy_rows_are_normalized(self):
        tmp = tempfile.TemporaryDirectory()
        db_path = Path(tmp.name) / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(LEGACY_SCHEMA)
        now = int(time.time())
        rows = [
            ("BVold1", "skipped", "no_subtitle", now - 10),
            ("BVold2", "skipped", "llm_fail:timeout", now - 10),
            ("BVold3", "skipped", "excluded", now - 10),
            ("BVold4", "ingested", "ok", now - 10),
        ]
        for bvid, status, reason, ts in rows:
            conn.execute(
                "INSERT INTO videos(bvid,title,author,status,reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (bvid, "t", "a", status, reason, ts, ts),
            )
        conn.commit()
        conn.close()
        store = AuditStore(db_path)
        self.assertEqual(store.get("BVold1")["status"], "no_subtitle")
        self.assertEqual(store.get("BVold2")["status"], "failed")
        self.assertEqual(store.get("BVold3")["status"], "excluded")
        self.assertEqual(store.get("BVold4")["status"], "ingested")
        self.assertFalse(store.should_process("BVold1")[0])
        self.assertFalse(store.should_process("BVold2")[0])
        self.assertFalse(store.should_process("BVold3")[0])
        self.assertFalse(store.should_process("BVold4")[0])
        store.close()
        tmp.cleanup()


class PipelineNamingTest(unittest.TestCase):
    class FakeClient:
        sessdata = ""

        async def view(self, bvid="", aid=None):
            return {
                "bvid": bvid,
                "aid": 1,
                "cid": 2,
                "pages": [{"cid": 2, "page": 1, "part": ""}],
                "title": "Herdr 多Agent工具介绍",
                "desc": "AI 终端管理",
                "tname": "科技",
                "author": "某UP",
                "duration": 300,
                "url": f"https://www.bilibili.com/video/{bvid}",
            }

        async def subtitles_for_pages(self, meta, page_limit=3, verify=True):
            return "字幕内容"

        async def recommend(self):
            return []

        async def search_videos(self, keyword, page=1):
            return []

        async def resolve_bvid(self, reference):
            return "BV1test00001"

    def _make(self, find_doc, llm_output="标题：Herdr 多Agent工具\n分区：AI\n\n- 要点一\n- 要点二"):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AuditStore(Path(self.tmp.name) / "t.db")
        self.uploaded = []

        async def llm(prompt):
            return llm_output

        async def upload(name, text, ftype):
            self.uploaded.append((name, text))
            return "doc-new"

        pipe = LearnPipeline(
            self.FakeClient(),
            self.store,
            llm,
            upload,
            {"keywords": "AI,科技", "max_duration_minutes": 0},
            find_doc=find_doc,
        )
        return pipe

    def tearDown(self):
        if getattr(self, "store", None):
            self.store.close()
        if getattr(self, "tmp", None):
            self.tmp.cleanup()

    def test_upload_uses_category_and_summary_title(self):
        pipe = self._make(None)
        result = asyncio.run(pipe.read_one("BV1test00001"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["doc_name"], "AI｜Herdr 多Agent工具.md")
        self.assertEqual(result["category"], "AI")
        self.assertEqual(len(self.uploaded), 1)
        name, doc = self.uploaded[0]
        self.assertEqual(name, "AI｜Herdr 多Agent工具.md")
        self.assertIn("- 归类: AI", doc)
        self.assertIn("- 概括: Herdr 多Agent工具", doc)
        self.assertNotIn("标题：", result["summary"])

    def test_existing_kb_doc_skips_upload(self):
        async def find_doc(name):
            return "existing-doc" if name == "AI｜Herdr 多Agent工具.md" else None

        pipe = self._make(find_doc)
        result = asyncio.run(pipe.read_one("BV1test00001"))
        self.assertTrue(result["ok"])
        self.assertTrue(result["ingested"])
        self.assertEqual(result["doc_id"], "existing-doc")
        self.assertEqual(self.uploaded, [])
        self.assertEqual(self.store.get("BV1test00001")["status"], "ingested")

    def test_second_read_is_cached(self):
        pipe = self._make(None)
        asyncio.run(pipe.read_one("BV1test00001"))
        result = asyncio.run(pipe.read_one("BV1test00001"))
        self.assertTrue(result["cached"])
        self.assertEqual(len(self.uploaded), 1)

    def test_on_demand_low_score_not_ingested(self):
        pipe = self._make(None, "标题：游戏测试\n分区：AI\n相关度：30\n\n- 要点")
        result = asyncio.run(pipe.read_one("BV1test00001"))
        self.assertTrue(result["ok"])
        self.assertFalse(result["ingested"])
        self.assertTrue(result["off_topic"])
        self.assertEqual(result["doc_name"], "")
        self.assertEqual(self.uploaded, [])
        row = self.store.get("BV1test00001")
        self.assertEqual(row["status"], "excluded")
        self.assertEqual(row["reason"], "off_topic")
        self.assertTrue(row["summary"])

    def test_on_demand_high_score_is_ingested(self):
        pipe = self._make(None, "标题：AI工具\n分区：AI\n相关度：92\n\n- 要点")
        result = asyncio.run(pipe.read_one("BV1test00001"))
        self.assertTrue(result["ingested"])
        self.assertEqual(len(self.uploaded), 1)

    def test_on_demand_does_not_consume_quota(self):
        pipe = self._make(None, "标题：AI工具\n分区：AI\n相关度：92\n\n- 要点")
        result = asyncio.run(pipe.read_one("BV1test00001"))
        self.assertTrue(result["ingested"])
        self.assertEqual(self.store.quota_used("AI"), 0)


class InterestPlanTest(unittest.TestCase):
    def _pipe(self, config):
        return LearnPipeline(None, None, None, None, config)

    def test_plan_from_template_list(self):
        pipe = self._pipe({
            "interest_quotas": [
                {"__template_key": "interest", "keyword": "AI", "quota": 5},
                {"keyword": "科技", "quota": "2"},
            ]
        })
        self.assertEqual(pipe.interest_plan(), [("AI", 5), ("科技", 2)])
        self.assertEqual(pipe.keywords(), ["AI", "科技"])

    def test_plan_fallback_to_keywords(self):
        pipe = self._pipe({"keywords": "AI,科技", "daily_per_keyword": 4})
        self.assertEqual(pipe.interest_plan(), [("AI", 4), ("科技", 4)])

    def test_plan_dedupes_and_cleans(self):
        pipe = self._pipe({
            "interest_quotas": [
                {"keyword": "AI", "quota": 2},
                {"keyword": " AI ", "quota": 9},
                {"keyword": "", "quota": 9},
                {"keyword": "科技", "quota": -3},
            ]
        })
        self.assertEqual(pipe.interest_plan(), [("AI", 2), ("科技", 0)])

    def test_empty_template_list_falls_back(self):
        pipe = self._pipe({"interest_quotas": [], "keywords": "AI", "daily_per_keyword": 2})
        self.assertEqual(pipe.interest_plan(), [("AI", 2)])

    def test_interest_configured_flag(self):
        self.assertFalse(self._pipe({"interest_quotas": []}).interest_configured())
        self.assertFalse(self._pipe({"interest_quotas": [{"keyword": ""}]}).interest_configured())
        self.assertTrue(
            self._pipe({"interest_quotas": [{"keyword": "AI", "quota": 1}]}).interest_configured()
        )


class PipelineModeTest(unittest.TestCase):
    class FakeClient:
        sessdata = ""

        def __init__(self, candidates):
            self.candidates = candidates

        async def view(self, bvid="", aid=None):
            return {
                "bvid": bvid,
                "aid": 1,
                "cid": 2,
                "pages": [{"cid": 2, "page": 1, "part": ""}],
                "title": f"视频{bvid}",
                "desc": "AI 内容",
                "tname": "科技",
                "author": "某UP",
                "duration": 300,
                "url": f"https://www.bilibili.com/video/{bvid}",
            }

        async def subtitles_for_pages(self, meta, page_limit=3, verify=True):
            return "字幕内容"

        async def recommend(self):
            return []

        async def search_videos(self, keyword, page=1):
            return [dict(item, keyword=keyword) for item in self.candidates]

        async def resolve_bvid(self, reference):
            return reference

    def _make(self, llm_output, config):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AuditStore(Path(self.tmp.name) / "t.db")
        self.uploaded = []
        self.llm_calls = 0

        async def llm(prompt):
            self.llm_calls += 1
            return llm_output

        async def upload(name, text, ftype):
            self.uploaded.append(name)
            return f"doc-{len(self.uploaded)}"

        client = self.FakeClient([{"bvid": "BV1a"}, {"bvid": "BV2b"}, {"bvid": "BV3c"}])
        return LearnPipeline(client, self.store, llm, upload, config)

    def tearDown(self):
        if getattr(self, "store", None):
            self.store.close()
        if getattr(self, "tmp", None):
            self.tmp.cleanup()

    def _config(self, **overrides):
        config = {
            "keywords": "AI",
            "daily_per_keyword": 1,
            "unlimited_mode": False,
            "missing_subtitle": "desc_only",
            "max_duration_minutes": 0,
        }
        config.update(overrides)
        return config

    def test_daily_quota_stops_after_limit(self):
        pipe = self._make("标题：测试视频\n分区：AI\n\n- 要点", self._config())
        first = asyncio.run(pipe.run(trigger="manual"))
        self.assertEqual(first["ingested"], 1)
        self.assertEqual(len(self.uploaded), 1)
        self.assertEqual(self.store.quota_used("AI"), 1)
        second = asyncio.run(pipe.run(trigger="manual"))
        self.assertEqual(second["ingested"], 0)
        self.assertEqual(len(self.uploaded), 1)

    def test_unlimited_mode_runs_rounds_until_no_new(self):
        pipe = self._make(
            "标题：测试视频\n分区：AI\n\n- 要点",
            self._config(
                unlimited_mode=True,
                interest_quotas=[{"keyword": "AI", "quota": 1}],
            ),
        )
        result = asyncio.run(pipe.run(trigger="manual"))
        self.assertEqual(result["ingested"], 3)
        self.assertGreaterEqual(result["rounds"], 3)

    def test_quota_mode_loops_until_daily_exhausted(self):
        pipe = self._make(
            "标题：测试视频\n分区：AI\n\n- 要点",
            self._config(
                daily_per_keyword=2,
                interest_quotas=[{"keyword": "AI", "quota": 1}],
            ),
        )
        result = asyncio.run(pipe.run(trigger="manual"))
        self.assertEqual(result["ingested"], 2)
        self.assertEqual(result["rounds"], 2)
        self.assertEqual(self.store.quota_used("AI"), 2)
        messages = [e["message"] for e in self.store.run_events(result["run_id"], 500)]
        self.assertIn("每日配额已刷完，结束", messages)

    def test_run_max_videos_caps_unlimited(self):
        pipe = self._make(
            "标题：测试视频\n分区：AI\n\n- 要点",
            self._config(
                unlimited_mode=True,
                interest_quotas=[{"keyword": "AI", "quota": 5}],
                run_max_videos=1,
            ),
        )
        result = asyncio.run(pipe.run(trigger="manual"))
        self.assertEqual(result["ingested"], 1)
        self.assertEqual(result["aborted"], "total_limit")

    def test_sequential_keyword_order(self):
        pipe = self._make(
            "标题：测试视频\n分区：AI\n\n- 要点",
            self._config(
                daily_per_keyword=1,
                interest_quotas=[
                    {"keyword": "AI", "quota": 1},
                    {"keyword": "科技", "quota": 1},
                ],
            ),
        )
        result = asyncio.run(pipe.run(trigger="manual"))
        messages = [e["message"] for e in self.store.run_events(result["run_id"], 500)]
        ai_index = next(i for i, message in enumerate(messages) if "「AI」目标" in message)
        tech_index = next(i for i, message in enumerate(messages) if "「科技」目标" in message)
        self.assertLess(ai_index, tech_index)
        self.assertIn("「AI」已达标 1/1", messages)
        self.assertEqual(result["ingested"], 1)

    def test_off_topic_is_excluded_without_upload(self):
        pipe = self._make("标题：别的\n分区：其他\n\n- 要点", self._config(daily_per_keyword=5))
        result = asyncio.run(pipe.run(trigger="manual"))
        self.assertEqual(result["ingested"], 0)
        self.assertEqual(self.uploaded, [])
        row = self.store.get("BV1a")
        self.assertEqual(row["status"], "excluded")
        self.assertEqual(row["reason"], "off_topic")

    def test_missing_declared_category_falls_back_to_keyword(self):
        pipe = self._make(
            "这是没有分区的正文",
            self._config(interest_quotas=[{"keyword": "AI", "quota": 1}]),
        )
        result = asyncio.run(pipe.run(trigger="manual"))
        self.assertEqual(result["ingested"], 1)

    def test_low_relevance_score_is_off_topic(self):
        pipe = self._make(
            "标题：游戏测试\n分区：AI\n相关度：40\n\n- 要点",
            self._config(daily_per_keyword=5),
        )
        result = asyncio.run(pipe.run(trigger="manual"))
        self.assertEqual(result["ingested"], 0)
        self.assertEqual(self.uploaded, [])
        self.assertEqual(self.store.get("BV1a")["reason"], "off_topic")

    def test_high_relevance_score_is_ingested(self):
        pipe = self._make(
            "标题：AI工具\n分区：AI\n相关度：95\n\n- 要点",
            self._config(interest_quotas=[{"keyword": "AI", "quota": 1}]),
        )
        result = asyncio.run(pipe.run(trigger="manual"))
        self.assertEqual(result["ingested"], 1)

    def test_category_quota_defers_then_reuses_summary(self):
        pipe = self._make(
            "标题：AI工具\n分区：AI\n相关度：95\n\n- 要点",
            self._config(
                keywords="科技,AI",
                daily_per_keyword=1,
                daily_quota_overrides="AI:1",
                interest_quotas=[
                    {"keyword": "科技", "quota": 1},
                    {"keyword": "AI", "quota": 1},
                ],
            ),
        )
        self.store.quota_add("AI")
        first = asyncio.run(pipe.run(trigger="manual"))
        self.assertEqual(first["ingested"], 0)
        deferred = self.store.get("BV1a")
        self.assertEqual(deferred["status"], "deferred")
        self.assertTrue(deferred["summary"])
        self.assertTrue(deferred["doc_name"])
        self.assertGreater(deferred["next_retry_at"], int(time.time()))
        calls_after_first = self.llm_calls

        self.store.reset_daily()
        self.store.execute("UPDATE videos SET next_retry_at=0 WHERE bvid='BV1a'")
        result = asyncio.run(pipe.process_one({"bvid": "BV1a", "keyword": "科技"}))
        self.assertEqual(result["status"], "ingested")
        self.assertEqual(self.llm_calls, calls_after_first)

    def test_read_one_reuses_deferred_summary(self):
        pipe = self._make(
            "标题：AI工具\n分区：AI\n相关度：95\n\n- 要点",
            self._config(
                keywords="科技,AI",
                daily_per_keyword=1,
                daily_quota_overrides="AI:1",
                interest_quotas=[
                    {"keyword": "科技", "quota": 1},
                    {"keyword": "AI", "quota": 1},
                ],
            ),
        )
        self.store.quota_add("AI")
        asyncio.run(pipe.run(trigger="manual"))
        self.assertEqual(self.store.get("BV1a")["status"], "deferred")
        calls_before = self.llm_calls

        self.store.reset_daily()
        self.store.execute("UPDATE videos SET next_retry_at=0 WHERE bvid='BV1a'")
        result = asyncio.run(pipe.read_one("BV1a"))
        self.assertTrue(result["ok"])
        self.assertTrue(result["ingested"])
        self.assertEqual(self.llm_calls, calls_before)

    def test_search_risk_marks_rate_limited(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AuditStore(Path(self.tmp.name) / "t.db")
        self.uploaded = []
        self.llm_calls = 0

        async def llm(prompt):
            self.llm_calls += 1
            return "标题：x\n分区：AI\n\n- 要点"

        async def upload(name, text, ftype):
            self.uploaded.append(name)
            return "doc"

        class RiskSearchClient(self.FakeClient):
            async def search_videos(self, keyword, page=1):
                raise BiliRiskError("bilibili http 412")

        pipe = LearnPipeline(
            RiskSearchClient([{"bvid": "BV1a"}]),
            self.store,
            llm,
            upload,
            self._config(),
        )
        result = asyncio.run(pipe.run(trigger="manual"))
        self.assertEqual(result["aborted"], "rate_limited")
        self.assertEqual(self.store.recent_runs(1)[0]["status"], "rate_limited")


class PipelineDigestTest(unittest.TestCase):
    def _pipe(self, responses, config=None, utility_responses=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AuditStore(Path(self.tmp.name) / "t.db")
        self.uploads = []
        self.deleted = []
        self.responses = list(responses)
        self.utility_responses = list(utility_responses or [])

        async def llm(prompt):
            return self.responses.pop(0) if self.responses else ""

        async def utility_llm(prompt):
            return self.utility_responses.pop(0) if self.utility_responses else ""

        async def upload(name, text, ftype):
            self.uploads.append((name, text))
            return f"doc-{len(self.uploads)}"

        async def delete(doc_id):
            self.deleted.append(doc_id)

        cfg = {
            "keywords": "AI",
            "consolidate_threshold": 2,
            "consolidate_delete_sources": True,
        }
        cfg.update(config or {})
        return LearnPipeline(
            None,
            self.store,
            llm,
            upload,
            cfg,
            delete_doc=delete,
            utility_llm=utility_llm if utility_responses else None,
        )

    def tearDown(self):
        if getattr(self, "store", None):
            self.store.close()
        if getattr(self, "tmp", None):
            self.tmp.cleanup()

    def _seed(self, bvid, summary, doc_id):
        self.store.mark_ingested(
            bvid,
            title=f"视频{bvid}",
            category="AI",
            summary=summary,
            doc_name=f"AI｜{bvid}.md",
            doc_id=doc_id,
            source_excerpt="素材",
        )

    def test_first_round_writes_digest_and_deletes_sources(self):
        pipe = self._pipe(["- 新知识一\n- 新知识二", "结论：通过\n问题：无"])
        self._seed("BV1", "摘要一", "d1")
        self._seed("BV2", "摘要二", "d2")
        result = asyncio.run(pipe.consolidate(force=True))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["round"], 1)
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(self.uploads[0][0], "【汇总】AI｜主题知识.md")
        self.assertIn("只增不删", self.uploads[0][1])
        digest = self.store.get_digest("AI")
        self.assertEqual(digest["rounds"], 1)
        self.assertIn("第 1 轮新增", digest["content"])
        self.assertIn("新知识一", digest["content"])
        self.assertEqual(self.store.get("BV1")["merged_round"], 1)
        self.assertEqual(self.store.get("BV2")["merged_round"], 1)
        self.assertEqual(self.deleted, ["d1", "d2"])

    def test_second_round_keeps_old_content_verbatim(self):
        pipe = self._pipe([
            "- 第一轮知识",
            "结论：通过\n问题：无",
            "- 第二轮知识",
            "结论：通过\n问题：无",
        ])
        self._seed("BV1", "摘要一", "d1")
        self._seed("BV2", "摘要二", "d2")
        asyncio.run(pipe.consolidate(force=True))
        first_content = self.store.get_digest("AI")["content"]
        self._seed("BV3", "摘要三", "d3")
        result = asyncio.run(pipe.consolidate(force=True))
        self.assertEqual(result["round"], 2)
        content = self.store.get_digest("AI")["content"]
        self.assertIn(first_content, content)
        self.assertIn("第二轮知识", content)
        self.assertEqual(self.deleted, ["d1", "d2", "doc-1", "d3"])

    def test_review_failure_blocks_write(self):
        pipe = self._pipe(["- 可疑知识", "结论：有问题\n问题：来源里没有这个数字"])
        self._seed("BV1", "摘要一", "d1")
        self._seed("BV2", "摘要二", "d2")
        result = asyncio.run(pipe.consolidate(force=True))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "review_failed")
        self.assertEqual(self.uploads, [])
        self.assertEqual(self.deleted, [])
        self.assertTrue(self.store.get_digest("AI")["last_error"])
        self.assertEqual(self.store.get("BV1")["merged_round"], 0)

    def test_review_failure_has_cooldown(self):
        pipe = self._pipe(["- 可疑知识", "结论：有问题\n问题：数字对不上"])
        self._seed("BV1", "摘要一", "d1")
        self._seed("BV2", "摘要二", "d2")
        asyncio.run(pipe.consolidate(force=True))
        result = asyncio.run(pipe.consolidate(force=False))
        self.assertEqual(result["reason"], "cooldown")

    def test_below_threshold_has_no_round(self):
        pipe = self._pipe([])
        self._seed("BV1", "摘要一", "d1")
        result = asyncio.run(pipe.consolidate(force=False))
        self.assertEqual(result["reason"], "below_threshold")
        self.assertNotIn("round", result)

    def test_shared_doc_id_is_not_deleted(self):
        pipe = self._pipe(["- 新知识", "结论：通过\n问题：无"])
        self._seed("BV1", "摘要一", "shared")
        self._seed("BV2", "摘要二", "shared")
        result = asyncio.run(pipe.consolidate(force=True))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(self.store.doc_id_ref_count("shared"), 2)

    def test_auto_skips_cooling_keyword(self):
        pipe = self._pipe(
            [
                "- 可疑知识",
                "结论：有问题\n问题：数字对不上",
                "- 科技新知识",
                "结论：通过\n问题：无",
            ],
            config={"keywords": "AI,科技"},
        )
        self._seed("BV1", "摘要一", "d1")
        self._seed("BV2", "摘要二", "d2")
        self.store.mark_ingested("BV3", title="t3", category="科技", summary="s3", doc_id="d3")
        self.store.mark_ingested("BV4", title="t4", category="科技", summary="s4", doc_id="d4")
        first = asyncio.run(pipe.consolidate(keyword="AI", force=True))
        self.assertEqual(first["reason"], "review_failed")
        second = asyncio.run(pipe.consolidate(force=False))
        self.assertTrue(second.get("round"), second)
        self.assertEqual(second["keyword"], "科技")

    def test_digest_review_uses_utility_model(self):
        pipe = self._pipe(
            ["- 新知识一"],
            utility_responses=["结论：通过\n问题：无"],
        )
        self._seed("BV1", "摘要一", "d1")
        self._seed("BV2", "摘要二", "d2")
        result = asyncio.run(pipe.consolidate(force=True))
        self.assertTrue(result["ok"], result)


class PipelineAuditTest(unittest.TestCase):
    def _pipe(self, responses):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AuditStore(Path(self.tmp.name) / "t.db")
        self.responses = list(responses)
        self.prompts: list[str] = []

        async def llm(prompt):
            self.prompts.append(prompt)
            return self.responses.pop(0) if self.responses else ""

        return LearnPipeline(None, self.store, llm, None, {"keywords": "AI"})

    def tearDown(self):
        if getattr(self, "store", None):
            self.store.close()
        if getattr(self, "tmp", None):
            self.tmp.cleanup()

    def test_audit_marks_suspect_and_ok(self):
        pipe = self._pipe(["结论：可疑\n问题：编造了数字", "结论：通过\n问题：无"])
        self.store.mark_ingested("BV1", title="t1", category="AI", summary="摘要1", source_excerpt="素材1")
        self.store.mark_ingested("BV2", title="t2", category="AI", summary="摘要2", source_excerpt="素材2")
        result = asyncio.run(pipe.audit_due_docs(force=True))
        self.assertEqual(result["audited"], 2)
        self.assertEqual(result["suspect"], 1)
        self.assertEqual(self.store.get("BV1")["audit_status"], "suspect")
        self.assertEqual(self.store.get("BV2")["audit_status"], "ok")

    def test_digest_audit(self):
        pipe = self._pipe(["结论：通过\n问题：无"])
        self.store.save_digest(
            "AI",
            doc_name="【汇总】AI｜主题知识.md",
            doc_id="dg1",
            rounds=1,
            sources=1,
            content="## 第 1 轮新增\n- 知识",
        )
        result = asyncio.run(pipe.audit_due_docs(force=True))
        self.assertEqual(result["audited"], 1)
        self.assertEqual(self.store.get_digest("AI")["audit_status"], "ok")

    def test_audit_excerpt_is_capped(self):
        pipe = self._pipe(["结论：通过\n问题：无"])
        pipe.config["audit_excerpt_chars"] = 1000
        self.store.mark_ingested(
            "BV1",
            title="t",
            category="AI",
            summary="摘要",
            source_excerpt="x" * 5000,
        )
        asyncio.run(pipe.audit_due_docs(force=True))
        prompt = self.prompts[0]
        self.assertNotIn("x" * 1200, prompt)
        self.assertIn("x" * 900, prompt)


class KnowledgeQueryTest(unittest.TestCase):
    def test_unwrap_arguments_layers(self):
        self.assertEqual(
            unwrap_arguments({"arguments": {"query": "构图"}}), {"query": "构图"}
        )
        self.assertEqual(
            unwrap_arguments({"arguments": {"arguments": {"query": "构图"}}}),
            {"query": "构图"},
        )
        self.assertEqual(
            unwrap_arguments({"arguments": '{"query": "构图"}'}), {"query": "构图"}
        )
        self.assertEqual(unwrap_arguments({"query": "构图"}), {"query": "构图"})
        self.assertEqual(unwrap_arguments({"arguments": "not json"})["arguments"], "not json")
        self.assertEqual(unwrap_arguments(None), {})

    def test_llm_search_text_has_doc_and_note(self):
        result = {
            "ok": True,
            "mode": "search",
            "source": "kb",
            "query": "构图",
            "items": [
                {
                    "kind": "kb",
                    "doc_name": "构图｜构图入门.md",
                    "score": 0.834,
                    "content": "三分法是最基础的构图技巧。",
                }
            ],
        }
        text = format_for_llm(result)
        self.assertIn("构图｜构图入门.md", text)
        self.assertIn("0.83", text)
        self.assertIn("三分法", text)
        self.assertIn("不是亲历", text)

    def test_llm_local_text_has_url(self):
        result = {
            "ok": True,
            "mode": "search",
            "source": "local",
            "query": "构图",
            "items": [
                {
                    "kind": "video",
                    "bvid": "BV1xx",
                    "title": "构图入门",
                    "status": "ingested",
                    "summary": "本地摘要",
                }
            ],
        }
        text = format_for_llm(result)
        self.assertIn("https://www.bilibili.com/video/BV1xx", text)
        self.assertIn("本地摘要库", text)

    def test_llm_search_total_is_capped(self):
        item = {"kind": "kb", "doc_name": "d", "score": 1.0, "content": "x" * 900}
        result = {"ok": True, "mode": "search", "source": "kb", "query": "q", "items": [item] * 20}
        text = format_for_llm(result)
        self.assertLessEqual(text.count("【素材"), 8)
        self.assertIn("已省略", text)

    def test_llm_doc_truncation_note(self):
        result = {"ok": True, "mode": "doc", "doc_name": "d.md", "content": "y" * 9000}
        text = format_for_llm(result)
        self.assertIn("【文档全文】d.md", text)
        self.assertIn("文档过长", text)
        self.assertNotIn("y" * 6100, text)

    def test_llm_empty_and_error(self):
        self.assertIn("没有", format_for_llm({"ok": True, "mode": "empty", "query": "q"}))
        self.assertIn(
            "没有找到文档", format_for_llm({"ok": False, "mode": "doc", "note": "没有找到文档「x」"})
        )
        self.assertIn("没有返回可用结果", format_for_llm(None))

    def test_chat_search_and_overview(self):
        chat = format_for_chat(
            {
                "ok": True,
                "mode": "search",
                "source": "kb",
                "query": "构图",
                "items": [
                    {
                        "kind": "kb",
                        "doc_name": "构图｜构图入门.md",
                        "score": 0.83,
                        "content": "三分法。",
                    }
                ],
            }
        )
        self.assertIn("命中 1 条", chat)
        self.assertIn("构图｜构图入门.md", chat)
        overview = format_for_chat(
            {
                "ok": True,
                "mode": "overview",
                "counts": {"ingested": 12, "excluded": 3, "no_subtitle": 1, "failed": 0, "digests": 2},
                "categories": [{"category": "AI", "count": 8}],
                "digests": [{"keyword": "AI", "rounds": 2, "sources": 10}],
                "recent": [{"bvid": "BV1", "title": "示例"}],
            }
        )
        self.assertIn("已入库视频 12 条", overview)
        self.assertIn("AI 8", overview)
        self.assertIn("汇总主题", overview)


class ConfigRobustnessTest(unittest.TestCase):
    """脏配置绝不能崩任务：非法值回默认值，显式 0 保持原有语义。"""

    def _pipe(self, **config):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = AuditStore(Path(tmp.name) / "t.db")
        self.addCleanup(store.close)
        return LearnPipeline(None, store, None, None, config)

    def test_as_int(self):
        self.assertEqual(_as_int("abc", 3), 3)
        self.assertEqual(_as_int(None, 3), 3)
        self.assertEqual(_as_int("5", 3), 5)
        self.assertEqual(_as_int(7, 3), 7)

    def test_daily_quota_default_matches_readme(self):
        pipe = self._pipe()
        self.assertEqual(pipe.daily_quota("AI"), 3)
        self.assertEqual(self._pipe(daily_per_keyword="bad").daily_quota("AI"), 3)
        self.assertEqual(self._pipe(daily_per_keyword=0).daily_quota("AI"), 0)

    def test_subtitle_page_limit_zero_means_all(self):
        self.assertEqual(self._pipe().subtitle_page_limit(), 3)
        self.assertEqual(self._pipe(subtitle_page_limit=0).subtitle_page_limit(), 0)
        self.assertEqual(self._pipe(subtitle_page_limit="bad").subtitle_page_limit(), 3)

    def test_fullwidth_separators(self):
        pipe = self._pipe(keywords="科技，数码、AI；提示词 构图\n审美")
        self.assertEqual(
            pipe.keywords(), ["科技", "数码", "AI", "提示词", "构图", "审美"]
        )
        self.assertEqual(
            self._pipe(exclude_keywords="广告，推广").exclude(), ["广告", "推广"]
        )
        pipe = self._pipe(daily_per_keyword=5, daily_quota_overrides="AI：9，科技:2")
        self.assertEqual(pipe.daily_quota("AI"), 9)
        self.assertEqual(pipe.daily_quota("科技"), 2)

    def test_garbage_numbers_fall_back(self):
        pipe = self._pipe(
            consolidate_threshold="x", audit_interval_days="x",
            audit_daily_limit="x", audit_excerpt_chars="x", run_max_videos="x",
            max_duration_minutes="x",
        )
        self.assertEqual(pipe.consolidate_threshold(), 10)
        self.assertEqual(pipe.audit_interval_days(), 7)
        self.assertEqual(pipe.audit_daily_limit(), 20)
        self.assertEqual(pipe.audit_excerpt_chars(), 4000)
        self.assertEqual(pipe.run_max_videos(), 100)
        self.assertEqual(pipe.max_duration(), 0)

    def test_throttle_garbage_interval(self):
        self.assertEqual(BiliThrottle("bad").min_gap, 3.0)
        self.assertEqual(BiliThrottle(None).min_gap, 3.0)
        self.assertEqual(BiliThrottle(0).min_gap, 0.5)


class LLMStrategyTest(unittest.TestCase):
    """任务分档 / Provider 回退链 / Token 预算闸 / 拒答识别。"""

    def test_resolve_provider_precedence(self):
        from bili.llm import resolve_provider

        config = {
            "summary_provider_id": "p-summary",
            "utility_provider_id": "p-utility",
            "fast_provider_id": "p-fast",
            "quality_provider_id": "p-quality",
        }
        self.assertEqual(resolve_provider("summary", config), ("p-summary", "explicit:summary_provider_id"))
        self.assertEqual(resolve_provider("utility", config), ("p-utility", "explicit:utility_provider_id"))
        # 清掉显式配置后走档位
        config["summary_provider_id"] = ""
        config["utility_provider_id"] = ""
        self.assertEqual(resolve_provider("summary", config), ("p-quality", "tier:quality"))
        self.assertEqual(resolve_provider("utility", config), ("p-fast", "tier:fast"))
        # 档位也空 → 跟随默认
        config["fast_provider_id"] = ""
        config["quality_provider_id"] = ""
        self.assertEqual(resolve_provider("summary", config), ("", "default"))
        self.assertEqual(resolve_provider("utility", config), ("", "default"))

    def test_utility_falls_back_to_summary(self):
        from bili.llm import resolve_provider

        self.assertEqual(
            resolve_provider("utility", {"summary_provider_id": "p-summary"}),
            ("p-summary", "explicit:summary_provider_id"),
        )

    def test_estimate_tokens(self):
        from bili.llm import estimate_tokens

        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("中文四个字"), 5)
        self.assertGreater(estimate_tokens("hello world"), 0)
        long_cn = estimate_tokens("中" * 1000)
        self.assertGreater(long_cn, 900)

    def test_looks_refusal(self):
        from bili.llm import looks_refusal

        self.assertTrue(looks_refusal("抱歉，我无法提供该内容。"))
        self.assertTrue(looks_refusal("作为一个AI，我不能协助这个请求"))
        self.assertTrue(looks_refusal("I cannot help with that."))
        self.assertFalse(looks_refusal("标题：测试视频摘要\n分区：科技\n相关度：90"))

    def test_budget_hard_and_soft(self):
        from bili.llm import BudgetGuard

        used = {"n": 0}
        guard = BudgetGuard(
            {"daily_token_limit": 100, "soft_token_limit": 50},
            lambda: used["n"],
        )
        self.assertTrue(guard.check("summary", "短提示词").allowed)
        used["n"] = 60
        self.assertTrue(guard.check("summary", "短提示词").allowed)
        blocked = guard.check("utility", "短提示词")
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "soft_token_limit")
        used["n"] = 100
        hard = guard.check("summary", "短提示词")
        self.assertFalse(hard.allowed)
        self.assertEqual(hard.reason, "daily_token_limit")

    def test_budget_single_call_cap(self):
        from bili.llm import BudgetGuard

        guard = BudgetGuard(
            {
                "single_call_token_cap": 10,
                "fallback_provider_id": "p-backup",
                "daily_token_limit": 0,
                "soft_token_limit": 0,
            },
            lambda: 0,
        )
        decision = guard.check("summary", "这是一段很长的提示词" * 20)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.provider_override, "p-backup")
        # 没配备用模型时不阻断，只是记录原因
        guard2 = BudgetGuard({"single_call_token_cap": 10}, lambda: 0)
        decision2 = guard2.check("summary", "这是一段很长的提示词" * 20)
        self.assertTrue(decision2.allowed)
        self.assertEqual(decision2.reason, "single_call_cap_no_fallback")

    def test_store_llm_usage_ledger(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = AuditStore(Path(tmp.name) / "usage.db")
        self.addCleanup(store.close)
        store.add_llm_usage("summary", "p1", 100, 50, source="tier:quality")
        store.add_llm_usage("utility", "p2", 20, 10, source="tier:fast")
        store.add_llm_usage("utility", "", 0, 0, ok=False, reason="daily_token_limit")
        self.assertEqual(store.llm_tokens_today(), 180)
        rows = store.llm_usage_today_by_task()
        self.assertEqual(rows[0]["task"], "summary")
        self.assertEqual(rows[0]["tokens"], 150)
        self.assertEqual({r["task"] for r in rows}, {"summary", "utility"})
        skips = store.llm_skips_today()
        self.assertEqual(skips[0]["reason"], "daily_token_limit")


class PipelineLockTest(unittest.TestCase):
    def test_bvid_lock_eviction(self):
        pipe = LearnPipeline(None, None, None, None, {})
        for i in range(1005):
            pipe._bvid_locks[f"BV{i}"] = asyncio.Lock()
        active_lock = pipe._bvid_locks["BV500"]

        async def _test():
            await active_lock.acquire()
            new_lock = pipe._bvid_lock("BV_NEW")
            self.assertIn("BV500", pipe._bvid_locks)
            self.assertIn("BV_NEW", pipe._bvid_locks)
            self.assertLess(len(pipe._bvid_locks), 10)
            active_lock.release()

        asyncio.run(_test())


class OfficialConclusionTest(unittest.TestCase):
    def test_render_conclusion(self):
        from bili.ingest import render_conclusion, format_seconds

        self.assertEqual(format_seconds(65), "01:05")
        self.assertEqual(format_seconds(3665), "01:01:05")

        data = {
            "summary": "这是全片核心概述。",
            "outline": [
                {
                    "title": "背景介绍",
                    "timestamp": 0,
                    "part_outline": [
                        {"timestamp": 10, "content": "问题背景与由来"},
                    ],
                },
                {
                    "title": "核心方案",
                    "timestamp": 120,
                    "part_outline": [
                        {"timestamp": 130, "content": "架构设计与权衡"},
                    ],
                },
            ],
        }
        text = render_conclusion(data)
        self.assertIn("【核心概述】", text)
        self.assertIn("这是全片核心概述。", text)
        self.assertIn("【分段大纲】", text)
        self.assertIn("• [00:00] 背景介绍", text)
        self.assertIn("  - 00:10 问题背景与由来", text)
        self.assertIn("• [02:00] 核心方案", text)
        self.assertIn("  - 02:10 架构设计与权衡", text)

    def test_pipeline_prefers_conclusion_without_llm(self):
        """验证官方 AI 总结直取：不调用 LLM，1 秒内直接入库/返回。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = AuditStore(Path(tmp.name) / "test.db")
        self.addCleanup(store.close)

        llm_called = []

        async def fake_llm(prompt: str) -> str:
            llm_called.append(prompt)
            return "标题：测试\n分区：科技\n相关度：90\n\n- 要点1"

        uploaded = []

        async def fake_upload(name: str, content: str, ext: str) -> str:
            uploaded.append((name, content))
            return "doc_123"

        client = BiliClient()

        async def fake_view(bvid: str = "", aid=None):
            return {
                "bvid": "BV1test_conc",
                "cid": 12345,
                "title": "科技新前沿解析",
                "desc": "深入探讨",
                "tname": "科技",
                "author": "科普君",
                "duration": 300,
                "url": "https://www.bilibili.com/video/BV1test_conc",
            }

        async def fake_conclusion(bvid: str, cid=None):
            return {
                "summary": "官方提炼的核心论点",
                "outline": [{"title": "第一章节", "timestamp": 0, "part_outline": []}],
            }

        client.view = fake_view
        client.conclusion = fake_conclusion

        config = {"enabled": True, "conclusion_first": True, "keywords": ["科技"]}
        pipe = LearnPipeline(client, store, fake_llm, fake_upload, config)

        res = asyncio.run(pipe.process_one({"bvid": "BV1test_conc", "keyword": "科技"}))
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("status"), "ingested")
        # 重点：完全不需要调用 LLM
        self.assertEqual(len(llm_called), 0)
        # 验证入库记录素材为官方AI总结
        row = store.get("BV1test_conc")
        self.assertIsNotNone(row)
        self.assertEqual(row.get("material"), "官方AI总结")
        self.assertIn("官方提炼的核心论点", row.get("summary"))

    def test_pipeline_fallback_when_conclusion_empty(self):
        """验证当官方 AI 总结不存在时，平滑降级到字幕 + LLM 流程。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = AuditStore(Path(tmp.name) / "test.db")
        self.addCleanup(store.close)

        llm_called = []

        async def fake_llm(prompt: str) -> str:
            llm_called.append(prompt)
            return "标题：测试视频\n分区：科技\n相关度：95\n\n- 本地模型总结要点"

        uploaded = []

        async def fake_upload(name: str, content: str, ext: str) -> str:
            uploaded.append((name, content))
            return "doc_456"

        client = BiliClient()

        async def fake_view(bvid: str = "", aid=None):
            return {
                "bvid": "BV1test_fallback",
                "cid": 12345,
                "title": "科技自制视频",
                "desc": "自制内容",
                "tname": "科技",
                "author": "创作者",
                "duration": 180,
                "url": "https://www.bilibili.com/video/BV1test_fallback",
            }

        async def fake_conclusion(bvid: str, cid=None):
            return None  # 无官方总结

        async def fake_subtitles(meta, limit, verify):
            return "00:01 大家好今天讲科技发展"

        client.view = fake_view
        client.conclusion = fake_conclusion
        client.subtitles_for_pages = fake_subtitles

        config = {"enabled": True, "conclusion_first": True, "keywords": ["科技"]}
        pipe = LearnPipeline(client, store, fake_llm, fake_upload, config)

        res = asyncio.run(pipe.process_one({"bvid": "BV1test_fallback", "keyword": "科技"}))
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("status"), "ingested")
        # 调用了 LLM 进行摘要
        self.assertEqual(len(llm_called), 1)
        row = store.get("BV1test_fallback")
        self.assertEqual(row.get("material"), "字幕")

    def test_read_one_prefers_conclusion(self):
        """验证 read_one 按需读取也优先利用官方 AI 总结。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = AuditStore(Path(tmp.name) / "test.db")
        self.addCleanup(store.close)

        client = BiliClient()

        async def fake_resolve(ref: str) -> str:
            return "BV1read_conc"

        async def fake_view(bvid: str = "", aid=None):
            return {
                "bvid": "BV1read_conc",
                "cid": 99999,
                "title": "按需视频标题",
                "desc": "简介",
                "tname": "科技",
                "author": "科普UP",
                "duration": 240,
                "url": "https://www.bilibili.com/video/BV1read_conc",
            }

        async def fake_conclusion(bvid: str, cid=None):
            return {
                "summary": "按需官方总结",
                "outline": [],
            }

        client.resolve_bvid = fake_resolve
        client.view = fake_view
        client.conclusion = fake_conclusion

        config = {"enabled": True, "conclusion_first": True, "keywords": ["科技"]}
        pipe = LearnPipeline(client, store, None, None, config)

        res = asyncio.run(pipe.read_one("BV1read_conc", ingest=False))
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("material"), "官方AI总结")
        self.assertIn("按需官方总结", res.get("summary"))


if __name__ == "__main__":
    unittest.main()

