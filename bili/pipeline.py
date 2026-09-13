"""一轮学习与按需读取：关键词搜索 → 字幕/简介 → 摘要 → 官方知识库。"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from .client import BiliClient, BiliRiskError
from .ingest import (
    AUDIT_PROMPT,
    DIGEST_PROMPT,
    DIGEST_REVIEW_PROMPT,
    FALLBACK_CATEGORY,
    SUMMARY_PROMPT,
    build_doc_name,
    clip,
    declared_category,
    declared_score,
    digest_doc_name,
    guess_category,
    is_suspect_verdict,
    now_bj,
    parse_summary,
    render_digest,
    render_digest_section,
    render_document,
    review_verdict,
    sample_text,
    worth_keeping,
)
from .runlog import new_run_id, next_day_start_bj, now_ts, today_start_ts
from .store import AuditStore

LLMFn = Callable[[str], Awaitable[str]]
KBUpload = Callable[[str, str, str], Awaitable[str]]
DocLookup = Callable[[str], Awaitable[str | None]]
DocDelete = Callable[[str], Awaitable[None]]

REVIEW_SOURCES_CHARS = 8000


def _parse_interest_items(raw: Any) -> list[tuple[str, int]]:
    """解析 template_list 配置：每项 {keyword, quota}。"""
    plan: list[tuple[str, int]] = []
    if not isinstance(raw, list):
        return plan
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        keyword = str(item.get("keyword") or "").strip()[:50]
        if not keyword or keyword in seen:
            continue
        try:
            quota = int(float(item.get("quota") or 0))
        except (TypeError, ValueError):
            quota = 0
        seen.add(keyword)
        plan.append((keyword, max(0, quota)))
    return plan


class LearnPipeline:
    def __init__(
        self,
        client: BiliClient,
        store: AuditStore,
        llm: LLMFn | None,
        upload: KBUpload | None,
        config: dict[str, Any],
        find_doc: DocLookup | None = None,
        delete_doc: DocDelete | None = None,
        utility_llm: LLMFn | None = None,
    ):
        self.client = client
        self.store = store
        self.llm = llm
        self.utility_llm = utility_llm
        self.upload = upload
        self.find_doc = find_doc
        self.delete_doc = delete_doc
        self.config = config
        self._bvid_locks: dict[str, asyncio.Lock] = {}
        self._digest_lock = asyncio.Lock()
        self._audit_lock = asyncio.Lock()

    def _review_llm(self) -> LLMFn | None:
        return self.utility_llm or self.llm

    def _bvid_lock(self, bvid: str) -> asyncio.Lock:
        lock = self._bvid_locks.get(bvid)
        if lock is None:
            lock = asyncio.Lock()
            self._bvid_locks[bvid] = lock
        return lock

    def keywords(self) -> list[str]:
        plan = _parse_interest_items(self.config.get("interest_quotas"))
        if plan:
            return [keyword for keyword, _ in plan]
        raw = str(self.config.get("keywords") or "")
        return [p.strip() for p in raw.split(",") if p.strip()]

    def interest_plan(self) -> list[tuple[str, int]]:
        """顺序执行计划：(关键词, 每轮入库目标数)。未配置时回退到 keywords。"""
        plan = _parse_interest_items(self.config.get("interest_quotas"))
        if plan:
            return plan
        fallback = max(1, int(self.config.get("daily_per_keyword") or 3))
        return [(keyword, fallback) for keyword in self.keywords()]

    def interest_configured(self) -> bool:
        return bool(_parse_interest_items(self.config.get("interest_quotas")))

    def exclude(self) -> list[str]:
        raw = str(self.config.get("exclude_keywords") or "")
        return [p.strip() for p in raw.split(",") if p.strip()]

    def subtitle_page_limit(self) -> int:
        return int(self.config.get("subtitle_page_limit") or 3)

    def subtitle_max_chars(self) -> int:
        return int(self.config.get("subtitle_max_chars") or 12000)

    def verify_subtitle(self) -> bool:
        return bool(self.config.get("subtitle_verify", True))

    def max_duration(self) -> int:
        minutes = int(self.config.get("max_duration_minutes") or 0)
        return max(0, minutes) * 60

    def unlimited_mode(self) -> bool:
        return bool(self.config.get("unlimited_mode", False))

    def category_min_score(self) -> int:
        raw = self.config.get("category_min_score")
        if raw is None:
            return 80
        try:
            return max(0, min(100, int(raw)))
        except (TypeError, ValueError):
            return 80

    def consolidate_enabled(self) -> bool:
        return bool(self.config.get("consolidate_enabled", True))

    def consolidate_threshold(self) -> int:
        return max(1, int(self.config.get("consolidate_threshold") or 10))

    def consolidate_delete_sources(self) -> bool:
        return bool(self.config.get("consolidate_delete_sources", True))

    def audit_enabled(self) -> bool:
        return bool(self.config.get("audit_enabled", True))

    def audit_interval_days(self) -> int:
        return max(1, int(self.config.get("audit_interval_days") or 7))

    def audit_daily_limit(self) -> int:
        return max(0, int(self.config.get("audit_daily_limit") or 20))

    def audit_excerpt_chars(self) -> int:
        return max(500, int(self.config.get("audit_excerpt_chars") or 4000))

    def run_max_videos(self) -> int:
        return max(1, int(self.config.get("run_max_videos") or 100))

    def daily_quota(self, keyword: str) -> int:
        base = max(0, int(self.config.get("daily_per_keyword") or 0))
        raw = str(self.config.get("daily_quota_overrides") or "")
        for part in raw.replace("：", ":").split(","):
            name, sep, value = part.partition(":")
            if sep and name.strip() == keyword:
                try:
                    return max(0, int(float(value.strip())))
                except ValueError:
                    break
        return base

    def quota_remaining(self, keyword: str) -> int:
        if self.unlimited_mode() or not keyword:
            return 10 ** 9
        quota = self.daily_quota(keyword)
        if quota <= 0:
            return 10 ** 9
        return max(0, quota - self.store.quota_used(keyword))

    async def _subtitle_for(self, meta: dict[str, Any]) -> str:
        return await self.client.subtitles_for_pages(
            meta, self.subtitle_page_limit(), self.verify_subtitle()
        )

    async def _summarize(
        self, meta: dict[str, Any], subtitle: str, item: dict[str, Any] | None = None
    ) -> dict[str, str]:
        categories = self.keywords()
        material = "字幕" if subtitle else "标题与简介"
        excerpt = (
            sample_text(subtitle, self.subtitle_max_chars())
            if subtitle
            else clip(meta.get("desc") or "", 800)
        )
        fallback_title = clip(meta.get("title") or meta.get("bvid") or "", 30)
        fallback_category = guess_category(
            categories,
            str(meta.get("title") or ""),
            str(meta.get("desc") or ""),
            str((item or {}).get("keyword") or ""),
        )
        if self.llm is None:
            return {
                "summary": self._fallback_summary(meta, subtitle),
                "doc_title": fallback_title,
                "category": fallback_category,
                "material": material,
                "excerpt": excerpt,
            }
        raw = (
            await self.llm(
                SUMMARY_PROMPT.format(
                    title=meta.get("title") or "",
                    author=meta.get("author") or "",
                    tname=meta.get("tname") or "",
                    url=meta.get("url") or "",
                    desc=clip(meta.get("desc") or "", 600),
                    subtitle=excerpt or "（无字幕）",
                    categories="、".join(categories + [FALLBACK_CATEGORY]),
                )
            )
        ).strip()
        if not raw:
            raw = self._fallback_summary(meta, subtitle)
        doc_title, category, body = parse_summary(raw, fallback_title, categories)
        declared = declared_category(raw)
        score = declared_score(raw)
        min_score = self.category_min_score()
        if (
            category != FALLBACK_CATEGORY
            and score is not None
            and min_score > 0
            and score < min_score
        ):
            category = FALLBACK_CATEGORY
        if category == FALLBACK_CATEGORY and not declared and fallback_category != FALLBACK_CATEGORY:
            category = fallback_category
        return {
            "summary": body or raw,
            "doc_title": doc_title,
            "category": category,
            "material": material,
            "excerpt": excerpt,
        }

    async def _find_existing_doc(self, doc_name: str) -> str:
        if self.find_doc is None or not doc_name:
            return ""
        try:
            return str(await self.find_doc(doc_name) or "")
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _fallback_summary(meta: dict[str, Any], subtitle: str) -> str:
        if subtitle:
            return "要点：\n- " + clip(subtitle.replace("\n", " "), 200)
        return "要点：\n- " + clip(meta.get("desc") or meta.get("title") or "", 200)

    def _meta_kwargs(self, meta: dict[str, Any], item: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "title": str(meta.get("title") or ""),
            "author": str(meta.get("author") or ""),
            "duration": int(meta.get("duration") or 0),
            "source": str((item or {}).get("source") or ""),
            "keyword": str((item or {}).get("keyword") or ""),
        }

    async def process_one(self, item: dict[str, Any]) -> dict[str, Any]:
        bvid = str(item.get("bvid") or "")
        if not bvid:
            return {"ok": False, "status": "skipped", "reason": "no_bvid", "costly": False}
        async with self._bvid_lock(bvid):
            return await self._process_one(item, bvid)

    async def _process_one(self, item: dict[str, Any], bvid: str) -> dict[str, Any]:
        ok, why = self.store.should_process(bvid)
        if not ok:
            return {"ok": True, "status": "skipped", "reason": why, "bvid": bvid, "costly": False}
        try:
            meta = await self.client.view(bvid)
        except BiliRiskError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.store.mark_retryable(
                bvid, "failed", f"view_fail:{exc}", title=str(item.get("title") or "")
            )
            return {"ok": False, "status": "failed", "reason": "view_fail", "bvid": bvid, "error": str(exc), "costly": True}
        kwargs = self._meta_kwargs(meta, item)
        duration = kwargs["duration"]
        limit_seconds = self.max_duration()
        if limit_seconds and duration > limit_seconds:
            self.store.mark_excluded(bvid, "too_long", **kwargs)
            return {"ok": True, "status": "skipped", "reason": "too_long", "bvid": bvid, "costly": True}
        row = self.store.get(bvid)
        reusable = bool(
            row
            and row.get("status") == "deferred"
            and row.get("summary")
            and row.get("category")
            and row.get("doc_name")
        )
        if reusable:
            doc_name = str(row.get("doc_name"))
            info = {
                "summary": str(row.get("summary")),
                "doc_title": "",
                "category": str(row.get("category")),
                "material": str(row.get("material") or "缓存素材"),
                "excerpt": str(row.get("source_excerpt") or ""),
            }
        else:
            try:
                subtitle = await self._subtitle_for(meta)
            except BiliRiskError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.store.mark_retryable(bvid, "failed", f"subtitle_fail:{exc}", **kwargs)
                return {"ok": False, "status": "failed", "reason": "subtitle_fail", "bvid": bvid, "error": str(exc), "costly": True}
            missing = str(self.config.get("missing_subtitle") or "desc_only")
            if not subtitle and missing == "skip":
                self.store.mark_retryable(bvid, "no_subtitle", "no_subtitle", **kwargs)
                return {"ok": True, "status": "skipped", "reason": "no_subtitle", "bvid": bvid, "costly": True}
            if not worth_keeping(meta.get("title") or "", meta.get("desc") or "", subtitle, self.exclude()):
                self.store.mark_excluded(bvid, "excluded", **kwargs)
                return {"ok": True, "status": "skipped", "reason": "excluded", "bvid": bvid, "costly": True}
            try:
                info = await self._summarize(meta, subtitle, item)
            except Exception as exc:  # noqa: BLE001
                self.store.mark_retryable(bvid, "failed", f"llm_fail:{exc}", **kwargs)
                return {"ok": False, "status": "failed", "reason": "llm_fail", "bvid": bvid, "error": str(exc), "costly": True}
            categories = self.keywords()
            if categories and info["category"] not in categories:
                self.store.mark_excluded(bvid, "off_topic", summary=info["summary"], **kwargs)
                return {"ok": True, "status": "skipped", "reason": "off_topic", "bvid": bvid, "costly": True}
            doc_name = build_doc_name(info["category"], info["doc_title"])
        if self.quota_remaining(info["category"]) <= 0:
            self.store.mark_deferred(
                bvid,
                "quota_full",
                next_day_start_bj(),
                summary=info["summary"],
                doc_name=doc_name,
                category=info["category"],
                material=info["material"],
                source_excerpt=info.get("excerpt", ""),
                **kwargs,
            )
            return {
                "ok": True,
                "status": "skipped",
                "reason": "quota_full",
                "bvid": bvid,
                "category": info["category"],
                "costly": True,
            }
        if self.upload is None:
            return {"ok": False, "status": "failed", "reason": "no_kb", "bvid": bvid, "error": "knowledge base unavailable", "costly": True}
        existing_doc = await self._find_existing_doc(doc_name)
        if existing_doc:
            self.store.mark_ingested(
                bvid,
                doc_id=existing_doc,
                summary=info["summary"],
                doc_name=doc_name,
                category=info["category"],
                material=info["material"],
                source_excerpt=info.get("excerpt", ""),
                **kwargs,
            )
            return {
                "ok": True,
                "status": "ingested",
                "reason": "kb_duplicate",
                "bvid": bvid,
                "title": meta.get("title"),
                "doc_name": doc_name,
                "doc_id": existing_doc,
                "costly": True,
            }
        doc = render_document(
            meta, info["summary"], now_bj(), info["material"], info["category"], info["doc_title"]
        )
        try:
            doc_id = await self.upload(doc_name, doc, "md")
        except Exception as exc:  # noqa: BLE001
            self.store.mark_retryable(bvid, "failed", f"kb_fail:{exc}", **kwargs)
            return {"ok": False, "status": "failed", "reason": "kb_fail", "bvid": bvid, "error": str(exc), "costly": True}
        self.store.mark_ingested(
            bvid,
            doc_id=str(doc_id or ""),
            chars=len(doc),
            summary=info["summary"],
            doc_name=doc_name,
            category=info["category"],
            material=info["material"],
            source_excerpt=info.get("excerpt", ""),
            **kwargs,
        )
        self.store.quota_add(info["category"])
        return {
            "ok": True,
            "status": "ingested",
            "bvid": bvid,
            "title": meta.get("title"),
            "doc_name": doc_name,
            "category": info["category"],
            "chars": len(doc),
            "costly": True,
        }

    async def read_one(self, reference: str, ingest: bool = True) -> dict[str, Any]:
        try:
            bvid = await self.client.resolve_bvid(reference)
        except BiliRiskError:
            raise
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": "resolve_fail", "message": f"链接解析失败：{exc}"}
        if not bvid:
            return {
                "ok": False,
                "reason": "bad_reference",
                "message": "没有识别出 B 站视频，支持 BV 号、av 号、bilibili.com 链接和 b23.tv 短链。",
            }
        async with self._bvid_lock(bvid):
            return await self._read_one(bvid, ingest)

    async def _read_one(self, bvid: str, ingest: bool) -> dict[str, Any]:
        url = f"https://www.bilibili.com/video/{bvid}"
        row = self.store.get(bvid)
        if row and row.get("summary") and row.get("status") in ("ingested", "excluded"):
            status = str(row.get("status"))
            return {
                "ok": True,
                "cached": True,
                "bvid": bvid,
                "title": row.get("title") or bvid,
                "author": row.get("author") or "",
                "url": url,
                "summary": row.get("summary"),
                "material": "已入库摘要" if status == "ingested" else "历史摘要（未入库）",
                "category": row.get("category") or "",
                "doc_name": row.get("doc_name") or "",
                "doc_id": row.get("doc_id") or "",
                "ingested": status == "ingested",
                "excluded": status == "excluded",
                "off_topic": str(row.get("reason") or "") == "off_topic",
                "reason": str(row.get("reason") or ""),
            }
        try:
            meta = await self.client.view(bvid)
        except BiliRiskError:
            raise
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": "view_fail", "message": f"获取视频信息失败：{exc}"}
        reusable = bool(
            row
            and row.get("status") == "deferred"
            and row.get("summary")
            and row.get("category")
            and row.get("doc_name")
        )
        if reusable:
            info = {
                "summary": str(row.get("summary")),
                "doc_title": "",
                "category": str(row.get("category")),
                "material": str(row.get("material") or "缓存素材"),
                "excerpt": str(row.get("source_excerpt") or ""),
            }
            doc_name = str(row.get("doc_name"))
            excluded = False
            off_topic = False
        else:
            try:
                subtitle = await self._subtitle_for(meta)
            except BiliRiskError:
                raise
            except Exception:  # noqa: BLE001
                subtitle = ""
            missing = str(self.config.get("missing_subtitle") or "desc_only")
            if not subtitle and missing == "skip":
                return {
                    "ok": False,
                    "reason": "no_subtitle",
                    "bvid": bvid,
                    "title": meta.get("title"),
                    "message": f"《{meta.get('title') or bvid}》没有可用字幕，无法总结。",
                }
            try:
                info = await self._summarize(meta, subtitle, None)
            except BiliRiskError:
                raise
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "reason": "llm_fail", "bvid": bvid, "title": meta.get("title"), "message": f"摘要生成失败：{exc}"}
            excluded = not worth_keeping(meta.get("title") or "", meta.get("desc") or "", subtitle, self.exclude())
            categories = self.keywords()
            off_topic = bool(categories) and info["category"] not in categories
            doc_name = build_doc_name(info["category"], info["doc_title"])
        doc_id = ""
        ingested = False
        already_ingested = bool(row and row.get("status") == "ingested")
        if already_ingested:
            self.store.update_summary(bvid, info["summary"], doc_name)
            doc_id = str(row.get("doc_id") or "")
            ingested = True
        elif ingest and self.upload is not None and not excluded and not off_topic:
            existing_doc = await self._find_existing_doc(doc_name)
            if existing_doc:
                self.store.mark_ingested(
                    bvid,
                    title=str(meta.get("title") or ""),
                    author=str(meta.get("author") or ""),
                    duration=int(meta.get("duration") or 0),
                    doc_id=existing_doc,
                    summary=info["summary"],
                    doc_name=doc_name,
                    category=info["category"],
                    material=info["material"],
                    source_excerpt=info.get("excerpt", ""),
                    source="on_demand",
                )
                doc_id = existing_doc
                ingested = True
            else:
                doc = render_document(
                    meta, info["summary"], now_bj(), info["material"], info["category"], info["doc_title"]
                )
                try:
                    doc_id = await self.upload(doc_name, doc, "md")
                    self.store.mark_ingested(
                        bvid,
                        title=str(meta.get("title") or ""),
                        author=str(meta.get("author") or ""),
                        duration=int(meta.get("duration") or 0),
                        doc_id=str(doc_id or ""),
                        chars=len(doc),
                        summary=info["summary"],
                        doc_name=doc_name,
                        category=info["category"],
                        material=info["material"],
                        source_excerpt=info.get("excerpt", ""),
                        source="on_demand",
                    )
                    ingested = True
                except Exception as exc:  # noqa: BLE001
                    self.store.mark_retryable(
                        bvid,
                        "failed",
                        f"kb_fail:{exc}",
                        title=str(meta.get("title") or ""),
                        author=str(meta.get("author") or ""),
                        duration=int(meta.get("duration") or 0),
                        source="on_demand",
                    )
        elif row is None and (excluded or off_topic):
            self.store.mark_excluded(
                bvid,
                "excluded" if excluded else "off_topic",
                title=str(meta.get("title") or ""),
                author=str(meta.get("author") or ""),
                duration=int(meta.get("duration") or 0),
                source="on_demand",
                summary=info["summary"],
                category=info["category"],
            )
        return {
            "ok": True,
            "cached": False,
            "bvid": bvid,
            "title": meta.get("title"),
            "author": meta.get("author"),
            "url": url,
            "summary": info["summary"],
            "material": info["material"],
            "category": info["category"],
            "doc_name": doc_name if ingested else "",
            "excluded": excluded,
            "off_topic": off_topic,
            "reason": "excluded" if excluded else ("off_topic" if off_topic else ""),
            "ingested": ingested,
            "doc_id": doc_id,
        }

    async def consolidate(self, keyword: str = "", force: bool = False) -> dict[str, Any]:
        async with self._digest_lock:
            return await self._consolidate_locked(keyword, force)

    async def _consolidate_locked(self, keyword: str = "", force: bool = False) -> dict[str, Any]:
        if self.llm is None or self.upload is None:
            return {"ok": False, "reason": "unavailable"}
        if not force and not self.consolidate_enabled():
            return {"ok": True, "reason": "disabled"}
        counts = self.store.unmerged_counts()
        if not counts:
            return {"ok": True, "reason": "nothing"}
        if keyword:
            if keyword not in counts:
                return {"ok": True, "reason": "nothing"}
            order = [(keyword, counts[keyword])]
        else:
            order = list(counts.items())
        chosen: tuple[str, int, dict[str, Any]] | None = None
        last_reason = "nothing"
        for candidate_keyword, candidate_count in order:
            if not force and candidate_count < self.consolidate_threshold():
                last_reason = "below_threshold"
                continue
            candidate_digest = self.store.get_digest(candidate_keyword) or {}
            if (
                not force
                and candidate_digest.get("last_error")
                and int(candidate_digest.get("last_attempt_at") or 0) > now_ts() - 86400
            ):
                last_reason = "cooldown"
                continue
            chosen = (candidate_keyword, candidate_count, candidate_digest)
            break
        if chosen is None:
            return {"ok": True, "reason": last_reason}
        keyword, _count, digest = chosen
        sources = self.store.unmerged_sources(keyword, limit=30)
        if not sources:
            return {"ok": True, "reason": "nothing"}
        existing = str(digest.get("content") or "")
        existing_input = (
            sample_text(existing, max(4000, self.subtitle_max_chars() * 3)) if existing else ""
        )
        sources_text = "\n\n".join(
            f"[{s['bvid']}] {s.get('title') or ''}\n{s.get('summary') or ''}" for s in sources
        )
        sources_text = sample_text(sources_text, max(4000, self.subtitle_max_chars() * 2))
        round_no = int(digest.get("rounds") or 0) + 1
        date_str = now_bj()
        points = (
            await self.llm(
                DIGEST_PROMPT.format(
                    keyword=keyword, existing=existing_input or "（暂无）", sources=sources_text
                )
            )
        ).strip()
        if not points:
            self.store.save_digest(
                keyword,
                doc_name=str(digest.get("doc_name") or ""),
                doc_id=str(digest.get("doc_id") or ""),
                rounds=int(digest.get("rounds") or 0),
                sources=int(digest.get("sources") or 0),
                content=existing,
                error="empty_points",
            )
            return {"ok": False, "reason": "empty_points", "keyword": keyword}
        review_llm = self._review_llm()
        review = (
            await review_llm(
                DIGEST_REVIEW_PROMPT.format(
                    points=points,
                    sources=sample_text(sources_text, REVIEW_SOURCES_CHARS),
                )
            )
        ).strip()
        verdict, problems = review_verdict(review)
        if is_suspect_verdict(verdict):
            self.store.save_digest(
                keyword,
                doc_name=str(digest.get("doc_name") or ""),
                doc_id=str(digest.get("doc_id") or ""),
                rounds=int(digest.get("rounds") or 0),
                sources=int(digest.get("sources") or 0),
                content=existing,
                error=problems or verdict,
            )
            return {
                "ok": False,
                "reason": "review_failed",
                "keyword": keyword,
                "problems": problems[:400],
            }
        bvids = [str(s["bvid"]) for s in sources]
        section = render_digest_section(round_no, date_str, bvids, points)
        sections = f"{existing}\n\n{section}".strip() if existing else section
        doc_name = digest_doc_name(keyword)
        content = render_digest(
            keyword,
            sections,
            round_no,
            date_str,
            int(digest.get("sources") or 0) + len(sources),
        )
        doc_id = await self.upload(doc_name, content, "md")
        old_doc_id = str(digest.get("doc_id") or "")
        if old_doc_id and old_doc_id != doc_id and self.delete_doc is not None:
            try:
                await self.delete_doc(old_doc_id)
            except Exception:  # noqa: BLE001
                pass
        self.store.save_digest(
            keyword,
            doc_name=doc_name,
            doc_id=str(doc_id or ""),
            rounds=round_no,
            sources=int(digest.get("sources") or 0) + len(sources),
            content=sections,
        )
        self.store.mark_merged(bvids, round_no)
        deleted = 0
        if self.consolidate_delete_sources() and self.delete_doc is not None:
            for source in sources:
                source_doc = str(source.get("doc_id") or "")
                if not source_doc:
                    continue
                if self.store.doc_id_ref_count(source_doc) > 1:
                    continue
                try:
                    await self.delete_doc(source_doc)
                    deleted += 1
                except Exception:  # noqa: BLE001
                    pass
        return {
            "ok": True,
            "keyword": keyword,
            "round": round_no,
            "sources": len(sources),
            "deleted": deleted,
            "doc_name": doc_name,
            "doc_id": str(doc_id or ""),
        }

    async def audit_due_docs(self, limit: int = 0, force: bool = False) -> dict[str, Any]:
        async with self._audit_lock:
            return await self._audit_locked(limit, force)

    async def _audit_locked(self, limit: int = 0, force: bool = False) -> dict[str, Any]:
        audit_llm = self._review_llm()
        if audit_llm is None:
            return {"ok": False, "reason": "no_llm", "audited": 0}
        if not force and not self.audit_enabled():
            return {"ok": True, "reason": "disabled", "audited": 0}
        if limit > 0:
            budget = limit
        else:
            used = self.store.audited_since(today_start_ts())
            budget = max(0, self.audit_daily_limit() - used)
        if budget <= 0:
            return {"ok": True, "reason": "daily_limit", "audited": 0}
        interval = self.audit_interval_days()
        results: list[dict[str, Any]] = []
        for row in self.store.audit_due_videos(interval, budget):
            excerpt = str(row.get("source_excerpt") or "").strip()
            try:
                text = await audit_llm(
                    AUDIT_PROMPT.format(
                        summary=str(row.get("summary") or ""),
                        excerpt=clip(excerpt, self.audit_excerpt_chars()) or "（没有留存素材）",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                self.store.mark_audit(str(row["bvid"]), "error", str(exc))
                results.append({"bvid": row["bvid"], "status": "error"})
                continue
            verdict, problems = review_verdict(text)
            status = "suspect" if is_suspect_verdict(verdict) else "ok"
            self.store.mark_audit(str(row["bvid"]), status, problems)
            results.append({"bvid": row["bvid"], "status": status, "note": problems[:120]})
        remaining = max(0, budget - len(results))
        if remaining > 0:
            for row in self.store.audit_due_digests(interval, remaining):
                keyword = str(row["keyword"])
                source_text = "\n\n".join(
                    f"[{s['bvid']}] {s.get('summary') or ''}"
                    for s in self.store.digest_sources(keyword)
                )
                source_text = sample_text(source_text, max(4000, self.audit_excerpt_chars() * 3))
                try:
                    text = await audit_llm(
                        AUDIT_PROMPT.format(
                            summary=sample_text(
                                str(row.get("content") or ""),
                                max(4000, self.subtitle_max_chars() * 2),
                            ),
                            excerpt=source_text or "（没有留存来源摘要）",
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    self.store.mark_digest_audit(keyword, "error", str(exc))
                    results.append({"keyword": keyword, "status": "error"})
                    continue
                verdict, problems = review_verdict(text)
                status = "suspect" if is_suspect_verdict(verdict) else "ok"
                self.store.mark_digest_audit(keyword, status, problems)
                results.append({"keyword": keyword, "status": status, "note": problems[:120]})
        return {
            "ok": True,
            "audited": len(results),
            "suspect": sum(1 for item in results if item.get("status") == "suspect"),
            "details": results,
        }

    async def _run_keyword(
        self, keyword: str, target: int, run_id: str, budget: int
    ) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "processed": 0,
            "ingested": 0,
            "skipped": 0,
            "failed": 0,
            "details": [],
            "aborted": "",
            "consecutive_fail": 0,
        }
        got = 0
        for page in range(1, 4):
            if got >= target or stats["processed"] >= budget:
                break
            try:
                candidates = await self.client.search_videos(keyword, page)
            except BiliRiskError as exc:
                stats["aborted"] = "rate_limited"
                self.store.add_event(
                    run_id, "keyword", "failed", f"「{keyword}」搜索被风控中止：{exc}"
                )
                return stats
            except Exception as exc:  # noqa: BLE001
                stats["failed"] += 1
                self.store.add_event(run_id, "keyword", "failed", f"「{keyword}」搜索失败：{exc}")
                break
            if not candidates:
                break
            for item in candidates:
                if got >= target or stats["processed"] >= budget:
                    break
                bvid = str(item.get("bvid") or "")
                if not bvid:
                    continue
                ok, why = self.store.should_process(bvid)
                if not ok:
                    stats["skipped"] += 1
                    self.store.add_event(run_id, "video", "skipped", f"不处理（{why}）", bvid=bvid)
                    continue
                self.store.add_event(run_id, "video", "started", f"「{keyword}」开始处理", bvid=bvid)
                try:
                    result = await self.process_one(item)
                except BiliRiskError as exc:
                    stats["aborted"] = "rate_limited"
                    self.store.add_event(run_id, "video", "failed", f"风控中止：{exc}", bvid=bvid)
                    return stats
                if not result.get("costly", True):
                    stats["skipped"] += 1
                    self.store.add_event(run_id, "video", "skipped", str(result.get("reason") or "跳过"), bvid=bvid)
                    continue
                stats["processed"] += 1
                stats["details"].append(result)
                status = result.get("status")
                if status == "ingested":
                    stats["ingested"] += 1
                    got += 1
                    stats["consecutive_fail"] = 0
                    self.store.add_event(run_id, "video", "ingested", "已写入知识库", bvid=bvid, detail=str(result))
                elif result.get("ok") is False:
                    stats["failed"] += 1
                    stats["consecutive_fail"] += 1
                    self.store.add_event(run_id, "video", "failed", str(result.get("reason") or "失败"), bvid=bvid, detail=str(result))
                    if stats["consecutive_fail"] >= 2:
                        stats["aborted"] = "consecutive_fail"
                        return stats
                else:
                    stats["skipped"] += 1
                    stats["consecutive_fail"] = 0
                    self.store.add_event(run_id, "video", "skipped", str(result.get("reason") or "跳过"), bvid=bvid)
                    if result.get("reason") == "quota_full":
                        stats["deferred"] = int(stats.get("deferred") or 0) + 1
                        if stats["deferred"] >= 2:
                            self.store.add_event(
                                run_id, "keyword", "skipped", f"「{keyword}」归类配额已满，提前停止该词"
                            )
                            return stats
        if got >= target:
            self.store.add_event(run_id, "keyword", "done", f"「{keyword}」已达标 {got}/{target}")
        else:
            self.store.add_event(run_id, "keyword", "done", f"「{keyword}」实际入库 {got}/{target}（候选不足）")
        return stats

    async def run(self, trigger: str = "manual", kb_id: str = "", embedding_provider: str = "", rerank_provider: str = "") -> dict[str, Any]:
        if not self.config.get("enabled", True):
            return {"ok": True, "skipped": True, "reason": "disabled"}
        run_id = new_run_id()
        self.store.start_run(run_id, trigger, kb_id, embedding_provider, rerank_provider)
        plan = self.interest_plan()
        self.store.add_event(
            run_id,
            "config",
            "info",
            "兴趣词计划：" + ("、".join(f"{keyword}×{quota}" for keyword, quota in plan) or "无"),
        )
        processed = ingested = skipped = failed = 0
        details: list[dict[str, Any]] = []
        aborted = ""
        rounds = 0
        max_total = self.run_max_videos()
        max_rounds = 20
        try:
            if not plan:
                self.store.add_event(run_id, "collect", "warning", "没有配置兴趣词，本轮无事可做")
            while plan and not aborted:
                rounds += 1
                round_ingested = 0
                self.store.add_event(run_id, "round", "started", f"第 {rounds} 轮开始")
                for keyword, round_quota in plan:
                    if processed >= max_total:
                        aborted = "total_limit"
                        break
                    target = round_quota
                    if not self.unlimited_mode():
                        target = min(target, self.quota_remaining(keyword))
                    if target <= 0:
                        skipped += 1
                        self.store.add_event(
                            run_id, "keyword", "skipped", f"「{keyword}」目标为 0（每日配额已满）"
                        )
                        continue
                    self.store.add_event(run_id, "keyword", "started", f"「{keyword}」目标 {target} 条")
                    stats = await self._run_keyword(keyword, target, run_id, max_total - processed)
                    processed += stats["processed"]
                    ingested += stats["ingested"]
                    skipped += stats["skipped"]
                    failed += stats["failed"]
                    round_ingested += stats["ingested"]
                    details.extend(stats["details"])
                    if stats.get("aborted"):
                        aborted = stats["aborted"]
                        break
                if aborted:
                    break
                if round_ingested == 0:
                    self.store.add_event(run_id, "round", "done", f"第 {rounds} 轮无新增入库，结束")
                    break
                if not self.unlimited_mode():
                    remaining = [self.quota_remaining(keyword) for keyword, _ in plan]
                    if all(value <= 0 for value in remaining):
                        self.store.add_event(run_id, "round", "done", "每日配额已刷完，结束")
                        break
                if rounds >= max_rounds:
                    aborted = "round_limit"
                    break
                self.store.add_event(
                    run_id, "round", "done", f"第 {rounds} 轮入库 {round_ingested} 条，继续下一轮"
                )
            digest_result: dict[str, Any] = {}
            audit_result: dict[str, Any] = {}
            try:
                digest_result = await self.consolidate()
                if digest_result.get("round"):
                    self.store.add_event(
                        run_id,
                        "digest",
                        "ok",
                        f"汇总「{digest_result['keyword']}」第 {digest_result.get('round')} 轮，"
                        f"来源 {digest_result.get('sources')} 条，删除原文档 {digest_result.get('deleted')} 份",
                        detail=str(digest_result),
                    )
                elif digest_result.get("reason") == "review_failed":
                    self.store.add_event(
                        run_id,
                        "digest",
                        "warning",
                        f"汇总「{digest_result.get('keyword')}」复审未通过，本轮未写入",
                        detail=str(digest_result.get("problems") or ""),
                    )
            except Exception as exc:  # noqa: BLE001
                self.store.add_event(run_id, "digest", "failed", f"汇总失败：{exc}")
            try:
                audit_result = await self.audit_due_docs()
                if audit_result.get("audited"):
                    self.store.add_event(
                        run_id,
                        "audit",
                        "ok",
                        f"审核 {audit_result['audited']} 篇，可疑 {audit_result.get('suspect', 0)} 篇",
                        detail=str(audit_result.get("details"))[:800],
                    )
            except Exception as exc:  # noqa: BLE001
                self.store.add_event(run_id, "audit", "failed", f"审核失败：{exc}")
            if aborted == "rate_limited":
                run_status = "rate_limited"
            elif failed == 0 and not aborted:
                run_status = "completed"
            else:
                run_status = "partial"
            self.store.finish_run(
                run_id,
                run_status,
                processed,
                processed,
                ingested,
                skipped,
                failed,
                f"trigger={trigger} rounds={rounds}{' abort=' + aborted if aborted else ''}",
            )
            return {
                "ok": True,
                "run_id": run_id,
                "rounds": rounds,
                "processed": processed,
                "ingested": ingested,
                "skipped": skipped,
                "failed": failed,
                "aborted": aborted,
                "digest": digest_result,
                "audit": audit_result,
                "details": details[-50:],
                "counts": self.store.counts(),
            }
        except Exception as exc:  # noqa: BLE001
            self.store.finish_run(run_id, "failed", 0, 0, 0, 0, 1, str(exc))
            return {"ok": False, "run_id": run_id, "error": str(exc), "counts": self.store.counts()}
