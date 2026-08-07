"""图片搜索: 抓取必应国内版图片结果, 并用豆包视觉模型批量验证内容。

流程: 搜索 -> 提取候选图片 URL -> 批量视觉验证内容是否符合需求 -> 返回验证过的链接。
API/网络错误与"内容不符"分开报告, 不静默吞错。
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import vision
from .config import require_api_key

BING_IMAGE_URL = "https://cn.bing.com/images/search"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

_META_RE = re.compile(r'm="(.*?)"')
_HAS_CJK = re.compile(r"[一-鿿]")
_IMAGE_EXT_RE = re.compile(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", re.I)
_NON_IMAGE_EXTS = (".pdf", ".mp4", ".webm", ".zip", ".svg", ".ico", ".txt")

# 批量验证参数: 单条消息最大图数 / 最小批量 (再小就冒泡报 API 错误)
_BATCH_MAX = 10
_BATCH_MIN = 4
_VERIFY_MAX_TOKENS = 1200
_TRANSLATE_MAX_TOKENS = 100

# 模型批量回答的宽松解析 (兼容不同输出格式, 不依赖 json_object):
# "第1张: 匹配: 是" 或 "第 1 张:匹配:否" 等
_MATCH_RE = re.compile(r"第\s*(\d+)\s*张?[:：]?\s*匹配[:：]\s*(是|否)")
_DESC_RE = re.compile(r"第\s*(\d+)\s*张?[:：]?\s*描述[:：]\s*(.+)")


def _translate_to_en(query: str) -> str:
    """用豆包把中文需求翻译成英文关键词 (必应中文搜索无会话时结果质量差)。"""
    if not _HAS_CJK.search(query):
        return query
    messages = [
        {
            "role": "user",
            "content": (
                "把下面这句中文图片搜索需求翻译成英文图片搜索关键词。规则:\n"
                "1. 最关键的主体实体放最前面 (如电影名、物体名)\n"
                "2. 每个词首字母大写, 用空格分隔, 不要标点\n"
                "3. 只输出关键词, 不要任何其他内容\n"
                f"需求: {query}"
            ),
        }
    ]
    try:
        content, _model = vision._chat_with_policy(messages)
        return content.strip() or query
    except Exception:
        return query  # 翻译是增强而非关键路径, 失败回退原文


def _fetch_candidates(query: str, limit: int = 12) -> list[str]:
    """抓取必应图片搜索结果, 提取原始图片 URL 列表 (去重)。"""
    try:
        resp = httpx.get(
            BING_IMAGE_URL,
            params={"q": query, "form": "HDRSC2", "setlang": "zh-cn"},
            headers=HEADERS,
            timeout=30,
            follow_redirects=True,  # cn.bing.com 会 302 到 www.bing.com
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise RuntimeError(f"必应搜索请求失败: {e}") from e
    urls: list[str] = []
    for m in _META_RE.finditer(resp.text):
        try:
            data = json.loads(unescape(m.group(1)))
            u = data.get("murl", "")
        except Exception:
            continue
        if not u or u.startswith(("data:", "blob:")):
            continue
        if _IMAGE_EXT_RE.search(u):
            pass  # 常见图片扩展名
        else:
            # 无扩展名 (CDN 签名 URL) 或图片格式变体允许, 靠视觉验证兜底;
            # 明显非图片扩展名排除
            ext = Path(urlparse(u).path).suffix.lower()
            if ext and ext not in _NON_IMAGE_EXTS and not _IMAGE_EXT_RE.search(u):
                continue
            if ext in _NON_IMAGE_EXTS:
                continue
        if u not in urls:
            urls.append(u)
        if len(urls) >= limit:
            break
    return urls


def _fetch_merged(query: str, limit: int = 12) -> list[str]:
    """多配方合并搜索: 翻译结果 / 逆序 / 原始查询, 去重汇总。

    必应对查询词序非常敏感 (实测 "Interstellar movie black hole" 有效,
    "Black Hole Interstellar Movie" 返回无关结果), 多配方可显著提高召回。
    """
    translated = _translate_to_en(query)
    variants = [translated]
    words = translated.split()
    if len(words) > 1:
        variants.append(" ".join(reversed(words)))
    if query != translated:
        variants.append(query)
    urls: list[str] = []
    for v in variants:
        for u in _fetch_candidates(v, limit=limit):
            if u not in urls:
                urls.append(u)
    return urls[:limit * 2]


def _verify_batch(image_urls: list[str], query: str) -> dict[str, str | None]:
    """单条消息批量验证多张图 (豆包多 image_url), 返回 {url: 一句话描述 或 None(不符)}。

    模型输出逐行 "第N张: 匹配: 是/否" + "第N张: 描述: ...", 宽松前缀解析;
    API 调用失败抛 RuntimeError, 由上层区分处理, 不当作"内容不符"。
    """
    prompt = (
        f"下面是多张图片 (按顺序编号)。请逐张判断每张图片的内容是否符合需求「{query}」?\n"
        "严格按以下格式逐张回答, 每张两行:\n"
        "第1张: 匹配: 是 或 否\n"
        "第1张: 描述: 一句话描述图片内容\n"
        "不要遗漏任何一张, 不要添加其他内容。"
    )
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                *[{"type": "image_url", "image_url": {"url": u}} for u in image_urls],
            ],
        }
    ]
    content, _model = vision._chat_with_policy(messages)

    matches: dict[int, bool] = {}
    for m in _MATCH_RE.finditer(content):
        matches[int(m.group(1))] = m.group(2) == "是"
    descs: dict[int, str] = {}
    for m in _DESC_RE.finditer(content):
        descs[int(m.group(1))] = m.group(2).strip()

    results: dict[str, str | None] = {}
    for i, u in enumerate(image_urls):
        n = i + 1
        if n in matches and matches[n]:
            results[u] = descs.get(n, "")
        else:
            results[u] = None  # 未回答或回答"否" → 不通过
    return results


def _verify_all(image_urls: list[str], query: str) -> dict[str, str | None]:
    """分批并发验证全部图片; 整批 API 错误时二分拆批重试, 单张仍失败则冒泡报错。"""

    def _verify_chunk(chunk: list[str]) -> dict[str, str | None]:
        try:
            return _verify_batch(chunk, query)
        except Exception:
            if len(chunk) <= _BATCH_MIN:
                raise
            mid = len(chunk) // 2
            return {**_verify_chunk(chunk[:mid]), **_verify_chunk(chunk[mid:])}

    results: dict[str, str | None] = {}
    chunks = [image_urls[i:i + _BATCH_MAX] for i in range(0, len(image_urls), _BATCH_MAX)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_verify_chunk, c) for c in chunks]
        for f in futures:
            results.update(f.result())
    return results


def search_images(query: str, count: int = 5, verify: bool = True) -> str:
    """搜索图片并返回验证过的链接列表 (供 agent 进一步处理)。

    Args:
        query: 图片需求描述, 如 "黑洞 电影 星际穿越"。
        count: 最多返回几张。
        verify: 是否用视觉模型逐张验证内容 (建议开启, 关闭只返回原始链接)。
    """
    if not verify:
        candidates = _fetch_merged(query, limit=max(count * 4, 12))[:15]
        if not candidates:
            return "未找到图片结果, 请尝试换一个关键词"
        lines = [f"{i + 1}. {u}" for i, u in enumerate(candidates[:count])]
        return "搜索到以下图片链接:\n" + "\n".join(lines)

    # 必应无会话结果不稳定: 垃圾块常霸占前排且每轮布局不同。
    # 验证全部候选 (而非只看前 N 个) + 最多重试 3 轮, 提高命中率。
    matched: list[tuple[str, str]] = []
    seen: set[str] = set()
    api_errors: list[str] = []
    for _ in range(3):
        candidates = _fetch_merged(query, limit=max(count * 4, 12))[:15]
        try:
            results = _verify_all(candidates, query)
        except Exception as e:
            api_errors.append(str(e))
            results = {}
        for u, desc in results.items():
            if desc is not None and u not in seen:
                seen.add(u)
                matched.append((u, desc))
        if matched:
            break

    if not matched:
        if api_errors:
            return (
                "视觉验证接口失败: " + "; ".join(api_errors[:2])
                + "。请检查 ARK_API_KEY、模型开通状态与网络后重试"
            )
        return "搜索到图片但视觉验证均未通过 (内容与需求不符), 请调整关键词重试"

    lines = [
        f"{i + 1}. {url}\n   内容: {desc}"
        for i, (url, desc) in enumerate(matched[:count])
    ]
    return f"验证通过的图片 ({len(matched[:count])} 张):\n" + "\n\n".join(lines)
