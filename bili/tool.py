"""LLM 工具：bilibili_read 按需读单个视频，bilibili_knowledge 查询已学知识库。"""

from __future__ import annotations

from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_context import AstrAgentContext

from .query import format_for_llm, unwrap_arguments


@dataclass(config=dict(arbitrary_types_allowed=True))
class BilibiliReadTool(FunctionTool[AstrAgentContext]):
    name: str = "bilibili_read"
    description: str = (
        "读取一个 B 站视频的公开信息与字幕，生成中文要点摘要，并可写入知识库。"
        "当用户发送 B 站链接、BV/AV 号，或要求总结某个 B 站视频内容时调用。"
        "参数 reference 支持 BV 号、av 号、bilibili.com 链接、b23.tv 短链。"
        "返回的是摘要素材，请按你的人格自然回复，不要照抄返回内容。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "B 站视频链接、BV 号或 av 号，例如 BV1GJ411x7h7 或 https://b23.tv/xxxxxxx",
                },
            },
            "required": ["reference"],
        }
    )
    pipeline: Any = None
    ingest: bool = True

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        kwargs = unwrap_arguments(kwargs)
        reference = str(kwargs.get("reference") or kwargs.get("bvid") or "").strip()
        if not reference:
            return "没有收到视频链接或 BV 号。"
        if self.pipeline is None:
            return "插件内部错误：pipeline 未注入。"
        try:
            result = await self.pipeline.read_one(reference, ingest=self.ingest)
        except Exception as exc:  # noqa: BLE001
            return f"读取视频失败：{exc}"
        if not result.get("ok"):
            return str(result.get("message") or "无法读取该视频。")
        lines = [
            f"《{result.get('title') or result.get('bvid')}》",
            f"UP：{result.get('author') or '未知'}",
            f"链接：{result.get('url')}",
        ]
        if result.get("category"):
            lines.append(f"归类：{result.get('category')}")
        if result.get("cached"):
            lines.append("（使用了已入库的摘要）")
        lines.append(f"素材来源：{result.get('material') or '未知'}")
        if result.get("off_topic"):
            lines.append("（不属于兴趣关键词，未写入知识库）")
        elif result.get("excluded"):
            reason = str(result.get("reason") or "").strip()
            lines.append(f"（未写入知识库：{reason or '已被排除'}）")
        elif result.get("ingested"):
            if result.get("doc_name"):
                lines.append(f"（已写入知识库：{result.get('doc_name')}）")
            else:
                lines.append("（已写入知识库）")
        else:
            lines.append("（未写入知识库）")
        lines.extend(
            [
                "",
                str(result.get("summary") or "").strip(),
                "",
                "以上内容来自公开视频摘要，不是亲历。请用你的人格自然地回复用户，不要原样朗读。",
            ]
        )
        return "\n".join(lines)


@dataclass(config=dict(arbitrary_types_allowed=True))
class BilibiliKnowledgeTool(FunctionTool[AstrAgentContext]):
    name: str = "bilibili_knowledge"
    description: str = (
        "查询 Bili Learn 知识库（由 B 站公开视频自动学习、汇总生成）。"
        "当用户询问某个主题、让你介绍或总结学过的知识、问「你学过什么」，"
        "或你需要引用之前学习的内容时调用。"
        "query 填要查的问题或关键词；想读某篇文档的完整内容时，"
        "把检索结果里的文档名填进 doc_name；两者都留空则返回知识库概览（已学主题列表）。"
        "可以连续调用：先看概览找主题，再检索，再读全文，直到能回答用户。"
        "回答时必须基于返回内容，不要编造；内容来自视频摘要，不是亲历。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要查询的问题或关键词，例如「构图技巧」「提示词怎么写」；留空则返回知识库概览。",
                },
                "doc_name": {
                    "type": "string",
                    "description": "可选：要读全文的文档名（先检索拿到文档名再读）。填了它时忽略 query。",
                },
                "top_k": {
                    "type": "integer",
                    "description": "可选：返回几条检索结果，默认跟随插件配置（5 条，上限 10）。",
                },
            },
            "required": [],
        }
    )
    query_knowledge: Any = None

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        kwargs = unwrap_arguments(kwargs)
        query = str(kwargs.get("query") or "").strip()
        doc_name = str(kwargs.get("doc_name") or "").strip()
        try:
            top_k = int(kwargs.get("top_k") or 0)
        except (TypeError, ValueError):
            top_k = 0
        if self.query_knowledge is None:
            return "插件内部错误：知识库查询未注入。"
        try:
            result = await self.query_knowledge(query=query, top_k=top_k, doc_name=doc_name)
        except Exception as exc:  # noqa: BLE001
            return f"知识库查询失败：{exc}"
        return format_for_llm(result)
