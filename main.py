import asyncio
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request
from astrbot.core.provider.provider import EmbeddingProvider, RerankProvider
from astrbot.core.utils.astrbot_path import get_astrbot_data_path, get_astrbot_plugin_data_path

from .bili.client import BiliClient
from .bili.pipeline import LearnPipeline
from .bili.reference import command_remainder
from .bili.runlog import fmt_ts
from .bili.store import AuditStore

PLUGIN_NAME = "astrbot_plugin_bili_learn"
PLUGIN_VERSION = "1.9.1"


def _tool_class():
    try:
        from .bili.tool import BilibiliReadTool

        return BilibiliReadTool
    except Exception as exc:  # noqa: BLE001
        logger.warning("Bili Learn: FunctionTool unavailable: %s", exc)
        return None


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
        self.client = BiliClient(
            sessdata=str(self.config.get("sessdata") or ""),
            interval=float(self.config.get("request_interval_seconds") or 3),
        )
        self.pipeline = LearnPipeline(
            client=self.client,
            store=self.store,
            llm=self._llm,
            upload=self._upload,
            config=self.config,
            find_doc=self._find_doc_by_name,
            delete_doc=self._delete_doc,
            utility_llm=self._utility_llm,
        )
        self._cron_id = None
        self._kb_id = ""
        self._tool = None
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
        if not bool(self.config.get("on_demand_enabled", True)):
            return
        tool_cls = _tool_class()
        register = getattr(self.context, "add_llm_tools", None)
        if tool_cls is None or not callable(register):
            logger.warning(
                "Bili Learn: AstrBot 不支持 LLM 工具，按需读视频不可用（需要 AstrBot >= 4.5.7）"
            )
            return
        self._tool = tool_cls(
            pipeline=self.pipeline,
            ingest=bool(self.config.get("on_demand_ingest", True)),
        )
        register(self._tool)
        logger.info("Bili Learn: registered tool bilibili_read")

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
        hour = int(self.config.get("daily_start_hour", 1))
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
        pid = str(self.config.get("summary_provider_id") or "").strip()
        if not pid:
            try:
                pid = await self.context.get_current_chat_provider_id("")
            except Exception:
                providers = self.context.get_all_providers()
                if not providers:
                    raise RuntimeError("no chat provider")
                pid = providers[0].meta().id
        resp = await self.context.llm_generate(chat_provider_id=pid, prompt=prompt)
        return getattr(resp, "completion_text", "") or ""

    async def _utility_llm(self, prompt: str) -> str:
        """复审/审核用的模型；未单独配置时跟随摘要模型。"""
        pid = str(self.config.get("utility_provider_id") or "").strip()
        if not pid:
            return await self._llm(prompt)
        resp = await self.context.llm_generate(chat_provider_id=pid, prompt=prompt)
        return getattr(resp, "completion_text", "") or ""

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
        mode = "无限（高消耗）" if self.pipeline.unlimited_mode() else f"每日配额 {self.config.get('daily_per_keyword')} 条/关键词"
        yield event.plain_result(
            f"Bili Learn 知识库={self.config.get('kb_name')} id={self._kb_id or '未创建'} "
            f"入库={counts.get('ingested', 0)} 排除={counts.get('excluded', 0)} "
            f"无字幕={counts.get('no_subtitle', 0)} 失败={counts.get('failed', 0)} "
            f"汇总={counts.get('digests', 0)} 已并入={counts.get('merged', 0)} "
            f"审核可疑={audit.get('suspect', 0)} "
            f"Cookie={'有' if self.client.sessdata else '无'} "
            f"按需读={'开' if self._tool else '关'} "
            f"模式={mode} 今日已入库={quota_desc} "
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
    @bilearn.command("once")
    async def cmd_once(self, event: AstrMessageEvent):
        """立刻跑一轮学习"""
        result = await self._run_pipeline("manual")
        if result.get("skipped"):
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
            }
        )

    async def page_recent(self):
        return json_response({"items": self.store.recent_brief(30)})

    async def page_run(self):
        busy = self._run_lock.locked()
        self._start_background_run("manual", wait=True)
        return json_response({"ok": True, "started": True, "queued": busy})

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
            self._start_background_run("unlimited", wait=True)
            return json_response({"enabled": True, "started": True, "queued": busy, "saved": saved})
        self.store.set_meta("unlimited_mode_last", "0")
        return json_response({"enabled": False, "started": False, "queued": False, "saved": saved})
