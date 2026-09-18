"""Turn a video + subtitle/desc into a short knowledge-base document."""

from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from typing import Any

BJ = timezone(timedelta(hours=8))
FALLBACK_CATEGORY = "其他"

DOC_TITLE_RE = re.compile(r"^\s*标题\s*[:：]\s*(.+?)\s*$", re.M)
DOC_CATEGORY_RE = re.compile(r"^\s*分区\s*[:：]\s*(.+?)\s*$", re.M)
DOC_SCORE_RE = re.compile(r"^\s*相关度\s*[:：]\s*(\d{1,3})\s*%?\s*$", re.M)
_BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')

SUMMARY_PROMPT = """你是视频摘要员。根据标题、UP、分区、简介和字幕（可能不完整）写一份给知识库用的短文。
输出格式（严格按下面四部分，不要 JSON，不要代码块）：
标题：<10-20 字的中文概括，不要书名号，不要 bvid，不要引号>
分区：<从可选分区里选最贴切的一个；都不合适就写 其他>
相关度：<0-100，你给「分区」与视频内容相关程度的打分>
空一行后，写 3 到 7 条要点，每条一行，以「- 」开头。

要求：
- 用中文。
- 标明这是视频摘要，不是亲历。
- 不确定的写成「视频中提到…」，不要编造数字和结论。
- 不要复制整段字幕。
- 不要出现 SESSDATA、Cookie、密钥。
可选分区：{categories}

标题：{title}
UP：{author}
分区：{tname}
链接：{url}
简介：
{desc}
字幕摘录：
{subtitle}
"""


def clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def sanitize_doc_title(text: str, limit: int = 30) -> str:
    cleaned = _BAD_NAME_CHARS.sub(" ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" 　.-_")
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip()
    return cleaned or "未命名视频"


def declared_category(raw: str) -> str:
    """返回模型输出里显式声明的分区；没写则返回空串。"""
    match = DOC_CATEGORY_RE.search(raw or "")
    return match.group(1).strip() if match else ""


def declared_score(raw: str) -> int | None:
    """返回模型给的相关度分数（0-100）；没写或非法则返回 None。"""
    match = DOC_SCORE_RE.search(raw or "")
    if not match:
        return None
    try:
        return max(0, min(100, int(match.group(1))))
    except ValueError:
        return None


_ASCII_KEYWORD_RE = re.compile(r"[a-z0-9_+\-.# ]+")


def _keyword_matches(hay: str, keyword: str) -> bool:
    """ASCII 关键词要求词边界（避免 train/openai 误命中 AI），中文关键词用子串。"""
    if not keyword:
        return False
    low = keyword.lower()
    if _ASCII_KEYWORD_RE.fullmatch(low):
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(low)}(?![a-z0-9])", hay))
    return low in hay


def match_category(raw: str, categories: list[str]) -> str:
    candidate = (raw or "").strip()
    if not candidate or not categories:
        return FALLBACK_CATEGORY
    for category in categories:
        if candidate == category:
            return category
    low = candidate.lower()
    for category in categories:
        c = category.lower()
        if not c:
            continue
        if low in c:
            return category
        if c in low and _keyword_matches(low, category):
            return category
    return FALLBACK_CATEGORY


def guess_category(categories: list[str], *texts: str) -> str:
    hay = " ".join(text or "" for text in texts).lower()
    for category in categories:
        if _keyword_matches(hay, category):
            return category
    return FALLBACK_CATEGORY


def parse_summary(raw: str, fallback_title: str, categories: list[str]) -> tuple[str, str, str]:
    """把模型输出拆成（文档标题、分区、正文）。解析失败时退回视频标题。"""
    text = (raw or "").strip()
    title = ""
    category = ""
    title_match = DOC_TITLE_RE.search(text)
    if title_match:
        title = title_match.group(1).strip()
    category_match = DOC_CATEGORY_RE.search(text)
    if category_match:
        category = category_match.group(1).strip()
    body = DOC_CATEGORY_RE.sub("", DOC_TITLE_RE.sub("", text)).strip()
    body = DOC_SCORE_RE.sub("", body).strip()
    lines = body.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].strip().rstrip(":：") in ("正文", "要点", "摘要"):
        lines.pop(0)
    body = "\n".join(lines).strip()
    if not title:
        title = fallback_title
    return sanitize_doc_title(title), match_category(category, categories), body


