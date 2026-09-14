"""知识库查询的结果处理：工具参数解包 + 给模型/人看的格式化。

纯函数模块，不依赖 AstrBot，可离线测试。
"""

from __future__ import annotations

import json
from typing import Any

NO_EXPERIENCE_NOTE = (
    "以上内容来自 B 站公开视频摘要，不是亲历；请按你的人格自然回答，不要照抄原文。"
)

LLM_ITEM_CHARS = 900
LLM_DOC_CHARS = 6000
LLM_TOTAL_CHARS = 6000
CHAT_ITEM_CHARS = 160
CHAT_DOC_CHARS = 600


def unwrap_arguments(kwargs: dict[str, Any]) -> dict[str, Any]:
    """兼容模型把参数多包一层 arguments 的情况（dict 或 JSON 字符串，可多层）。

    AstrBot 核心把模型返回的参数原样传给 handler，部分模型会输出
    {'arguments': {'query': ...}} 这种包装，导致 handler 拿不到参数。
    """
    if not isinstance(kwargs, dict):
        return {}
    current: dict[str, Any] = dict(kwargs)
    for _ in range(4):
        if "arguments" not in current:
            break
        inner = current.get("arguments")
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except (TypeError, ValueError):
                break
        if not isinstance(inner, dict):
            break
        merged = {key: value for key, value in current.items() if key != "arguments"}
        merged.update(inner)
        current = merged
    return current


def clamp(text: Any, limit: int) -> str:
    value = str(text or "").strip()
    if limit <= 0 or len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def video_url(bvid: Any) -> str:
    bvid = str(bvid or "").strip()
    return f"https://www.bilibili.com/video/{bvid}" if bvid else ""


def format_for_llm(result: Any) -> str:
    """给 AI 工具返回的文本：带使用说明与非亲历声明。"""
    if not isinstance(result, dict):
        return "查询没有返回可用结果。"
    if result.get("ok") is False:
        return str(result.get("note") or "没有找到相关内容。")
    mode = str(result.get("mode") or "")
    if mode == "overview":
        return _overview_text(result)
    if mode == "doc":
        return _doc_text(result)
    if mode == "empty":
        return str(result.get("note") or "知识库和本地摘要库都没有相关内容。")
    return _search_text(result)


def format_for_chat(result: Any) -> str:
    """给管理员命令用的纯文本：紧凑、带相关度和 BV 链接。"""
    if not isinstance(result, dict):
        return "查询没有返回可用结果。"
    if result.get("ok") is False:
        return str(result.get("note") or "没有找到相关内容。")
    mode = str(result.get("mode") or "")
    if mode == "overview":
        return _overview_text(result)
    if mode == "doc":
        content = str(result.get("content") or "")
        body = clamp(content, CHAT_DOC_CHARS)
        head = f"文档「{result.get('doc_name') or ''}」：共 {len(content)} 字"
        if len(content) > len(body):
            head += "（只显示前部分，AI 工具可读全文）"
        return f"{head}\n\n{body}"
    if mode == "empty":
        return str(result.get("note") or "知识库和本地摘要库都没有相关内容。")
    items = [item for item in (result.get("items") or []) if isinstance(item, dict)]
    source = "知识库语义检索" if str(result.get("source") or "") == "kb" else "本地摘要库"
    lines = [f"查询「{result.get('query') or ''}」：{source}命中 {len(items)} 条"]
    if result.get("note"):
        lines.append(str(result["note"]))
    if not items:
        lines.append("没有找到相关内容。")
        return "\n".join(lines)
    for index, item in enumerate(items, 1):
        lines.append(_chat_item_block(index, item))
    return "\n".join(lines)


