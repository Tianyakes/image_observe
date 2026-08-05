"""图片搜索: 抓取必应国内版图片结果, 并用豆包视觉模型验证内容。

流程: 搜索 -> 提取候选图片 URL -> 视觉验证内容是否符合需求 -> 返回验证过的链接。
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from html import unescape

import httpx
from openai import OpenAI

from .config import ARK_BASE_URL, VISION_MODEL, require_api_key

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


def _translate_to_en(query: str) -> str:
    """用豆包把中文需求翻译成英文关键词 (必应中文搜索无会话时结果质量差)。"""
    if not _HAS_CJK.search(query):
        return query
    client = OpenAI(base_url=ARK_BASE_URL, api_key=require_api_key(), timeout=60)
    try:
        resp = client.chat.completions.create(
            model=VISION_MODEL,
            messages=[
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
            ],
            max_tokens=100,
        )
        translated = (resp.choices[0].message.content or "").strip()
        return translated or query
    except Exception:
        return query


def _fetch_candidates(query: str, limit: int = 12) -> list[str]:
    """抓取必应图片搜索结果, 提取原始图片 URL 列表 (去重)。"""
    resp = httpx.get(
        BING_IMAGE_URL,
        params={"q": query, "form": "HDRSC2", "setlang": "zh-cn"},
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    urls: list[str] = []
    for m in _META_RE.finditer(resp.text):
        try:
            data = json.loads(unescape(m.group(1)))
            u = data.get("murl", "")
        except Exception:
            continue
        if not u or u.startswith(("data:", "blob:")):
            continue
        if not re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", u, re.I):
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


def _verify_one(image_url: str, query: str) -> str | None:
    """用视觉模型判断图片内容是否符合需求, 返回一句话描述 (不符/失败返回 None)。"""
    client = OpenAI(base_url=ARK_BASE_URL, api_key=require_api_key(), timeout=60)
    prompt = (
        f"这是一张图片。请判断: 这张图片的内容是否符合需求「{query}」?\n"
        "严格按以下格式回答:\n"
        "匹配: 是 或 否\n"
        "描述: 一句话描述图片内容"
    )
    try:
        resp = client.chat.completions.create(
            model=VISION_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
            max_tokens=200,
        )
    except Exception:
        return None  # 图片失效/无法访问, 视为不通过
    text = resp.choices[0].message.content or ""
    match_line = text.splitlines()[0] if text else ""
    if "否" in match_line:
        return None
    desc = re.search(r"描述[:：]\s*(.+)", text, re.M)
    return desc.group(1).strip() if desc else text[:80]


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
    for _ in range(3):
        candidates = _fetch_merged(query, limit=max(count * 4, 12))[:15]
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(_verify_one, u, query): u for u in candidates}
            for f in futures:
                desc = f.result()
                u = futures[f]
                if desc is not None and u not in seen:
                    seen.add(u)
                    matched.append((u, desc))
        if matched:
            break

    if not matched:
        return "搜索到图片但视觉验证均未通过 (内容与需求不符), 请调整关键词重试"

    lines = [
        f"{i + 1}. {url}\n   内容: {desc}"
        for i, (url, desc) in enumerate(matched[:count])
    ]
    return f"验证通过的图片 ({len(matched[:count])} 张):\n" + "\n\n".join(lines)
