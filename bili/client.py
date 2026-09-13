"""B 站公开接口客户端：WBI 签名搜索、多 P 字幕、风控冷却。

字幕获取参考 mjy1113451/astrbot_plugin_b- 的 api/subtitles.py 实测经验（MIT）：
优先 player/wbi/v2（带 fnval），412 或空 URL 时回退 player/v2，并支持 subtitle_url_v2。
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import random
import re
import time
import uuid
from http.cookiejar import Cookie, CookieJar
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

from .reference import VideoReference, extract_video_reference
from .throttle import BiliThrottle

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
SEARCH_URL = "https://api.bilibili.com/x/web-interface/search/type"
VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
PLAYER_WBI_URL = "https://api.bilibili.com/x/player/wbi/v2"
PLAYER_URL = "https://api.bilibili.com/x/player/v2"
RECOMMEND_URL = "https://api.bilibili.com/x/web-interface/index/top/rcmd"
WBI_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32,
    15, 50, 10, 31, 58, 3, 45, 35,
    27, 30, 26, 22, 20, 49, 6, 25,
    51, 11, 14, 59, 17, 1, 43, 28,
]
RISK_CODES = {-799, -509}
SUBTITLE_HOSTS = (".hdslb.com", ".bilibili.com", ".bilivideo.com")
BUVID3_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}infoc$"
)


def valid_buvid3(value: str) -> bool:
    return bool(value and BUVID3_RE.match(value))


class BiliRiskError(RuntimeError):
    """命中 B 站风控/限流，本轮任务应停止后续请求。"""


class BiliHttpError(RuntimeError):
    def __init__(self, status: int, payload: dict[str, Any] | None = None):
        super().__init__(f"bilibili http {status}")
        self.status = status
        self.payload = payload or {}


def _extract_subs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") or {}
    subtitle = data.get("subtitle") or {}
    subs = subtitle.get("subtitles") or data.get("subtitles") or []
    return [item for item in subs if isinstance(item, dict)]


def _has_sub_url(subs: list[dict[str, Any]]) -> bool:
    return any(_subtitle_url(item) for item in subs)


def _subtitle_url(entry: dict[str, Any]) -> str:
    for key in ("subtitle_url", "subtitle_url_v2"):
        url = str(entry.get(key) or "").strip()
        if not url or url == "/":
            continue
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = "https://api.bilibili.com" + url
        if _trusted_subtitle_url(url):
            return url
    return ""


def _trusted_subtitle_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host.endswith(suffix) for suffix in SUBTITLE_HOSTS)


def _track_rank(entry: dict[str, Any]) -> int:
    lan = str(entry.get("lan") or "").lower()
    if lan in ("zh-hans", "zh-cn"):
        return 0
    if lan.startswith("zh"):
        return 1
    if "zh" in lan:
        return 2
    return 3


def _title_fragments(title: str) -> set[str]:
    cleaned = re.sub(r"[^\u4e00-\u9fff\w]", " ", (title or "").lower())
    out: set[str] = set()
    for part in cleaned.split():
        if len(part) >= 2 and not part.isdigit():
            out.add(part)
    return out


def subtitle_mismatch(title: str, text: str) -> bool:
    """标题关键词在字幕前段全部缺席时，认为这条字幕轨不可信。"""
    fragments = _title_fragments(title)
    if len(fragments) < 2 or not text:
        return False
    low = text.lower()
    if any(fragment in low[:600] for fragment in fragments):
        return False
    return not any(fragment in low[:2000] for fragment in fragments)


def _cache_bust(url: str, attempt: int) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}_retry={attempt}&_r={random.randint(1, 99999)}"


class BiliClient:
    def __init__(self, sessdata: str = "", interval: float = 3.0):
        self.sessdata = (sessdata or "").strip()
        self.throttle = BiliThrottle(interval)
        self._cookiejar = CookieJar()
        self._opener = build_opener(HTTPCookieProcessor(self._cookiejar))
        self._wbi_key = ""
        self._wbi_at = 0.0
        self._wbi_lock = asyncio.Lock()
        self._fetch_lock = asyncio.Lock()
        self._buvid_ready = False
        self._buvid_lock = asyncio.Lock()
        if self.sessdata:
            self._set_cookie("SESSDATA", self.sessdata)

    def _set_cookie(self, name: str, value: str) -> None:
        cookie = Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=".bilibili.com",
            domain_specified=True,
            domain_initial_dot=True,
            path="/",
            path_specified=True,
            secure=False,
            expires=None,
            discard=False,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
        self._cookiejar.set_cookie(cookie)

    async def _ensure_buvid(self) -> None:
        if self._buvid_ready:
            return
        async with self._buvid_lock:
            if self._buvid_ready:
                return
            buvid3 = ""
            buvid4 = ""
            try:
                async with self._fetch_lock:
                    await self.throttle.wait()
                    status, payload = await asyncio.to_thread(
                        self._fetch_sync, SPI_URL, "https://www.bilibili.com/", "GET"
                    )
                if status == 200 and payload.get("code") == 0:
                    data = payload.get("data") or {}
                    buvid3 = str(data.get("b_3") or "").strip()
                    buvid4 = str(data.get("b_4") or "").strip()
            except Exception:
                pass
            if not buvid3 or len(buvid3) < 20:
                buvid3 = str(uuid.uuid1()) + "infoc"
                buvid4 = ""
            self._set_cookie("buvid3", buvid3)
            if buvid4:
                self._set_cookie("buvid4", buvid4)
            self._set_cookie("b_nut", str(int(time.time())))
            self._buvid_ready = True

    async def warmup(self) -> None:
        await self._ensure_buvid()

    def _headers(self, referer: str = "https://www.bilibili.com/") -> dict[str, str]:
        return {
            "User-Agent": UA,
            "Referer": referer,
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": "https://www.bilibili.com",
            "Connection": "keep-alive",
        }

    def _fetch_sync(self, url: str, referer: str, method: str = "GET") -> tuple[int, dict[str, Any]]:
        req = Request(url, headers=self._headers(referer), method=method)
        try:
            with self._opener.open(req, timeout=20) as resp:
                if method == "HEAD":
                    return resp.status, {}
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    return resp.status, json.loads(raw)
                except json.JSONDecodeError:
                    return resp.status, {"code": -1, "message": "bad json", "data": None}
        except HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                payload = {"code": -1, "message": f"http {exc.code}"}
            return int(exc.code), payload

    async def _raw(
        self,
        url: str,
        referer: str,
        *,
        retries: int = 2,
        retry_412: bool = True,
    ) -> dict[str, Any]:
        status = 0
        payload: dict[str, Any] = {}
        async with self._fetch_lock:
            for attempt in range(retries + 1):
                await self.throttle.wait()
                try:
                    status, payload = await asyncio.to_thread(self._fetch_sync, url, referer, "GET")
                except Exception:
                    status, payload = 0, {}
                if status == 200:
                    code = payload.get("code")
                    if code in RISK_CODES:
                        self.throttle.trigger_cooldown()
                        raise BiliRiskError(f"bilibili risk code {code}")
                    return payload
                if status in (412, 429):
                    if not retry_412:
                        raise BiliHttpError(status, payload)
                    self.throttle.trigger_cooldown(5.0, 15.0)
                    raise BiliRiskError(f"bilibili http {status}")
                if attempt >= retries:
                    break
                if status == 403:
                    continue
                if status >= 500 or status == 0:
                    await asyncio.sleep(1.5 * (attempt + 1) + random.uniform(0.0, 1.0))
                    continue
                break
        raise BiliHttpError(status or 0, payload)

    def _extract_key(self, url: str) -> str:
        match = re.search(r"/([^/]+)\.(?:png|jpg|webp|svg)(?:\?|$)", url or "")
        if not match:
            match = re.search(r"/([^/?]+?)(?:\.[^/?]+)?(?:\?|$)", url or "")
        return match.group(1) if match else ""

    def _load_wbi_sync(self) -> str:
        if self._wbi_key and time.time() - self._wbi_at < 3600:
            return self._wbi_key
        status, nav = self._fetch_sync(NAV_URL, "https://www.bilibili.com/")
        if status != 200:
            if status in (412, 429):
                raise BiliRiskError(f"nav blocked http {status}")
            raise BiliHttpError(status, nav)
        data = nav.get("data") or {}
        images = data.get("wbi_img") or {}
        img_key = self._extract_key(str(images.get("img_url") or ""))
        sub_key = self._extract_key(str(images.get("sub_url") or ""))
        if not img_key or not sub_key:
            raise RuntimeError(
                f"Bilibili WBI keys unavailable: code={nav.get('code')} message={nav.get('message')}"
            )
        raw = img_key + sub_key
        self._wbi_key = "".join(raw[i] for i in WBI_TABLE if i < len(raw))[:32]
        self._wbi_at = time.time()
        return self._wbi_key

    async def _mixin_key(self, force: bool = False) -> str:
        async with self._wbi_lock:
            if self._wbi_key and time.time() - self._wbi_at < 3600 and not force:
                return self._wbi_key
            async with self._fetch_lock:
                await self.throttle.wait()
                return await asyncio.to_thread(self._load_wbi_sync)

    async def _signed(
        self,
        base: str,
        params: dict[str, Any],
        referer: str,
        *,
        retries: int = 2,
        retry_412: bool = True,
        force_wbi: bool = False,
    ) -> dict[str, Any]:
        mixin = await self._mixin_key(force=force_wbi)
        signed = dict(params)
        signed["wts"] = int(time.time())
        query = urlencode(sorted(signed.items()))
        signed["w_rid"] = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
        return await self._raw(
            f"{base}?{urlencode(signed)}", referer, retries=retries, retry_412=retry_412
        )

    async def search_videos(self, keyword: str, page: int = 1) -> list[dict[str, Any]]:
        await self._ensure_buvid()
        payload = await self._signed(
            SEARCH_URL,
            {
                "search_type": "video",
                "keyword": keyword,
                "page": page,
                "pagesize": 20,
                "web_location": 1430654,
            },
            "https://search.bilibili.com/",
        )
        result = ((payload.get("data") or {}).get("result")) or []
        out = []
        for item in result:
            if not isinstance(item, dict):
                continue
            bvid = str(item.get("bvid") or "").strip()
            if not bvid:
                continue
            out.append(
                {
                    "bvid": bvid,
                    "title": _strip_em(str(item.get("title") or "")),
                    "author": _strip_em(str(item.get("author") or item.get("uname") or "")),
                    "mid": str(item.get("mid") or ""),
                    "description": _strip_em(
                        str(item.get("description") or item.get("desc") or "")
                    ),
                    "typename": str(item.get("typename") or ""),
                    "url": f"https://www.bilibili.com/video/{bvid}",
                    "source": "search",
                    "keyword": keyword,
                }
            )
        return out

    async def recommend(self) -> list[dict[str, Any]]:
        if not self.sessdata:
            return []
        await self._ensure_buvid()
        payload = await self._raw(
            RECOMMEND_URL
            + "?fresh_type=3&version=1&ps=10&fresh_idx=1&fresh_idx_1h=1"
            + "&homepage_ver=1&brush=0&fetch_row=1&web_location=1430650",
            "https://www.bilibili.com/",
        )
        items = ((payload.get("data") or {}).get("item")) or []
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            bvid = str(item.get("bvid") or "").strip()
            if not bvid:
                continue
            owner = item.get("owner") or {}
            out.append(
                {
                    "bvid": bvid,
                    "title": str(item.get("title") or ""),
                    "author": str(owner.get("name") or ""),
                    "mid": str(owner.get("mid") or ""),
                    "description": "",
                    "typename": "",
                    "url": f"https://www.bilibili.com/video/{bvid}",
                    "source": "recommend",
                    "keyword": "",
                }
            )
        return out

    async def view(self, bvid: str = "", aid: Any = None) -> dict[str, Any]:
        query = urlencode({"bvid": bvid} if bvid else {"aid": aid})
        payload = await self._raw(f"{VIEW_URL}?{query}", "https://www.bilibili.com/")
        data = payload.get("data") or {}
        if not data:
            raise RuntimeError(
                f"view failed: code={payload.get('code')} message={payload.get('message')}"
            )
        owner = data.get("owner") or {}
        pages = []
        for index, page in enumerate(data.get("pages") or [], start=1):
            cid = page.get("cid")
            if not cid:
                continue
            pages.append(
                {
                    "cid": int(cid),
                    "page": int(page.get("page") or index),
                    "part": str(page.get("part") or ""),
                }
            )
        cid = pages[0]["cid"] if pages else data.get("cid")
        stat = data.get("stat") or {}
        return {
            "bvid": str(data.get("bvid") or bvid),
            "aid": data.get("aid"),
            "cid": cid,
            "pages": pages,
            "title": str(data.get("title") or ""),
            "desc": str(data.get("desc") or ""),
            "tname": str(data.get("tname") or ""),
            "author": str(owner.get("name") or ""),
            "mid": str(owner.get("mid") or ""),
            "pic": str(data.get("pic") or ""),
            "duration": int(data.get("duration") or 0),
            "view_count": int(stat.get("view") or 0),
            "like_count": int(stat.get("like") or 0),
            "url": f"https://www.bilibili.com/video/{data.get('bvid') or bvid}",
        }

    def _final_url_sync(self, url: str) -> str:
        for method in ("HEAD", "GET"):
            try:
                req = Request(url, headers=self._headers(), method=method)
                with self._opener.open(req, timeout=15) as resp:
                    return resp.geturl()
            except HTTPError as exc:
                if method == "GET" and exc.geturl():
                    return exc.geturl()
            except Exception:
                continue
        return url

    async def resolve_short_url(self, url: str) -> str:
        target = url if re.match(r"https?://", url or "", re.I) else "https://" + (url or "")
        async with self._fetch_lock:
            await self.throttle.wait()
            try:
                final = await asyncio.to_thread(self._final_url_sync, target)
            except Exception:
                final = target
        match = re.search(r"BV[0-9A-Za-z]{10}", final or "")
        if match:
            return match.group(0)
        aid_match = re.search(r"[?&/]av(\d{1,20})", final or "", re.I)
        if aid_match:
            return await self._bvid_from_aid(aid_match.group(1))
        return ""

    async def _bvid_from_aid(self, aid: Any) -> str:
        payload = await self._raw(f"{VIEW_URL}?aid={aid}", "https://www.bilibili.com/")
        return str(((payload.get("data") or {}).get("bvid")) or "")

    async def resolve_bvid(self, reference: str | VideoReference) -> str:
        if isinstance(reference, VideoReference):
            kind, value = reference.kind, reference.value
        else:
            ref = extract_video_reference(str(reference or ""))
            if ref is None:
                text = str(reference or "").strip()
                if text.lower().startswith("bv") and len(text) >= 12:
                    kind, value = "bvid", "BV" + text[2:]
                elif text.lower().startswith("av") and text[2:].isdigit():
                    kind, value = "aid", text[2:]
                else:
                    return ""
            else:
                kind, value = ref.kind, ref.value
        if kind == "bvid":
            return value
        if kind == "aid":
            return await self._bvid_from_aid(value)
        return await self.resolve_short_url(value)

    async def _player_subs(self, aid: Any, cid: Any, referer: str) -> list[dict[str, Any]]:
        subs: list[dict[str, Any]] = []
        try:
            payload = await self._signed(
                PLAYER_WBI_URL,
                {
                    "cid": cid,
                    "aid": aid,
                    "fnver": 0,
                    "fnval": 4048,
                    "isGaiaAvoided": False,
                    "web_location": 1315873,
                },
                referer,
                retries=1,
                retry_412=False,
            )
            subs = _extract_subs(payload)
        except BiliHttpError:
            subs = []
        if _has_sub_url(subs):
            return subs
        try:
            payload2 = await self._signed(
                PLAYER_URL, {"cid": cid, "aid": aid}, referer, retries=1, retry_412=False
            )
            subs2 = _extract_subs(payload2)
            if _has_sub_url(subs2):
                return subs2
        except BiliHttpError as exc:
            if exc.status in (412, 429):
                raise BiliRiskError(f"player subtitle blocked http {exc.status}") from exc
        return subs

    async def _fetch_subtitle_json(self, url: str, retries: int = 2) -> str:
        for attempt in range(retries + 1):
            target = url if attempt == 0 else _cache_bust(url, attempt)
            try:
                payload = await self._raw(target, "https://www.bilibili.com/", retries=0)
            except BiliRiskError:
                raise
            except Exception:
                continue
            body = payload.get("body")
            if not isinstance(body, list):
                continue
            lines = [
                str(item.get("content") or "").strip()
                for item in body
                if isinstance(item, dict) and item.get("content")
            ]
            text = "\n".join(line for line in lines if line)
            if text:
                return text
        return ""

    async def subtitle_text(self, aid: Any, cid: Any, bvid: str = "", title: str = "") -> str:
        if not cid:
            return ""
        referer = f"https://www.bilibili.com/video/{bvid}" if bvid else "https://www.bilibili.com/"
        subs = await self._player_subs(aid, cid, referer)
        for entry in sorted(subs, key=_track_rank):
            url = _subtitle_url(entry)
            if not url:
                continue
            text = await self._fetch_subtitle_json(url)
            if not text:
                continue
            if title and subtitle_mismatch(title, text):
                continue
            return text
        return ""

    async def subtitles_for_pages(
        self,
        meta: dict[str, Any],
        page_limit: int = 3,
        verify: bool = True,
    ) -> str:
        pages = list(meta.get("pages") or [])
        if not pages:
            pages = [{"cid": meta.get("cid"), "page": 1, "part": ""}]
        if page_limit > 0:
            pages = pages[:page_limit]
        sections = []
        for page in pages:
            cid = page.get("cid")
            if not cid:
                continue
            text = await self.subtitle_text(
                meta.get("aid"),
                cid,
                str(meta.get("bvid") or ""),
                str(meta.get("title") or "") if verify else "",
            )
            if not text:
                continue
            part = str(page.get("part") or "").strip()
            heading = f"[P{page.get('page')}{' ' + part if part else ''}]"
            sections.append(f"{heading}\n{text}")
        return "\n\n".join(sections)


_EM_TAG_RE = re.compile(r"</?em[^>]*>", re.I)


def _strip_em(text: str) -> str:
    return html.unescape(_EM_TAG_RE.sub("", text or "")).strip()
