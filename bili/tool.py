"""LLM 工具 bilibili_read：按需读取单个视频并返回摘要素材。"""

from __future__ import annotations

from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_context import AstrAgentContext


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
