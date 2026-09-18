import asyncio
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request
from astrbot.core.provider.provider import EmbeddingProvider, RerankProvider
from astrbot.core.utils.astrbot_path import get_astrbot_data_path, get_astrbot_plugin_data_path

from .bili.client import BiliClient
from .bili.llm import (
    BudgetGuard,
    LLMBudgetExceeded,
    estimate_tokens,
    looks_refusal,
    resolve_provider,
)
from .bili.pipeline import LearnPipeline
from .bili.query import format_for_chat
from .bili.reference import command_remainder
from .bili.runlog import fmt_ts
from .bili.store import AuditStore

PLUGIN_NAME = "astrbot_plugin_bili_learn"
PLUGIN_VERSION = "1.11.1"


def _tool_classes():
    try:
        from .bili.tool import BilibiliKnowledgeTool, BilibiliReadTool

        return BilibiliReadTool, BilibiliKnowledgeTool
    except Exception as exc:  # noqa: BLE001
        logger.warning("Bili Learn: FunctionTool unavailable: %s", exc)
        return None, None


def _data_dir() -> Path:
    try:
        root = Path(get_astrbot_plugin_data_path())
    except Exception:
        root = Path(get_astrbot_data_path()) / "plugin_data"
    path = root / PLUGIN_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


@register(
    PLUGIN_NAME,
    "Sxd55",
    "从 B 站公开视频学习摘要，写入 AstrBot 官方知识库，支持按需读单个视频。",
    PLUGIN_VERSION,
)
class BiliLearnPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = _data_dir()
        self.store = AuditStore(self.data_dir / "bili_learn.db")
        try:
            interval = float(self.config.get("request_interval_seconds") or 3)
        except (TypeError, ValueError):
            interval = 3.0
        self.client = BiliClient(
            sessdata=str(self.config.get("sessdata") or ""),
            interval=interval,
        )
        self._llm_guard: BudgetGuard | None = None
        self.pipeline = LearnPipeline(
            client=self.client,
            store=self.store,
            llm=self._llm_for("summary"),
            upload=self._upload,
            config=self.config,
            find_doc=self._find_doc_by_name,
            delete_doc=self._delete_doc,
            utility_llm=self._llm_for("utility"),
        )
        self._cron_id = None
        self._kb_id = ""
        self._tool = None
        self._query_tool = None
        self._run_lock = asyncio.Lock()
        self._bg_tasks: set[asyncio.Task] = set()
        self._register_tool()
        self.context.register_web_api(f"/{PLUGIN_NAME}/status", self.page_status, ["GET"], "Status")
        self.context.register_web_api(f"/{PLUGIN_NAME}/recent", self.page_recent, ["GET"], "Recent videos")
        self.context.register_web_api(f"/{PLUGIN_NAME}/run", self.page_run, ["POST"], "Run once")
        self.context.register_web_api(f"/{PLUGIN_NAME}/runs", self.page_runs, ["GET"], "Run history")
        self.context.register_web_api(f"/{PLUGIN_NAME}/run-events", self.page_run_events, ["GET"], "Run events")
        self.context.register_web_api(f"/{PLUGIN_NAME}/health", self.page_health, ["GET"], "Health check")
        self.context.register_web_api(f"/{PLUGIN_NAME}/interest", self.page_interest, ["GET"], "Interest list")
        self.context.register_web_api(f"/{PLUGIN_NAME}/interest/save", self.page_interest_save, ["POST"], "Save interest list")
        self.context.register_web_api(f"/{PLUGIN_NAME}/theme", self.page_theme, ["GET"], "UI theme")
        self.context.register_web_api(f"/{PLUGIN_NAME}/theme/save", self.page_theme_save, ["POST"], "Save UI theme")
        self.context.register_web_api(f"/{PLUGIN_NAME}/unlimited", self.page_unlimited, ["POST"], "Toggle unlimited mode")
        logger.info("Bili Learn loaded, db=%s", self.store.db_path)

    async def initialize(self):
        try:
            await self.client.warmup()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bili Learn: fingerprint warmup failed: %s", exc)
        try:
            await self._ensure_kb()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bili Learn: knowledge base init failed: %s", exc)
        await self._ensure_cron()
        self._maybe_start_unlimited()

    def _start_background_run(self, trigger: str, wait: bool = False) -> bool:
        if self._run_lock.locked() and not wait:
            return False
        pending = sum(1 for task in self._bg_tasks if not task.done())
        if pending >= 3:
            logger.warning("Bili Learn: too many queued runs (%s), refuse %s", pending, trigger)
            return False
        task = asyncio.create_task(self._run_pipeline_queued(trigger))
        self._bg_tasks.add(task)
        task.add_done_callback(self._on_background_done)
        return True

    async def _run_pipeline_queued(self, trigger: str) -> dict:
        async with self._run_lock:
            return await self.pipeline.run(
                trigger=trigger,
                kb_id=self._kb_id,
                embedding_provider=self._resolve_embedding_id(),
                rerank_provider=self._resolve_rerank_id() or "",
            )

    def _on_background_done(self, task: asyncio.Task) -> None:
        self._bg_tasks.discard(task)
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc:
            logger.error("Bili Learn background run failed: %s", exc)

    def _maybe_start_unlimited(self) -> None:
        if not self.pipeline.unlimited_mode():
            self.store.set_meta("unlimited_mode_last", "0")
            return
        if self.store.get_meta("unlimited_mode_last") == "1":
            return
        self.store.set_meta("unlimited_mode_last", "1")
        self._start_background_run("unlimited", wait=True)
        logger.info("Bili Learn: 无限模式已开启，开始排队刷取")

    def _next_run_text(self) -> str:
        if not self._cron_id:
            return "未注册"
        try:
            nxt = self.context.cron_manager.get_next_run_time(self._cron_id)
        except Exception:  # noqa: BLE001
            return "未知"
        if nxt is None:
            return "无"
        try:
            return fmt_ts(int(nxt.timestamp()))
        except (TypeError, ValueError, OSError):
            return "未知"

    def _schedule_text(self) -> str:
        try:
            hour = int(self.config.get("daily_start_hour", 1))
        except (TypeError, ValueError):
            hour = 1
        if hour < 0 or hour > 23:
            return "关闭（只手动）"
        return f"每天{hour:02d}:00"

    def _register_tool(self) -> None:
        read_cls, query_cls = _tool_classes()
        register = getattr(self.context, "add_llm_tools", None)
        if read_cls is None or not callable(register):
            logger.warning(
                "Bili Learn: AstrBot 不支持 LLM 工具，按需读与知识查询不可用（需要 AstrBot >= 4.5.7）"
            )
            return
        tools = []
        if bool(self.config.get("on_demand_enabled", True)):
            self._tool = read_cls(
                pipeline=self.pipeline,
                ingest=bool(self.config.get("on_demand_ingest", True)),
            )
            tools.append(self._tool)
        if bool(self.config.get("query_enabled", True)) and query_cls is not None:
            self._query_tool = query_cls(query_knowledge=self._query_knowledge)
            tools.append(self._query_tool)
        if not tools:
            return
        try:
            register(*tools)
        except TypeError:
            for tool in tools:
                register(tool)
        logger.info(
            "Bili Learn: registered tools %s",
            "、".join(getattr(tool, "name", "?") for tool in tools),
        )

    async def terminate(self):
        try:
            if self._cron_id:
                await self.context.cron_manager.delete_job(self._cron_id)
        except Exception:
            pass
        for task in list(self._bg_tasks):
            task.cancel()
        try:
            self.store.close()
        except Exception:
            pass

    def _provider_id(self, provider) -> str:
        try:
            meta = provider.meta()
            return str(getattr(meta, "id", "") or "")
        except Exception:
            return str(getattr(provider, "id", "") or "")

    def _resolve_embedding_id(self) -> str:
        configured = str(self.config.get("embedding_provider_id") or "").strip()
        if configured:
            return configured
        getter = getattr(self.context, "get_all_embedding_providers", None)
        providers = getter() if callable(getter) else []
        if not providers:
            inst_map = getattr(getattr(self.context, "provider_manager", None), "inst_map", {}) or {}
            providers = [p for p in inst_map.values() if isinstance(p, EmbeddingProvider)]
        for provider in providers:
            pid = self._provider_id(provider)
            if pid:
                logger.info("Bili Learn: using AstrBot embedding provider %s", pid)
                return pid
        return ""

    def _resolve_rerank_id(self) -> str | None:
        configured = str(self.config.get("rerank_provider_id") or "").strip()
        if configured:
            return configured
        inst_map = getattr(getattr(self.context, "provider_manager", None), "inst_map", {}) or {}
        for provider in inst_map.values():
            if isinstance(provider, RerankProvider):
                pid = self._provider_id(provider)
                if pid:
                    logger.info("Bili Learn: using AstrBot rerank provider %s", pid)
                    return pid
        return None

    async def _ensure_kb(self) -> str:
        name = str(self.config.get("kb_name") or "Bili Learn")
        mgr = self.context.kb_manager
        existing = await mgr.get_kb_by_name(name)
        if existing:
            self._kb_id = existing.kb.kb_id
            return self._kb_id
        emb = self._resolve_embedding_id()
        if not emb:
            logger.warning(
                "Bili Learn: no Embedding Provider in AstrBot and no embedding_provider_id, KB not created"
            )
            return ""
        rerank = self._resolve_rerank_id()
        helper = await mgr.create_kb(
            kb_name=name,
            description="B 站公开视频摘要。由 astrbot_plugin_bili_learn 写入。",
            emoji="📺",
            embedding_provider_id=emb,
            rerank_provider_id=rerank,
        )
        self._kb_id = helper.kb.kb_id
        logger.info("Bili Learn created KB %s (%s) embedding=%s rerank=%s", name, self._kb_id, emb, rerank or "")
        return self._kb_id

    async def _ensure_cron(self) -> None:
        try:
            hour = int(self.config.get("daily_start_hour", 1))
        except (TypeError, ValueError):
            logger.warning("Bili Learn: bad daily_start_hour=%r, cron disabled", self.config.get("daily_start_hour"))
            return
        if hour < 0 or hour > 23:
            logger.info("Bili Learn: daily_start_hour=%s，未注册定时任务（只手动运行）", hour)
            return
        cron = f"0 {hour} * * *"
        try:
            jobs = await self.context.cron_manager.list_jobs("basic")
        except Exception:  # noqa: BLE001
            jobs = []
        for job in jobs or []:
            if getattr(job, "name", "") in ("bili-learn-daily", "bili-learn-pass"):
                try:
                    await self.context.cron_manager.delete_job(job.job_id)
                except Exception:  # noqa: BLE001
                    pass
        kwargs = {
            "name": "bili-learn-daily",
            "cron_expression": cron,
            "handler": self._cron_run,
            "description": f"Bili Learn daily pass at {hour:02d}:00",
            "persistent": False,
        }
        job = None
        try:
            job = await self.context.cron_manager.add_basic_job(
                **kwargs, timezone="Asia/Shanghai"
            )
        except Exception:  # noqa: BLE001
            try:
                job = await self.context.cron_manager.add_basic_job(**kwargs)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Bili Learn cron not registered: %s", exc)
                return
        self._cron_id = job.job_id
        logger.info("Bili Learn: daily cron registered at %02d:00 (job=%s)", hour, self._cron_id)

    async def _run_pipeline(self, trigger: str) -> dict:
        if self._run_lock.locked():
            return {"ok": False, "reason": "already_running", "message": "已有一次学习任务在进行，请等它结束。"}
        async with self._run_lock:
            return await self.pipeline.run(
                trigger=trigger,
                kb_id=self._kb_id,
                embedding_provider=self._resolve_embedding_id(),
                rerank_provider=self._resolve_rerank_id() or "",
            )

    async def _cron_run(self):
        if not self.config.get("enabled", True):
            return
        return await self._run_pipeline("cron")

    async def _llm(self, prompt: str) -> str:
        """摘要任务（保留旧方法名给测试与外部调用）。"""
        return await self._llm_task("summary", prompt)

    async def _utility_llm(self, prompt: str) -> str:
        """复审/审核任务。"""
        return await self._llm_task("utility", prompt)

    def llm_guard(self) -> BudgetGuard:
        if self._llm_guard is None:
            self._llm_guard = BudgetGuard(
                self.config, lambda: self.store.llm_tokens_today()
            )
        return self._llm_guard

    def _llm_for(self, task: str):
        async def call(prompt: str) -> str:
            return await self._llm_task(task, prompt)

        return call

    async def _resolve_provider_id(self, provider_id: str) -> str:
        if provider_id:
            return provider_id
        try:
            current = await self.context.get_current_chat_provider_id("")
            if current:
                return str(current)
        except Exception:  # noqa: BLE001
            pass
        providers = self.context.get_all_providers()
        if not providers:
            raise RuntimeError("no chat provider")
        return str(providers[0].meta().id)

    async def _generate(self, provider_id: str, prompt: str) -> tuple[str, int, int]:
        resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
        text = str(getattr(resp, "completion_text", "") or "")
        usage = getattr(resp, "usage", None)
        tokens_in = int(getattr(usage, "input", 0) or 0) if usage is not None else 0
        tokens_out = int(getattr(usage, "output", 0) or 0) if usage is not None else 0
        if tokens_in <= 0:
            tokens_in = estimate_tokens(prompt)
        if tokens_out <= 0:
            tokens_out = estimate_tokens(text)
        return text, tokens_in, tokens_out

    async def _llm_task(self, task: str, prompt: str) -> str:
        """统一入口：预算闸 → 选模（显式/档位/默认）→ 调用 → 记账 → 拒答重试。"""
        guard = self.llm_guard()
        decision = guard.check(task, prompt)
        if not decision.allowed:
            self.store.add_llm_usage(task, "", 0, 0, ok=False, reason=decision.reason)
            logger.warning(
                "Bili Learn: LLM budget blocked task=%s reason=%s used=%s",
                task, decision.reason, guard.status()["used"],
            )
            raise LLMBudgetExceeded(decision.reason)
        configured, source = resolve_provider(task, self.config)
        provider_id = await self._resolve_provider_id(configured)
        if decision.provider_override:
            source = decision.reason if decision.provider_override == configured else "single_call_cap"
            provider_id = decision.provider_override
        text, tokens_in, tokens_out = await self._generate(provider_id, prompt)
        if looks_refusal(text):
            fallback = guard.fallback_provider()
            if fallback and fallback != provider_id:
                self.store.add_llm_usage(
                    task, provider_id, tokens_in, tokens_out, ok=True,
                    source=source, reason="refusal_retry",
                )
                logger.warning(
                    "Bili Learn: model refusal on task=%s, retrying with fallback %s",
                    task, fallback,
                )
                text, tokens_in, tokens_out = await self._generate(fallback, prompt)
                self.store.add_llm_usage(
                    task, fallback, tokens_in, tokens_out, ok=True, source="fallback"
                )
                return text
            logger.warning("Bili Learn: model refusal on task=%s (no fallback configured)", task)
        self.store.add_llm_usage(task, provider_id, tokens_in, tokens_out, ok=True, source=source)
        return text

    async def _upload(self, file_name: str, text: str, file_type: str) -> str:
        kb_id = self._kb_id or await self._ensure_kb()
        if not kb_id:
            raise RuntimeError("knowledge base unavailable")
        helper = await self.context.kb_manager.get_kb(kb_id)
        if helper is None:
            raise RuntimeError("knowledge base missing")
        doc = await helper.upload_document(
            file_name=file_name,
            file_content=text.encode("utf-8"),
            file_type=file_type,
        )
        return getattr(doc, "doc_id", "") or ""

    async def _find_doc_by_name(self, doc_name: str) -> str | None:
        """知识库按文件名精确查重，防止同一视频写入两份文档。"""
        kb_id = self._kb_id or await self._ensure_kb()
        if not kb_id:
            return None
        helper = await self.context.kb_manager.get_kb(kb_id)
        if helper is None:
            return None
        lister = getattr(helper, "list_documents", None)
        if not callable(lister):
            return None
        try:
            docs = await lister(0, 50, search=doc_name)
        except TypeError:
            docs = await lister()
        except Exception:  # noqa: BLE001
            return None
        for doc in docs or []:
            if str(getattr(doc, "doc_name", "") or "") == doc_name:
                return str(getattr(doc, "doc_id", "") or "")
        return None

    async def _delete_doc(self, doc_id: str) -> None:
        """按 doc_id 删除知识库文档（汇总合并原文档时使用）。"""
        if not doc_id:
            return
        kb_id = self._kb_id or await self._ensure_kb()
        if not kb_id:
            raise RuntimeError("knowledge base unavailable")
        helper = await self.context.kb_manager.get_kb(kb_id)
        if helper is None:
            raise RuntimeError("knowledge base missing")
        deleter = getattr(helper, "delete_document", None)
        if not callable(deleter):
            raise RuntimeError("knowledge base delete unsupported")
        await deleter(doc_id)

    def _query_top_k(self, top_k: Any = 0) -> int:
        try:
            value = int(top_k) if top_k else int(self.config.get("query_top_k") or 5)
        except (TypeError, ValueError):
            value = 5
        return max(1, min(10, value))

    def _kb_name(self) -> str:
        return str(self.config.get("kb_name") or "Bili Learn")

    async def _kb_retrieve(self, query: str, top_k: int) -> list[dict[str, Any]] | None:
        """知识库语义检索；不可用时返回 None（调用方回退本地摘要库）。"""
        kb_id = self._kb_id or await self._ensure_kb()
        if not kb_id:
            return None
        retrieve = getattr(self.context.kb_manager, "retrieve", None)
        if not callable(retrieve):
            return None
        try:
            try:
                data = await retrieve(
                    query=query,
                    kb_names=[self._kb_name()],
                    top_k_fusion=20,
                    top_m_final=top_k,
                )
            except TypeError:
                data = await retrieve(query, [self._kb_name()])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bili Learn: knowledge retrieval failed: %s", exc)
            return None
        if not isinstance(data, dict):
            return None
        items: list[dict[str, Any]] = []
        for row in (data.get("results") or [])[:top_k]:
            if not isinstance(row, dict):
                continue
            items.append(
                {
                    "kind": "kb",
                    "doc_name": str(row.get("doc_name") or ""),
                    "score": row.get("score"),
                    "content": str(row.get("content") or ""),
                    "doc_id": str(row.get("doc_id") or ""),
                }
            )
        return items

    async def _kb_doc_text(self, doc_name: str) -> str:
        """按文件名读知识库文档全文；读不到返回空串。"""
        if not doc_name:
            return ""
        kb_id = self._kb_id or await self._ensure_kb()
        if not kb_id:
            return ""
        helper = await self.context.kb_manager.get_kb(kb_id)
        if helper is None:
            return ""
        lister = getattr(helper, "list_documents", None)
        if not callable(lister):
            return ""
        try:
            try:
                docs = await lister(0, 10, search=doc_name)
            except TypeError:
                docs = await lister()
        except Exception:  # noqa: BLE001
            return ""
        doc_id = ""
        for doc in docs or []:
            if str(getattr(doc, "doc_name", "") or "") == doc_name:
                doc_id = str(getattr(doc, "doc_id", "") or "")
                break
        if not doc_id:
            for doc in docs or []:
                if doc_name in str(getattr(doc, "doc_name", "") or ""):
                    doc_id = str(getattr(doc, "doc_id", "") or "")
                    break
        if not doc_id:
            return ""
        getter = getattr(helper, "get_chunks_by_doc_id", None)
        if not callable(getter):
            return ""
        try:
            chunks = await getter(doc_id, 0, 200)
        except Exception:  # noqa: BLE001
            return ""
        rows = [row for row in (chunks or []) if isinstance(row, dict)]
        rows.sort(key=lambda row: int(row.get("chunk_index") or 0))
        return "\n".join(str(row.get("content") or "") for row in rows).strip()

    def _knowledge_overview(self) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "overview",
            "kb_ready": bool(self._kb_id),
            "counts": self.store.counts(),
            "categories": self.store.category_counts(),
            "digests": self.store.all_digest_briefs()[:20],
            "recent": self.store.recent_ingested(10),
        }

    async def _query_knowledge(
        self, query: str = "", top_k: Any = 0, doc_name: str = ""
    ) -> dict[str, Any]:
        """统一的查询入口：工具和命令都走这里。

        优先知识库语义检索，不可用或没命中时回退本地 SQLite 摘要搜索。
        """
        query = str(query or "").strip()
        doc_name = str(doc_name or "").strip()
        k = self._query_top_k(top_k)
        if not query and not doc_name:
            return self._knowledge_overview()
        if doc_name:
            text = await self._kb_doc_text(doc_name)
            if text:
                return {
                    "ok": True,
                    "mode": "doc",
                    "source": "kb",
                    "doc_name": doc_name,
                    "content": text,
                }
            local = self.store.search_local(doc_name, limit=3)
            if local:
                return {
                    "ok": True,
                    "mode": "search",
                    "source": "local",
                    "query": doc_name,
                    "items": local,
                    "note": "知识库里没有这篇文档，以下是本地摘要库的匹配结果。",
                }
            return {
                "ok": False,
                "mode": "doc",
                "doc_name": doc_name,
                "note": f"没有找到文档「{doc_name}」，可以先用 query 检索。",
            }
        kb_items = await self._kb_retrieve(query, k)
        if kb_items:
            return {
                "ok": True,
                "mode": "search",
                "source": "kb",
                "query": query,
                "items": kb_items,
            }
        local_items = self.store.search_local(query, limit=k)
        if local_items:
            return {
                "ok": True,
                "mode": "search",
                "source": "local",
                "query": query,
                "items": local_items,
                "note": "知识库语义检索不可用或没有命中，以下是本地摘要库的关键词匹配结果。",
            }
        return {
            "ok": True,
            "mode": "empty",
            "query": query,
            "note": f"知识库和本地摘要库都没有和「{query}」相关的内容。",
        }

    @filter.command_group("bilearn")
    def bilearn(self):
        pass

    @bilearn.command("status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看 Bili Learn 状态"""
        counts = self.store.counts()
        last = self.store.last_run()
        emb = self._resolve_embedding_id() or "无"
        rerank = self._resolve_rerank_id() or "无"
        daily = self.store.daily_counts()
        quota_desc = "、".join(f"{k}:{v}" for k, v in daily.items()) or "无"
        audit = self.store.audit_stats()
        tokens = self.tokens_status()
        hard = tokens["hard_limit"] or "不限"
        soft = tokens["soft_limit"] or "不限"
        skipped = sum(item.get("count", 0) for item in tokens.get("skipped") or [])
        mode = "无限（高消耗）" if self.pipeline.unlimited_mode() else f"每日配额 {self.config.get('daily_per_keyword') or 3} 条/关键词"
        yield event.plain_result(
            f"Bili Learn 知识库={self.config.get('kb_name') or 'Bili Learn'} id={self._kb_id or '未创建'} "
            f"入库={counts.get('ingested', 0)} 排除={counts.get('excluded', 0)} "
            f"无字幕={counts.get('no_subtitle', 0)} 失败={counts.get('failed', 0)} "
            f"汇总={counts.get('digests', 0)} 已并入={counts.get('merged', 0)} "
            f"审核可疑={audit.get('suspect', 0)} "
            f"Cookie={'有' if self.client.sessdata else '无'} "
            f"按需读={'开' if self._tool else '关'} "
            f"知识查询={'开' if self._query_tool else '关'} "
            f"模式={mode} 今日已入库={quota_desc} "
            f"今日Token={tokens['used']}（硬限 {hard} / 软限 {soft} / 预算跳过 {skipped} 次） "
            f"Embedding={emb} Rerank={rerank} "
            f"上次运行={last.get('status') if last else '无'} "
            f"时间={fmt_ts(last.get('started_at')) if last else '无'} "
            f"run_id={last.get('run_id') if last else '无'} "
            f"定时={self._schedule_text()} 下次={self._next_run_text()}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @bilearn.command("read")
    async def cmd_read(self, event: AstrMessageEvent):
        """读取一个 B 站视频并返回摘要（不经过 AI 工具）"""
        reference = command_remainder(event.message_str, "read").strip()
        if not reference:
            yield event.plain_result("用法：/bilearn read <BV号 / av号 / 链接>")
            return
        try:
            result = await self.pipeline.read_one(
                reference,
                ingest=bool(self.config.get("on_demand_ingest", True)),
            )
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"读取失败：{exc}")
            return
        if not result.get("ok"):
            yield event.plain_result(str(result.get("message") or "读取失败。"))
            return
        yield event.plain_result(
            f"《{result.get('title') or result.get('bvid')}》 {result.get('url')}\n"
            f"归类：{result.get('category') or '其他'} 素材：{result.get('material')} "
            f"入库：{'是' if result.get('ingested') else '否'}\n"
            f"文档：{result.get('doc_name') or '未写入'}\n\n"
            f"{result.get('summary')}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @bilearn.command("search")
    async def cmd_search(self, event: AstrMessageEvent):
        """查询知识库学过的内容；不带参数列出概览"""
        query = command_remainder(event.message_str, "search").strip()
        try:
            result = await self._query_knowledge(query=query)
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"查询失败：{exc}")
            return
        yield event.plain_result(format_for_chat(result))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @bilearn.command("once")
    async def cmd_once(self, event: AstrMessageEvent):
        """立刻跑一轮学习"""
        result = await self._run_pipeline("manual")
        if result.get("skipped") is True or result.get("reason") == "disabled":
            yield event.plain_result(f"未运行：{result.get('reason') or '已跳过'}")
            return
        if not result.get("ok"):
            yield event.plain_result(
                str(
                    result.get("message")
                    or result.get("error")
                    or result.get("reason")
                    or "未知原因"
                )
            )
            return
        aborted = f" 中止={result['aborted']}" if result.get("aborted") else ""
        yield event.plain_result(
            f"运行完成 run_id={result.get('run_id')} 轮次={result.get('rounds')} "
            f"处理={result.get('processed')} 入库={result.get('ingested')} "
            f"跳过={result.get('skipped')} 失败={result.get('failed')}{aborted}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @bilearn.command("recent")
    async def cmd_recent(self, event: AstrMessageEvent):
        """最近处理的视频"""
        arg = command_remainder(event.message_str, "recent")
        first = arg.split()[0] if arg.split() else ""
        n = int(first) if first.isdigit() else 8
        items = self.store.recent_brief(max(1, min(n, 20)))
        if not items:
            yield event.plain_result("还没有处理记录。")
            return
        lines = [
            f"{fmt_ts(v.get('updated_at'))} {v['bvid']} {v['status']} "
            f"{v['title'][:40]} ({v['reason']})"
            for v in items
        ]
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @bilearn.command("quota")
    async def cmd_quota(self, event: AstrMessageEvent):
        """查看或重置今日入库配额计数"""
        arg = command_remainder(event.message_str, "quota").strip().lower()
        if arg in ("reset", "重置", "clear"):
            self.store.reset_daily()
            yield event.plain_result("已重置今日入库配额计数。")
            return
        daily = self.store.daily_counts()
        if self.pipeline.unlimited_mode():
            desc = "、".join(f"{k}:{v}" for k, v in daily.items()) or "无"
            yield event.plain_result(f"无限模式已开启，配额不生效。今日已入库：{desc}")
            return
        if not daily:
            yield event.plain_result("今日还没有入库记录，所有配额可用。")
            return
        lines = []
        plan = dict(self.pipeline.interest_plan())
        for keyword in self.pipeline.keywords():
            used = daily.get(keyword, 0)
            quota = self.pipeline.daily_quota(keyword)
            total = "不限" if quota <= 0 else str(quota)
            lines.append(f"{keyword}: 今日 {used}/{total}（每轮目标 {plan.get(keyword, 0)}）")
        for keyword, used in daily.items():
            if keyword not in self.pipeline.keywords():
                lines.append(f"{keyword}（非当前关键词）: {used}")
        yield event.plain_result("今日配额：\n" + "\n".join(lines) + "\n（/bilearn quota reset 可重置当天计数）")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @bilearn.command("digest")
    async def cmd_digest(self, event: AstrMessageEvent):
        """立即汇总某关键词的未并入视频；不带参数则自动挑一个"""
        keyword = command_remainder(event.message_str, "digest").strip()
        try:
            result = await self.pipeline.consolidate(keyword=keyword, force=True)
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"汇总失败：{exc}")
            return
        if result.get("ok") and result.get("keyword"):
            yield event.plain_result(
                f"汇总完成：「{result.get('keyword')}」第 {result.get('round')} 轮，"
                f"来源 {result.get('sources')} 条，删除原文档 {result.get('deleted')} 份\n"
                f"文档：{result.get('doc_name')}"
            )
            return
        reason = result.get("reason")
        if reason == "review_failed":
            yield event.plain_result(
                f"汇总「{result.get('keyword')}」复审未通过，本轮未写入。\n问题：{result.get('problems')}"
            )
        elif reason == "nothing":
            yield event.plain_result("没有需要汇总的文档。")
        elif reason == "disabled":
            yield event.plain_result("汇总功能已关闭。")
        else:
            yield event.plain_result(f"汇总未执行：{reason or result}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @bilearn.command("audit")
    async def cmd_audit(self, event: AstrMessageEvent):
        """查看审核状态；/bilearn audit run [n] 立即审核 n 篇"""
        arg = command_remainder(event.message_str, "audit").strip()
        if arg.lower().startswith("run"):
            parts = arg.split()
            if len(parts) > 1 and parts[1].isdigit():
                n = int(parts[1])
            else:
                n = max(1, self.pipeline.audit_daily_limit() or 20)
            try:
                result = await self.pipeline.audit_due_docs(limit=n, force=True)
            except Exception as exc:  # noqa: BLE001
                yield event.plain_result(f"审核失败：{exc}")
                return
            if not result.get("ok"):
                yield event.plain_result(f"审核未执行：{result.get('reason')}")
                return
            lines = [f"审核 {result.get('audited')} 篇，可疑 {result.get('suspect')} 篇"]
            for item in result.get("details", [])[:10]:
                key = item.get("bvid") or item.get("keyword")
                lines.append(f"{key}: {item.get('status')} {item.get('note') or ''}")
            yield event.plain_result("\n".join(lines))
            return
        stats = self.store.audit_stats()
        digests = self.store.all_digest_briefs()
        lines = [
            f"审核：通过 {stats.get('ok', 0)} 可疑 {stats.get('suspect', 0)} "
            f"错误 {stats.get('error', 0)} 未审 {stats.get('pending', 0)}",
            f"汇总文档：{len(digests)} 份（可疑 {stats.get('digest_suspect', 0)}）",
            f"重审间隔 {self.pipeline.audit_interval_days()} 天，每日上限 {self.pipeline.audit_daily_limit()} 篇",
        ]
        suspects = self.store.recent_suspects(10)
        if suspects:
            lines.append("可疑列表：")
            for item in suspects:
                lines.append(
                    f"- {item['bvid']} {str(item.get('title') or '')[:30]}："
                    f"{str(item.get('audit_note') or '')[:80]}"
                )
        yield event.plain_result("\n".join(lines))

    def tokens_status(self) -> dict[str, Any]:
        guard = self.llm_guard()
        status = guard.status()
        status["by_task"] = self.store.llm_usage_today_by_task()
        status["skipped"] = self.store.llm_skips_today()
        return status

    async def page_status(self):
        digests = [
            {
                key: item.get(key)
                for key in ("keyword", "doc_name", "rounds", "sources", "audit_status", "audit_note")
            }
            for item in self.store.all_digest_briefs()
        ]
        return json_response(
            {
                "counts": self.store.counts(),
                "kb_id": self._kb_id,
                "kb_name": self.config.get("kb_name"),
                "has_cookie": bool(self.client.sessdata),
                "enabled": bool(self.config.get("enabled", True)),
                "on_demand": bool(self._tool),
                "unlimited_mode": self.pipeline.unlimited_mode(),
                "daily": self.store.daily_counts(),
                "audit": self.store.audit_stats(),
                "digests": digests,
                "daily_start_hour": self.config.get("daily_start_hour", 1),
                "cron_registered": bool(self._cron_id),
                "next_run": self._next_run_text(),
                "version": PLUGIN_VERSION,
                "tokens": self.tokens_status(),
            }
        )

    async def page_recent(self):
        return json_response({"items": self.store.recent_brief(30)})

    async def page_run(self):
        busy = self._run_lock.locked()
        started = self._start_background_run("manual", wait=True)
        return json_response({"ok": started, "started": started, "queued": busy})

    async def page_runs(self):
        return json_response({"items": self.store.recent_runs(30)})

    async def page_run_events(self):
        run_id = request.query.get("run_id", "")
        return json_response({"items": self.store.run_events(run_id, 200)})

    async def page_health(self):
        emb = self._resolve_embedding_id()
        rerank = self._resolve_rerank_id()
        kb_ready = bool(self._kb_id)
        if not kb_ready:
            try:
                existing = await self.context.kb_manager.get_kb_by_name(
                    str(self.config.get("kb_name") or "Bili Learn")
                )
                kb_ready = bool(existing)
            except Exception:  # noqa: BLE001
                kb_ready = False
        return json_response({
            "bili_search": "public_search_available",
            "recommend": "not_used",
            "cookie": "configured" if self.client.sessdata else "empty_public_mode",
            "embedding": emb or "missing",
            "rerank": rerank or "not_configured",
            "knowledge_base": "ready" if kb_ready else "missing_embedding_provider",
            "on_demand_tool": "ready" if self._tool else "disabled_or_unsupported",
            "knowledge_query": "ready" if self._query_tool else "disabled_or_unsupported",
            "subtitle_cooldown_seconds": round(self.client.throttle.cooldown_remaining(), 1),
            "last_run": self.store.last_run(),
        })

    async def page_interest(self):
        plan = self.pipeline.interest_plan()
        return json_response(
            {
                "items": [{"keyword": keyword, "quota": quota} for keyword, quota in plan],
                "configured": self.pipeline.interest_configured(),
                "unlimited_mode": self.pipeline.unlimited_mode(),
            }
        )

    async def page_interest_save(self):
        payload = await request.json(default={})
        items = payload.get("items")
        if not isinstance(items, list):
            return error_response("items must be a list")
        cleaned: list[dict] = []
        seen: set[str] = set()
        for item in items[:50]:
            if not isinstance(item, dict):
                continue
            keyword = str(item.get("keyword") or "").strip()[:50]
            if not keyword or keyword in seen:
                continue
            try:
                quota = max(0, min(999, int(float(item.get("quota") or 0))))
            except (TypeError, ValueError):
                quota = 0
            seen.add(keyword)
            cleaned.append({"__template_key": "interest", "keyword": keyword, "quota": quota})
        self.config["interest_quotas"] = cleaned
        saver = getattr(self.config, "save_config", None)
        if not callable(saver):
            return error_response("当前 AstrBot 版本不支持保存插件配置")
        saver()
        return json_response({"saved": True, "count": len(cleaned)})

    async def page_theme(self):
        return json_response(
            {
                "name": str(self.config.get("ui_theme") or "mist"),
                "custom": self.config.get("ui_theme_custom") or {},
            }
        )

    async def page_theme_save(self):
        payload = await request.json(default={})
        name = str(payload.get("name") or "mist")
        if name not in ("mist", "clay", "celadon", "lavender", "graphite", "custom"):
            name = "mist"
        self.config["ui_theme"] = name
        custom = payload.get("custom")
        if isinstance(custom, dict):
            self.config["ui_theme_custom"] = {
                "bg": str(custom.get("bg") or ""),
                "dark": str(custom.get("dark") or ""),
                "light": str(custom.get("light") or ""),
                "accent": str(custom.get("accent") or ""),
            }
        saver = getattr(self.config, "save_config", None)
        if not callable(saver):
            return error_response("当前 AstrBot 版本不支持保存插件配置")
        saver()
        return json_response({"saved": True, "name": name})

    async def page_unlimited(self):
        payload = await request.json(default={})
        enabled = payload.get("enabled") is True
        self.config["unlimited_mode"] = enabled
        saver = getattr(self.config, "save_config", None)
        saved = False
        if callable(saver):
            saver()
            saved = True
        if enabled:
            self.store.set_meta("unlimited_mode_last", "1")
            busy = self._run_lock.locked()
            started = self._start_background_run("unlimited", wait=True)
            return json_response({"enabled": True, "started": started, "queued": busy, "saved": saved})
        self.store.set_meta("unlimited_mode_last", "0")
        return json_response({"enabled": False, "started": False, "queued": False, "saved": saved})