def build_doc_name(category: str, doc_title: str) -> str:
    prefix = _BAD_NAME_CHARS.sub("", category or "").strip() or FALLBACK_CATEGORY
    return f"{prefix}｜{sanitize_doc_title(doc_title)}.md"


DIGEST_PREFIX = "【汇总】"

DIGEST_PROMPT = """你是知识整理员。下面是主题「{keyword}」的新视频摘要，以及该主题已有的汇总内容。
任务：只提取「已有汇总里没有的新知识」，整理成新增要点。
要求：
- 只输出新知识；已有汇总里出现过的说法不要重复。
- 3 到 10 条，每条一行，以「- 」开头。
- 合并相近内容，去掉视频标题、UP 和 bvid。
- 不确定的保留「视频中提到…」的说法。
- 不要出现链接、Cookie、密钥。
- 只输出要点正文，不要标题、不要 JSON、不要代码块。
已有汇总内容（可能为空）：
{existing}

新视频摘要：
{sources}
"""

DIGEST_REVIEW_PROMPT = """你是审校员。下面是准备追加进汇总文档的新增要点，以及它们的来源摘要。
检查新增要点里是否有来源中没有依据、被曲解或明显编造的内容。
输出格式（不要 JSON）：
结论：通过 或 有问题
问题：<逐条列出可疑点；没有就写 无>

新增要点：
{points}

来源摘要：
{sources}
"""

AUDIT_PROMPT = """你是事实核查员。下面是一份视频摘要和它的来源素材（字幕摘录或简介）。
检查摘要中是否有来源里找不到依据的断言（虚构事实、编造数字、把猜测写成结论）。
输出格式（不要 JSON）：
结论：通过 或 可疑
问题：<逐条列出可疑点；没有就写 无>

视频摘要：
{summary}

来源素材：
{excerpt}
"""

_VERDICT_RE = re.compile(r"^\s*结论\s*[:：]\s*(.+?)\s*$", re.M)
_PROBLEM_RE = re.compile(r"^\s*问题\s*[:：]\s*([\s\S]*)$", re.M)


def digest_doc_name(keyword: str) -> str:
    return f"{DIGEST_PREFIX}{_BAD_NAME_CHARS.sub('', keyword or '').strip() or FALLBACK_CATEGORY}｜主题知识.md"


def render_digest(keyword: str, sections: str, round_no: int, date_str: str, source_total: int) -> str:
    header = [
        f"# {DIGEST_PREFIX}{keyword} 主题知识",
        "",
        f"> 只增不删。最后更新：{date_str}。累计 {round_no} 轮，来源 {source_total} 条视频。",
        f"- 类型: 主题汇总（只增不删）",
        f"- 关键词: {keyword}",
        "- 说明: 本文档由视频摘要逐轮机械追加生成，历史内容不会被改写或删除。",
        "",
    ]
    return "\n".join(header) + "\n" + (sections.strip() or "（暂无内容）") + "\n"


def render_digest_section(round_no: int, date_str: str, bvids: list[str], points: str) -> str:
    sources = "、".join(bvids) if bvids else "无"
    return f"## 第 {round_no} 轮新增（{date_str}，来源：{sources}）\n\n{points.strip()}\n"


def review_verdict(text: str) -> tuple[str, str]:
    raw = (text or "").strip()
    if not raw:
        return "可疑", "复审没有返回内容"
    match = _VERDICT_RE.search(raw)
    verdict = match.group(1).strip() if match else ""
    problem_match = _PROBLEM_RE.search(raw)
    problems = problem_match.group(1).strip() if problem_match else raw
    if not verdict:
        verdict = "可疑" if any(word in raw for word in ("可疑", "问题", "不通过")) else "通过"
    return verdict, problems