def _overview_text(result: dict[str, Any]) -> str:
    counts = result.get("counts") or {}
    lines = [
        "【知识库概览】",
        "已入库视频 {ingested} 条，排除 {excluded} 条，无字幕 {no_subtitle} 条，"
        "失败 {failed} 条；汇总文档 {digests} 份。".format(
            ingested=counts.get("ingested", 0),
            excluded=counts.get("excluded", 0),
            no_subtitle=counts.get("no_subtitle", 0),
            failed=counts.get("failed", 0),
            digests=counts.get("digests", 0),
        ),
    ]
    categories = [row for row in (result.get("categories") or []) if isinstance(row, dict)]
    if categories:
        lines.append(
            "分区："
            + "、".join(
                f"{row.get('category')} {row.get('count')}" for row in categories[:12]
            )
        )
    digests = [row for row in (result.get("digests") or []) if isinstance(row, dict)]
    if digests:
        lines.append(
            "汇总主题："
            + "、".join(
                f"{row.get('keyword')}（{row.get('rounds', 0)} 轮 / {row.get('sources', 0)} 条）"
                for row in digests[:10]
            )
        )
    recent = [row for row in (result.get("recent") or []) if isinstance(row, dict)]
    if recent:
        lines.append(
            "最近入库："
            + "；".join(
                f"{row.get('bvid')}《{clamp(row.get('title'), 24)}》" for row in recent[:8]
            )
        )
    if not categories and not digests and not recent:
        lines.append("知识库还是空的，还没有学过任何内容。")
    lines.append(
        "需要具体内容时，用 query 检索；想读某篇文档的完整内容，"
        "把检索结果里的文档名填进 doc_name。"
    )
    return "\n".join(lines)


def _search_text(result: dict[str, Any]) -> str:
    items = [item for item in (result.get("items") or []) if isinstance(item, dict)]
    source = "知识库语义检索" if str(result.get("source") or "") == "kb" else "本地摘要库关键词匹配"
    lines = [f"【知识检索】关键词：{result.get('query') or ''}（{source}，{len(items)} 条）"]
    if result.get("note"):
        lines.append(str(result["note"]))
    if not items:
        lines.append("没有找到相关内容。")
        return "\n".join(lines)
    used = 0
    for index, item in enumerate(items, 1):
        block = _llm_item_block(index, item)
        if index > 1 and used + len(block) > LLM_TOTAL_CHARS:
            lines.append("（结果较多，已省略后面的内容；需要更精确时可以换关键词再查。）")
            break
        lines.append(block)
        used += len(block)
    lines.append(NO_EXPERIENCE_NOTE)
    return "\n".join(lines)


def _llm_item_block(index: int, item: dict[str, Any]) -> str:
    kind = str(item.get("kind") or "kb")
    if kind == "video":
        title = str(item.get("title") or item.get("doc_name") or "未命名视频")
        mark = "已入库" if str(item.get("status") or "") == "ingested" else "未入库"
        body = clamp(item.get("summary"), LLM_ITEM_CHARS)
        url = video_url(item.get("bvid"))
        if url:
            body = f"{body}\n来源：{url}"
        return f"【素材 {index}】{title}（本地摘要，{mark}）\n{body}"
    if kind == "digest":
        title = str(item.get("doc_name") or item.get("keyword") or "汇总文档")
        body = clamp(item.get("content"), LLM_ITEM_CHARS)
        return f"【素材 {index}】汇总文档：{title}\n{body}"
    title = str(item.get("doc_name") or "未命名文档")
    body = clamp(item.get("content"), LLM_ITEM_CHARS)
    return f"【素材 {index}】{title}（相关度 {_score_text(item.get('score'))}）\n{body}"


def _chat_item_block(index: int, item: dict[str, Any]) -> str:
    kind = str(item.get("kind") or "kb")
    if kind == "video":
        title = str(item.get("title") or item.get("doc_name") or "未命名视频")
        mark = "已入库" if str(item.get("status") or "") == "ingested" else "未入库"
        body = clamp(item.get("summary"), CHAT_ITEM_CHARS)
        url = video_url(item.get("bvid"))
        tail = f"\n   {url}" if url else ""
        return f"{index}. {title}（{mark}）\n   {body}{tail}"
    if kind == "digest":
        title = str(item.get("doc_name") or item.get("keyword") or "汇总文档")
        body = clamp(item.get("content"), CHAT_ITEM_CHARS)
        return f"{index}. 汇总：{title}\n   {body}"
    title = str(item.get("doc_name") or "未命名文档")
    body = clamp(item.get("content"), CHAT_ITEM_CHARS)
    return f"{index}. [{_score_text(item.get('score'))}] {title}\n   {body}"


def _doc_text(result: dict[str, Any]) -> str:
    content = str(result.get("content") or "")
    body = clamp(content, LLM_DOC_CHARS)
    lines = [f"【文档全文】{result.get('doc_name') or ''}", body]
    if len(content) > len(body):
        lines.append("（文档过长，只读取了前部分；可改用 query 检索具体问题。）")
    lines.append(NO_EXPERIENCE_NOTE)
    return "\n".join(lines)


def _score_text(score: Any) -> str:
    try:
        return f"{float(score):.2f}"
    except (TypeError, ValueError):
        return "?"