def is_suspect_verdict(verdict: str) -> bool:
    return any(word in (verdict or "") for word in ("可疑", "有问题", "不通过", "未通过"))


def sample_text(text: str, max_chars: int, marker: str = "\n\n（中间字幕已省略）\n\n") -> str:
    """超长字幕取开头、中间、结尾三段，控制送入模型的长度。

    采样思路参考 xiaowan138/astrbot_plugin_bilibili_parser 的 _fit_subtitle_text（MIT）。
    """
    text = (text or "").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if max_chars <= 80:
        return text[:max_chars]
    available = max_chars - len(marker) * 2
    if available < 30:
        return text[:max_chars]
    chunk = max(1, available // 3)
    middle_start = max((len(text) - chunk) // 2, chunk)
    end_start = max(len(text) - chunk, middle_start + chunk)
    return (
        text[:chunk]
        + marker
        + text[middle_start : middle_start + chunk]
        + marker
        + text[end_start:]
    )[:max_chars]


def worth_keeping(title: str, desc: str, subtitle: str, exclude: list[str]) -> bool:
    hay = " ".join([title or "", desc or "", subtitle[:400] if subtitle else ""])
    for word in exclude:
        if word and word in hay:
            return False
    if not (title or desc or subtitle):
        return False
    return True


def render_document(
    meta: dict[str, Any],
    summary: str,
    watched_at: str,
    subtitle_note: str = "",
    category: str = "",
    doc_title: str = "",
) -> str:
    lines = [
        f"# {meta.get('title') or meta.get('bvid')}",
        "",
        f"- bvid: {meta.get('bvid')}",
        f"- 链接: {meta.get('url')}",
        f"- UP: {meta.get('author') or '未知'}",
        f"- 分区: {meta.get('tname') or meta.get('typename') or '未知'}",
    ]
    if category:
        lines.append(f"- 归类: {category}")
    if doc_title:
        lines.append(f"- 概括: {doc_title}")
    if subtitle_note:
        lines.append(f"- 素材: {subtitle_note}")
    lines.extend(
        [
            f"- 观看整理时间: {watched_at}",
            "",
            "这是公开视频的摘要，不是 Bot 亲历，也不是用户说过的话。",
            "",
            summary.strip() or "（摘要为空，仅保留元数据。）",
            "",
            "适用问题：当用户问到该主题、该 UP、或相近的公开知识时，可以参考上面要点，并保留不确定。",
        ]
    )
    return "\n".join(lines)


def now_bj() -> str:
    return datetime.now(BJ).strftime("%Y-%m-%d %H:%M")


def format_seconds(seconds: int) -> str:
    m, s = divmod(max(0, int(seconds)), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def render_conclusion(data: dict[str, Any]) -> str:
    summary = str(data.get("summary") or "").strip()
    outline = data.get("outline") or []
    parts = []
    if summary:
        parts.append(f"【核心概述】\n{summary}")

    outline_lines = []
    if isinstance(outline, list):
        for ch in outline:
            if not isinstance(ch, dict):
                continue
            title = str(ch.get("title") or "").strip()
            ts = ch.get("timestamp")
            ts_prefix = f"[{format_seconds(ts)}] " if ts is not None else ""
            outline_lines.append(f"• {ts_prefix}{title}")
            for part in ch.get("part_outline") or []:
                if not isinstance(part, dict):
                    continue
                p_content = str(part.get("content") or "").strip()
                p_ts = part.get("timestamp")
                p_prefix = f"  - {format_seconds(p_ts)} " if p_ts is not None else "  - "
                if p_content:
                    outline_lines.append(f"{p_prefix}{p_content}")
    if outline_lines:
        parts.append("【分段大纲】\n" + "\n".join(outline_lines))
    return "\n\n".join(parts)

