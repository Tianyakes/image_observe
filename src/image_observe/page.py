"""网页分析: 无头浏览器渲染页面 -> 程序化布局诊断 + 豆包视觉模型设计描述。

所有结果以文字返回 (适配无视觉能力的 agent), 截图保存到 output/pages/。
playwright 在函数内延迟导入: 缺依赖时只影响 analyze_page, 不影响其他工具。
"""
import asyncio
import collections
from pathlib import Path

from . import design, vision
from .utils import OUTPUT_DIR, unique_filename

# ── 模块级常量 ──────────────────────────────────────────────
MAX_FULLPAGE_HEIGHT = 12000    # 全页截图最大高度(px), 超出仅截首屏
MAX_ELEMENTS = 400             # DOM 元素采样上限, 仅覆盖文档前 N 个元素
OVERLAP_RATIO = 0.3            # 重叠交集占比阈值 (交集 > 较小元素此比例则报告)
LARGE_TEXT_PX = 24             # WCAG 大字号阈值 (>=24px)
LARGE_BOLD_TEXT_PX = 18.66     # WCAG 粗体大字号阈值 (>=18.66px 且 bold)
TINY_TEXT_PX = 12              # 小字号警告阈值 (<12px)
NETWORK_IDLE_TIMEOUT_MS = 5000 # networkidle 等待上限 (尽力)
RENDER_STABILIZE_MS = 500      # 渲染稳定等待 (ms)
TEXT_SNIPPET_LEN = 30          # 元素文本截断长度 (字符)
# ────────────────────────────────────────────────────────────

# 布局指标提取: 单次 evaluate 返回 JSON, 几何分析在 Python 侧完成
_LAYOUT_JS = """
() => {
  const vw = window.innerWidth, vh = window.innerHeight;
  const d = document.documentElement;
  const docW = d.scrollWidth;
  const docH = Math.max(d.scrollHeight, document.body ? document.body.scrollHeight : 0);
  // --- WCAG 对比度: 颜色解析与背景合成 (Chromium computed color 恒为 rgb()/rgba()) ---
  function parseRGBA(str) {
    const m = str.match(/[\\d.]+/g); if (!m || m.length < 3) return null;
    return [+m[0], +m[1], +m[2], m.length > 3 ? +m[3] : 1];
  }
  function blend(dst, src) {                // src 覆盖在 dst 上 (src over dst)
    const a = src[3];
    return [src[0]*a + dst[0]*(1-a), src[1]*a + dst[1]*(1-a), src[2]*a + dst[2]*(1-a), 1];
  }
  function effBg(el) {                      // 祖先链取有效背景色 (自上而下混合)
    let cur = el, acc = null;
    while (cur && cur.nodeType === 1) {
      const cs = getComputedStyle(cur);
      if (cs.backgroundImage !== 'none') return { rgb: null, limit: 'image' };  // 背景含图/渐变
      const c = parseRGBA(cs.backgroundColor);
      if (c && c[3] > 0) acc = acc ? blend(acc, c) : c;   // 祖先背景在下方: c over acc
      if (c && c[3] >= 1) break;                          // 已不透明, 无需再查
      cur = cur.parentElement;
    }
    if (!acc) return { rgb: [255, 255, 255], fallback: true };   // 全透明 -> Chromium 实际渲染为白底
    if (acc[3] < 1) acc = blend([255, 255, 255, 1], acc);        // 链走完仍半透明 -> 与白底合成
    return { rgb: [acc[0] | 0, acc[1] | 0, acc[2] | 0] };
  }
  const els = [];
  const collectedIdx = new Map();   // 已收集元素 -> els 下标 (文档序, 祖先先于后代)
  const nodes = document.querySelectorAll('*');
  for (let i = 0; i < nodes.length && els.length < 400; i++) {
    const el = nodes[i];
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') continue;
    const r = el.getBoundingClientRect();
    const cls = typeof el.className === 'string'
      ? el.className.trim().split(/\\s+/).slice(0, 3).join(' ') : '';
    // 已收集祖先的 els 下标 (重叠诊断跳过祖先-后代配对, 避免正常嵌套误报)
    const ancs = [];
    let anc = el.parentElement;
    while (anc) {
      const ai = collectedIdx.get(anc);
      if (ai !== undefined) ancs.push(ai);
      anc = anc.parentElement;
    }
    const info = {
      tag: el.tagName.toLowerCase(), id: el.id || '', cls,
      x: Math.round(r.x), y: Math.round(r.y + window.scrollY),  // 文档坐标
      w: Math.round(r.width), h: Math.round(r.height),
      font: Math.round(parseFloat(s.fontSize) || 0),
      scrollW: el.scrollWidth, clientW: el.clientWidth,          // 内容截断检测
      ancs,
    };
    if (r.width === 0 && r.height === 0) { info.zero = true; els.push(info); collectedIdx.set(el, els.length - 1); continue; }
    info.inView = r.top < vh && r.bottom > 0 && r.left < vw && r.right > 0;
    info.offRight = r.left >= vw || r.right > vw + 1;            // 横向越界
    info.offLeft = r.right <= 0;
    // 对比度数据: 有文本的可见元素 (html/body 整页文本重复, 跳过)
    const t = (el.textContent || '').trim();
    if (t && info.tag !== 'html' && info.tag !== 'body') {
      info.text = t.slice(0, 30);
      info.fontW = parseInt(s.fontWeight) || 400;
      const pc = parseRGBA(s.color);
      if (pc) { info.fcolor = pc.slice(0, 3); info.falpha = pc[3]; }
      info.bg = effBg(el);
    }
    const hasContent = el.id || cls || t.length > 0
      || el.querySelector('img, video, canvas, svg');
    if (!hasContent && !info.offRight) continue;                 // 过滤空包装盒噪声
    els.push(info);
    collectedIdx.set(el, els.length - 1);
  }
  return { viewport: { w: vw, h: vh }, doc: { w: docW, h: docH },
           hasHScroll: docW > vw, els };
}
"""

_VISION_PROMPT = (
    "这是某个网页的完整截图。请用中文从 UI/UX 角度描述页面设计: 整体布局结构、"
    "主要配色、字体排版、视觉层次; 并指出明显视觉问题 (元素重叠、内容溢出、"
    "留白异常、对比度不足等)。控制在 300 字以内。"
)


def _normalize_url(url: str) -> str:
    """http/https 原样返回; 本地路径或 file:// 转成 Windows 安全的 file:// URI。"""
    if url.startswith(("http://", "https://")):
        return url
    if url.lower().startswith("file://"):
        p = url[len("file://"):]
        if p.startswith("/") and len(p) > 2 and p[2] == ":":  # file:///D:/... -> D:/...
            p = p[1:]
        return Path(p).resolve().as_uri()  # 百分号编码中文/空格路径
    return Path(url).resolve().as_uri()


async def _launch_browser():
    """系统 Edge -> 系统 Chrome -> 内置 chromium, 全失败报中文错误。"""
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    errors = []
    for channel in ("msedge", "chrome", None):
        try:
            browser = await pw.chromium.launch(
                channel=channel,
                headless=True,
                args=["--disable-dev-shm-usage", "--no-first-run"],
            )
            return pw, browser
        except Exception as e:
            errors.append(f"{channel or '内置 chromium'}: {e}")
    await pw.stop()
    raise RuntimeError(
        "无法启动浏览器: 请安装 Edge/Chrome, 或运行 `uv run playwright install chromium`。"
        + " " + "; ".join(errors)
    )


async def _render_page(
    browser,
    url: str,
    width: int,
    height: int,
    timeout_s: int,
    monitor: bool = False,
    events: dict | None = None,
):
    """打开页面并等待渲染稳定, 返回 page 对象 (browser 由调用方关闭)。

    monitor=True 时挂载加载监控: console 错误/未捕获异常/请求失败/HTTP>=400/
    字体加载失败/图片加载失败, 收集到调用方传入的 events dict。
    """
    context = await browser.new_context(
        viewport={"width": width, "height": height}, ignore_https_errors=True
    )
    page = await context.new_page()
    if monitor and events is not None:
        events["console"] = []
        events["pageerror"] = []
        events["failed"] = []
        events["http"] = []
        events["fonts"] = []
        events["imgfail"] = []
        page.on("console", lambda m: events["console"].append(m.text) if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: events["pageerror"].append(str(e)))
        page.on("requestfailed", lambda r: events["failed"].append(f"{r.url} ({r.failure})"))
        page.on("response", lambda r: events["http"].append(f"{r.status} {r.url}") if r.status >= 400 else None)
    try:
        await page.goto(url, timeout=timeout_s * 1000, wait_until="load")
    except Exception as e:
        await context.close()
        raise RuntimeError(f"无法加载页面: {e}") from e
    try:
        await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)  # 尽力而为
    except Exception:
        pass
    await page.wait_for_timeout(RENDER_STABILIZE_MS)
    # 等待字体加载完成 (超时 3s 尽力, 失败忽略)
    try:
        await asyncio.wait_for(
            page.evaluate("document.fonts.ready.then(() => true).catch(() => true)"),
            timeout=3,
        )
    except Exception:
        pass
    # 页面内字体/图片加载状态 (监控模式下)
    if monitor and events is not None:
        try:
            events["fonts"] = await page.evaluate(
                "[...document.fonts].filter(f => f.status === 'error').map(f => f.family)"
            )
        except Exception:
            pass
        try:
            events["imgfail"] = await page.evaluate(
                "[...document.images].filter(i => i.complete && i.naturalWidth === 0)"
                ".map(i => i.currentSrc || i.src).slice(0, 5)"
            )
        except Exception:
            pass
    return page


async def _save_screenshot(page, full_page: bool) -> Path:
    target = OUTPUT_DIR / "pages"
    target.mkdir(parents=True, exist_ok=True)
    path = target / unique_filename("page", ".png")
    await page.screenshot(path=str(path), full_page=full_page, timeout=60_000)
    return path


def _fmt_el(e: dict) -> str:
    if e.get("id"):
        return f"{e['tag']}#{e['id']}"
    if e.get("cls"):
        return f"{e['tag']}.{e['cls'].split()[0]}"
    return e["tag"]


def _rel_luminance(rgb: list) -> float:
    """WCAG 相对亮度: 线性化后 0.2126R + 0.7152G + 0.0722B。"""

    def lin(c: float) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = map(lin, rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(fg: list, bg: list) -> float:
    l1, l2 = sorted([_rel_luminance(fg), _rel_luminance(bg)], reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


def _is_large_text(e: dict) -> bool:
    """WCAG 大字号: >=24px, 或 >=18.66px 且粗体 (>=700)。"""
    return e["font"] >= LARGE_TEXT_PX or (e["font"] >= LARGE_BOLD_TEXT_PX and (e.get("fontW") or 400) >= 700)


def _overlap_pairs(els: list) -> list:
    """可见非零元素的显著重叠配对 (交集 > 较小元素 30%, 祖先-后代跳过)。"""
    pairs = []
    for i, a in enumerate(els):
        if a.get("zero") or a["w"] <= 0 or a["h"] <= 0:
            continue
        for j, b in enumerate(els[i + 1:], start=i + 1):
            if b.get("zero") or b["w"] <= 0 or b["h"] <= 0:
                continue
            if i in b.get("ancs", []) or j in a.get("ancs", []):
                continue
            ix = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
            iy = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
            inter = max(0, ix) * max(0, iy)
            if inter <= 0:
                continue
            ratio = inter / min(a["w"] * a["h"], b["w"] * b["h"])
            if ratio > OVERLAP_RATIO:
                pairs.append((ratio, a, b, ix, iy))
    return pairs


def _analyze_layout(metrics: dict) -> str:
    """纯 Python 布局诊断, 不依赖视觉模型。"""
    els = metrics["els"]
    vw, vh = metrics["viewport"]["w"], metrics["viewport"]["h"]
    doc_w, doc_h = metrics["doc"]["w"], metrics["doc"]["h"]
    visible = [e for e in els if not e.get("zero") and e["w"] > 0 and e["h"] > 0]
    lines = []

    summary = f"视口 {vw}x{vh}, 页面总高 {doc_h}px, 统计元素 {len(els)} 个 (采样上限 {MAX_ELEMENTS})"
    if metrics.get("hasHScroll"):
        summary += f" — 文档宽 {doc_w}px > 视口, 存在横向滚动"
    lines.append(summary)

    # 重叠: 交集 > 较小元素 30% 的配对 (祖先-后代关系跳过, 正常嵌套不误报)
    overlaps = _overlap_pairs(els)
    overlaps.sort(key=lambda t: t[0], reverse=True)
    if overlaps:
        lines.append(f"[重叠] {len(overlaps)} 处 (交集 > 较小元素 {OVERLAP_RATIO:.0%}):")
        for ratio, a, b, ix, iy in overlaps[:15]:
            lines.append(f"  - {_fmt_el(a)} 与 {_fmt_el(b)} 重叠 {ratio:.0%} ({ix}x{iy}px)")
        if len(overlaps) > 15:
            lines.append(f"  - …共 {len(overlaps)} 处")

    # 横向溢出 (造成横向滚动条)
    off_right = [e for e in visible if e.get("offRight")]
    if off_right:
        lines.append(f"[横向溢出] {len(off_right)} 个元素超出视口右侧:")
        for e in off_right[:5]:
            lines.append(
                f"  - {_fmt_el(e)} (x={e['x']} w={e['w']}, 右缘 {e['x'] + e['w']}px > 视口 {vw}px)"
            )
        if len(off_right) > 5:
            lines.append(f"  - …共 {len(off_right)} 个")

    # 内容截断/溢出 (scrollWidth > clientWidth); html/body 的文档级超宽已由横向溢出覆盖
    clipped = [
        e for e in visible
        if e["tag"] not in ("html", "body") and e["scrollW"] > e["clientW"] and e["w"] > 0
    ]
    if clipped:
        lines.append(f"[内容截断/溢出] {len(clipped)} 个元素内容超出容器:")
        for e in clipped[:10]:
            lines.append(f"  - {_fmt_el(e)} (内容宽 {e['scrollW']}px > 容器宽 {e['clientW']}px)")
        if len(clipped) > 10:
            lines.append(f"  - …共 {len(clipped)} 个")

    # 零尺寸
    zeros = [e for e in els if e.get("zero")]
    if zeros:
        names = ", ".join(_fmt_el(e) for e in zeros[:8])
        lines.append(f"[零尺寸] {len(zeros)} 个元素宽高为 0: {names}")

    # 首屏外 / 左越界 (参考性)
    below = [e for e in visible if e["y"] >= vh]
    if below:
        lines.append(f"[首屏外] {len(below)} 个元素位于首屏下方 (y ≥ {vh}px)")
    off_left = [e for e in visible if e.get("offLeft")]
    if off_left:
        lines.append(f"[左越界] {len(off_left)} 个元素完全在视口左侧之外")

    # 字号分布
    fonts = collections.Counter(e["font"] for e in els if e["font"] > 0)
    if fonts:
        top = ", ".join(f"{size}px(×{n})" for size, n in fonts.most_common(3))
        small = sum(n for size, n in fonts.items() if size < TINY_TEXT_PX)
        lines.append(f"[字号] 主要字号: {top}; 最小 {min(fonts)}px; <{TINY_TEXT_PX}px 共 {small} 个")

    # 对比度 (WCAG): 正文 4.5:1, 大字号 3:1; 背景含图片/渐变无法精确计算
    contrast = []
    bg_image_n = 0
    for e in els:
        if not e.get("text") or not e.get("fcolor"):
            continue
        if e.get("falpha", 1) == 0:  # 全透明文本无实际渲染, 跳过对比度判定
            continue
        bg = e.get("bg") or {}
        if bg.get("rgb") is None:
            if bg.get("limit") == "image":
                bg_image_n += 1
            continue
        ratio = _contrast_ratio(e["fcolor"], bg["rgb"])
        threshold = 3.0 if _is_large_text(e) else 4.5
        if ratio < threshold:
            contrast.append((ratio, e, threshold))
    if contrast:
        contrast.sort(key=lambda t: t[0])
        lines.append(f"[对比度] {len(contrast)} 个文本元素低于 WCAG 阈值:")
        for ratio, e, thr in contrast[:10]:
            lines.append(f"  - {_fmt_el(e)}「{e['text']}」对比度 {ratio:.2f}:1 (阈值 {thr}:1)")
        if len(contrast) > 10:
            lines.append(f"  - …共 {len(contrast)} 个")
    else:
        lines.append("[对比度] 未发现低于阈值的文字对比度问题。")
    if bg_image_n:
        lines.append(f"(注: {bg_image_n} 个元素背景含图片/渐变, 未参与对比度计算)")

    if not (overlaps or off_right or clipped or zeros):
        lines.append("未发现元素重叠、横向溢出、内容截断、零尺寸等明显布局问题。")
    return "\n".join(lines)


async def _describe_screenshot(path: Path, prompt: str = _VISION_PROMPT) -> str:
    """豆包视觉模型代看截图, 失败只降级提示, 不阻断其他诊断。"""
    try:
        return await asyncio.to_thread(vision.describe_image, str(path), prompt)
    except Exception as e:
        return f"(视觉描述不可用: {e})"


_LEVEL_MARK = {"error": "❌ ", "warn": "⚠ ", "info": ""}


def _format_design(findings: list[dict]) -> list[str]:
    """把设计审查 Finding 按分区聚合为报告文本行。"""
    if not findings:
        return []
    by_section: dict[str, list[dict]] = {}
    for f in findings:
        by_section.setdefault(f["分区"], []).append(f)
    lines = []
    for section, fs in by_section.items():
        lines.append(f"【{section}】")
        for f in fs:
            lines.append(f"- {_LEVEL_MARK.get(f.get('等级', ''), '')}{f['名称']}: {f['依据']}")
            lines.append(f"  建议: {f['建议']}")
        lines.append("")
    return lines


def _top_findings(findings: list[dict], n: int = 3) -> list[dict]:
    """按严重度取 top N: error > warn > info, 同等级保持原序。"""
    order = {"error": 0, "warn": 1, "info": 2}
    return sorted(findings, key=lambda f: order.get(f.get("等级"), 3))[:n]


def _format_loading(events: dict) -> list[str]:
    """【加载与渲染】监控报告: 无监控数据返回空, 有事件列出, 全绿则确认。"""
    if not events:
        return []
    lines = ["【加载与渲染】"]
    errs = []
    if events.get("pageerror"):
        errs.append(f"- 未捕获 JS 异常 {len(events['pageerror'])} 个: {'; '.join(events['pageerror'][:3])}")
    if events.get("failed"):
        errs.append(f"- 请求失败 {len(events['failed'])} 个: {'; '.join(events['failed'][:3])}")
    if events.get("console"):
        errs.append(f"- console 错误/警告 {len(events['console'])} 条: {'; '.join(events['console'][:3])}")
    if events.get("fonts"):
        errs.append(f"- 字体加载失败: {', '.join(events['fonts'][:3])}")
    if events.get("imgfail"):
        errs.append(f"- 图片加载失败 {len(events['imgfail'])} 张: {'; '.join(events['imgfail'][:3])}")
    if events.get("http"):
        errs.append(f"- HTTP ≥400 响应 {len(events['http'])} 个: {'; '.join(events['http'][:3])}")
    if errs:
        lines += errs
    else:
        lines.append("- 无 JS 错误/请求失败/字体或图片加载失败, 渲染稳定")
    return lines


async def analyze_page(
    url: str,
    viewport_width: int = 1440,
    viewport_height: int = 900,
    timeout: int = 30,
    depth: str = "standard",
) -> str:
    """渲染并分析网页, 返回纯文本报告 (布局诊断 + 设计系统审查 + 加载监控 + 视觉描述 + 截图路径)。

    depth: "quick" (基础布局 + 加载监控) / "standard" (默认, 全部静态设计检查) /
           "deep" (standard + hover 交互态采样)。
    """
    if not (0 < viewport_width <= 4096):
        raise ValueError(f"参数错误: viewport_width 需 >0 且 ≤4096, 当前 {viewport_width}")
    if not (0 < viewport_height <= 4096):
        raise ValueError(f"参数错误: viewport_height 需 >0 且 ≤4096, 当前 {viewport_height}")
    if not (1 <= timeout <= 300):
        raise ValueError(f"参数错误: timeout 需 1~300 秒, 当前 {timeout}")
    if depth not in ("quick", "standard", "deep"):
        raise ValueError(f"参数错误: depth 需为 quick/standard/deep, 当前 {depth}")
    target = _normalize_url(url)
    pw = browser = None
    screenshot = None
    findings: list[dict] = []
    events: dict = {}
    try:
        pw, browser = await _launch_browser()
        page = await _render_page(
            browser, target, viewport_width, viewport_height, timeout,
            monitor=depth != "quick", events=events,
        )
        try:
            metrics = await page.evaluate(_LAYOUT_JS)
        except Exception as e:
            raise RuntimeError(f"页面脚本执行失败: {e}") from e

        # 超高页面降级为视口截图 (豆包视觉模型对超长图识别差, 且避免引入 Pillow)
        full_page = metrics["doc"]["h"] <= MAX_FULLPAGE_HEIGHT
        screenshot = await _save_screenshot(page, full_page=full_page)

        # 设计系统审查 (standard/deep): 采集与判定与布局诊断独立, 互不影响
        if depth != "quick":
            try:
                design_metrics = await page.evaluate(design._DESIGN_JS)
            except Exception:
                design_metrics = None
            if design_metrics:
                findings = design.analyze_design(design_metrics, depth, viewport_width)
                if depth == "deep":
                    findings += await design.collect_hover_states(page)
    finally:
        if browser is not None:
            await browser.close()
            browser = None
        if pw is not None:
            await pw.stop()
            pw = None

    # 截图已落盘, 浏览器已关闭; 以下纯分析 (不依赖浏览器)
    parts = ["【布局诊断】"]
    if not full_page:
        parts.append(f"注: 页面总高超过 {MAX_FULLPAGE_HEIGHT}px, 截图仅含首屏 (布局指标仍为全页数据)。")
    parts.append(_analyze_layout(metrics))
    parts += _format_design(findings)
    parts += _format_loading(events)
    prompt = _VISION_PROMPT
    if findings:
        prompt += "\n\n【程序化发现复核】以下是程序化检测发现的最严重问题, 请逐一确认/反驳/补充:\n" + \
            "\n".join(f"- {f['依据']}" for f in _top_findings(findings, 3))
    parts += ["", "【视觉描述】", await _describe_screenshot(screenshot, prompt)]
    parts += ["", f"【截图已保存】{screenshot}"]
    return "\n".join(parts)


async def _render_and_metrics(browser, url: str, width: int, height: int, timeout_s: int):
    """渲染并提取布局指标, 返回 (page, metrics)。"""
    page = await _render_page(browser, url, width, height, timeout_s)
    try:
        metrics = await page.evaluate(_LAYOUT_JS)
    except Exception as e:
        raise RuntimeError(f"页面脚本执行失败: {e}") from e
    return page, metrics


def _layout_signals(metrics: dict) -> dict:
    """跨视口对比用的轻量信号计数, 判定逻辑与 _analyze_layout 同源。"""
    els = metrics["els"]
    vh = metrics["viewport"]["h"]
    visible = [e for e in els if not e.get("zero") and e["w"] > 0 and e["h"] > 0]
    clipped = [
        e for e in visible
        if e["tag"] not in ("html", "body") and e["scrollW"] > e["clientW"] and e["w"] > 0
    ]
    fonts = [e["font"] for e in els if e["font"] > 0]
    return {
        "hscroll": bool(metrics.get("hasHScroll")),
        "overlaps": len(_overlap_pairs(els)),
        "offRight": sum(1 for e in visible if e.get("offRight")),
        "clipped": len(clipped),
        "zeros": sum(1 for e in els if e.get("zero")),
        "below": sum(1 for e in visible if e["y"] >= vh),
        "smallFont": sum(1 for f in fonts if f < TINY_TEXT_PX),
    }


_RESPONSIVE_VISION_PROMPT = (
    "这是某个网页的截图 (分析视口中较宽的一档)。请用中文从 UI/UX 角度描述: 布局结构、"
    "主要配色、字体排版、视觉层次, 并指出明显视觉问题。控制在 200 字以内。"
)


async def analyze_responsive(
    url: str,
    viewports: list[list[int]] | None = None,
    timeout: int = 30,
) -> str:
    """多视口渲染同一页面: 逐档布局诊断 (含对比度) + 跨视口对比 + 最宽视口视觉描述。

    默认视口 [[375,812],[768,1024],[1440,900]] 覆盖手机/平板/桌面。
    视觉模型只调用一次 (最宽视口截图), 各视口截图保存到 output/pages/。
    """
    if not (1 <= timeout <= 300):
        raise ValueError(f"参数错误: timeout 需 1~300 秒, 当前 {timeout}")
    if viewports is not None:
        for i, vp in enumerate(viewports):
            if not (isinstance(vp, (list, tuple)) and len(vp) == 2):
                raise ValueError(f"参数错误: viewports[{i}] 需为 [w, h] 格式, 当前 {vp}")
            w, h = vp
            if not (0 < w <= 4096):
                raise ValueError(f"参数错误: viewports[{i}] 宽度需 >0 且 ≤4096, 当前 {w}")
            if not (0 < h <= 4096):
                raise ValueError(f"参数错误: viewports[{i}] 高度需 >0 且 ≤4096, 当前 {h}")
    target = _normalize_url(url)
    vps = [tuple(v) for v in (viewports or [[375, 812], [768, 1024], [1440, 900]])]
    pw = browser = None
    try:
        pw, browser = await _launch_browser()
        rendered = []
        for w, h in vps:
            page, metrics = await _render_and_metrics(browser, target, w, h, timeout)
            design_metrics = None
            try:
                design_metrics = await page.evaluate(design._DESIGN_JS)
            except Exception:
                pass
            shot = await _save_screenshot(page, full_page=False)
            await page.context.close()
            rendered.append(((w, h), metrics, shot, design_metrics))
    finally:
        if browser is not None:
            await browser.close()
            browser = None
        if pw is not None:
            await pw.stop()
            pw = None

    # 截图已落盘, 浏览器已关闭; 以下纯分析 (不依赖浏览器)
    parts = []
    for (w, h), metrics, shot, design_metrics in rendered:
        parts.append(f"【视口 {w}x{h}】截图: {shot}")
        parts.append(_analyze_layout(metrics))
        if design_metrics:
            parts += _format_design(design.analyze_design(design_metrics, "light", w))
        parts.append("")

    # 跨视口对比
    names = {
        "hscroll": "横向滚动", "overlaps": "元素重叠", "offRight": "横向溢出",
        "clipped": "内容截断/溢出", "zeros": "零尺寸元素", "below": "首屏外元素",
        "smallFont": f"小字号(<{TINY_TEXT_PX}px)",
    }
    # 预计算所有视口的布局信号 (每视口算一次, 避免循环内重复计算)
    signals_map = {(w, h): _layout_signals(m) for (w, h), m, _, __ in rendered}
    parts.append("【跨视口对比】")
    found_any = False
    for key, name in names.items():
        present = [f"{w}x{h}" for (w, h), _, _, __ in rendered if signals_map[(w, h)][key]]
        if present:
            found_any = True
            parts.append(f"- {name}: {', '.join(present)}")
    largest = max(vps, key=lambda v: v[0] * v[1])
    smallest = min(vps, key=lambda v: v[0] * v[1])
    if smallest != largest:
        sig_small = signals_map[smallest]
        sig_large = signals_map[largest]
        only_small = [
            name for key, name in names.items()
            if sig_small[key] and not sig_large[key]
        ]
        if only_small:
            found_any = True
            parts.append(f"- 仅 {smallest[0]}x{smallest[1]} 出现: {', '.join(only_small)}")
    if not found_any:
        parts.append("- 各视口均未发现明显布局问题。")

    # 视觉: 只分析最大面积视口的截图 (控制 API 成本)
    largest_shot = next(shot for (w, h), _, shot, __ in rendered if (w, h) == largest)
    parts += ["", f"【视觉描述 (最宽视口 {largest[0]}x{largest[1]})】",
              await _describe_screenshot(largest_shot, _RESPONSIVE_VISION_PROMPT)]
    return "\n".join(parts)


# 元素信息提取: 在目标元素上 evaluate (元素作为参数传入)
_ELEM_INFO_JS = """
(el) => {
  const s = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  const cls = typeof el.className === 'string' ? el.className.trim() : '';
  // 最近的非 static 定位祖先 (绝对定位元素的上下文)
  let anc = el.parentElement, posAnc = null;
  while (anc && anc.nodeType === 1) {
    const as = getComputedStyle(anc);
    if (as.position !== 'static') {
      const ar = anc.getBoundingClientRect();
      posAnc = { tag: anc.tagName.toLowerCase(), id: anc.id || '',
                 cls: (typeof anc.className === 'string' ? anc.className.trim() : ''),
                 position: as.position, x: Math.round(ar.x),
                 y: Math.round(ar.y + window.scrollY),
                 w: Math.round(ar.width), h: Math.round(ar.height) };
      break;
    }
    anc = anc.parentElement;
  }
  return {
    tag: el.tagName.toLowerCase(), id: el.id || '', cls,
    x: Math.round(r.x), y: Math.round(r.y + window.scrollY),  // 文档坐标
    w: Math.round(r.width), h: Math.round(r.height),
    inView: r.top < window.innerHeight && r.bottom > 0
      && r.left < window.innerWidth && r.right > 0,
    position: s.position, display: s.display,
    font: Math.round(parseFloat(s.fontSize) || 0), fontWeight: s.fontWeight,
    color: s.color, bg: s.backgroundColor, zIndex: s.zIndex,
    text: (el.textContent || '').trim().slice(0, 50),
    posAnc,
  };
}
"""

# 元素未命中时的候选提示: 页面标题 + 元素总数 + id/class 清单
_CANDIDATES_JS = """
() => {
  const ids = [...document.querySelectorAll('[id]')].map(e => e.id);
  const cls = new Set();
  document.querySelectorAll('[class]').forEach(e =>
    (e.className || '').split(/\\s+/).forEach(c => c && cls.add(c)));
  return { title: document.title, total: document.querySelectorAll('*').length,
           ids: ids.slice(0, 15), cls: [...cls].slice(0, 15) };
}
"""

_ELEM_VISION_PROMPT = (
    "这是网页中单个元素的裁剪特写截图。请从组件 UI 角度评审: 视觉样式、状态、"
    "可读性、间距、潜在问题。控制在 150 字以内。"
)


async def inspect_element(
    url: str,
    selector: str,
    viewport_width: int = 1440,
    viewport_height: int = 900,
    prompt: str | None = None,
    timeout: int = 30,
) -> str:
    """定位并特写分析页面中的单个元素: 几何信息 + 定位上下文 + 组件级视觉评审。

    支持 CSS 选择器 (主文档内; iframe 内容不在范围)。元素截图保存到 output/pages/。
    """
    if not selector or not isinstance(selector, str) or not selector.strip():
        raise ValueError("参数错误: selector 不能为空字符串")
    if not (0 < viewport_width <= 4096):
        raise ValueError(f"参数错误: viewport_width 需 >0 且 ≤4096, 当前 {viewport_width}")
    if not (0 < viewport_height <= 4096):
        raise ValueError(f"参数错误: viewport_height 需 >0 且 ≤4096, 当前 {viewport_height}")
    if not (1 <= timeout <= 300):
        raise ValueError(f"参数错误: timeout 需 1~300 秒, 当前 {timeout}")
    target = _normalize_url(url)
    pw = browser = None
    try:
        pw, browser = await _launch_browser()
        page = await _render_page(browser, target, viewport_width, viewport_height, timeout)
        try:
            loc = page.locator(selector)
            count = await loc.count()
        except Exception as e:
            raise RuntimeError(f"选择器无效: {selector} ({e})") from e
        if count == 0:
            cand = await page.evaluate(_CANDIDATES_JS)
            tail = selector.rsplit(" ", 1)[-1]
            token = tail[1:] if tail[:1] in ("#", ".") else tail
            hit_ids = [i for i in cand["ids"] if token and token in i]
            hit_cls = [c for c in cand["cls"] if token and token in c]
            msg = (f"未找到元素: {selector}。页面标题: {cand['title']}, 共 {cand['total']} 个元素。")
            if hit_ids or hit_cls:
                msg += " 类似选择器: " + ", ".join(
                    [f"#{i}" for i in hit_ids[:5]] + [f".{c}" for c in hit_cls[:5]]
                )
            else:
                msg += (f" 页面 id 示例: {', '.join('#' + i for i in cand['ids'][:6])}; "
                        f"class 示例: {', '.join('.' + c for c in cand['cls'][:6])}")
            raise RuntimeError(msg)
        note = f"匹配到 {count} 个元素, 分析文档序第一个。\n" if count > 1 else ""
        loc = loc.first
        await loc.scroll_into_view_if_needed()
        if await loc.bounding_box() is None:
            raise RuntimeError(f"元素不可见 (无渲染盒): {selector}")
        info = await loc.evaluate(_ELEM_INFO_JS)

        shot_dir = OUTPUT_DIR / "pages"
        shot_dir.mkdir(parents=True, exist_ok=True)
        shot = shot_dir / unique_filename("elem", ".png")
        await loc.screenshot(path=str(shot))
    finally:
        if browser is not None:
            await browser.close()
            browser = None
        if pw is not None:
            await pw.stop()
            pw = None

    # 截图已落盘, 浏览器已关闭; 以下纯分析 (不依赖浏览器)
    parts = ["【元素信息】"]
    if note:
        parts.append(note.strip())
    name = info["id"] and f"{info['tag']}#{info['id']}" or (
        info["cls"] and f"{info['tag']}.{info['cls'].split()[0]}" or info["tag"])
    parts.append(
        f"元素: {name}  |  位置 (文档坐标): x={info['x']} y={info['y']} "
        f"w={info['w']} h={info['h']}  |  在视口内: {'是' if info['inView'] else '否'}"
    )
    parts.append(
        f"样式: position={info['position']} display={info['display']} "
        f"z-index={info['zIndex']}  |  字号 {info['font']}px 字重 {info['fontWeight']} "
        f"颜色 {info['color']} 背景 {info['bg']}"
    )
    if info["text"]:
        parts.append(f"文本: 「{info['text']}」")
    if info["posAnc"]:
        p = info["posAnc"]
        pname = p["id"] and f"{p['tag']}#{p['id']}" or (
            p["cls"] and f"{p['tag']}.{p['cls'].split()[0]}" or p["tag"])
        parts.append(
            f"定位祖先: {pname} (position={p['position']}, "
            f"x={p['x']} y={p['y']} w={p['w']} h={p['h']})"
        )
    parts += ["", "【视觉描述】",
              await _describe_screenshot(shot, prompt or _ELEM_VISION_PROMPT)]
    parts += ["", f"【元素截图已保存】{shot}"]
    return "\n".join(parts)


# 无障碍审计: 6 项纯 DOM 检查, 不依赖视觉模型
_A11Y_JS = """
() => {
  function selPath(el) {           // tag#id.class:nth-of-type 链 (≤5 级)
    const parts = [];
    for (let cur = el; cur && cur.nodeType === 1 && parts.length < 5; cur = cur.parentElement) {
      let s = cur.tagName.toLowerCase();
      if (cur.id) s += '#' + cur.id;
      else if (cur.classList.length) s += '.' + [...cur.classList].slice(0, 2).join('.');
      if (cur.id) { parts.unshift(s); break; }
      const p = cur.parentElement;
      if (p && [...p.children].filter(c => c.tagName === cur.tagName).length > 1)
        s += ':nth-of-type(' + ([...p.children].indexOf(cur) + 1) + ')';
      parts.unshift(s);
    }
    return parts.join(' > ');
  }
  const out = { title: document.title, img: [], heading: [], empty: [],
                label: [], dupid: [], ambig: [] };
  // 1. img 缺 alt (排除 aria-hidden / role=presentation)
  document.querySelectorAll('img').forEach(el => {
    const alt = el.getAttribute('alt');
    const hidden = el.getAttribute('aria-hidden') === 'true'
      || el.getAttribute('role') === 'presentation';
    if (hidden) return;
    if (alt === null || alt.trim() === '') out.img.push({ path: selPath(el), text: '' });
  });
  // 2. 标题层级跳级 (h1->h3 标记, 连续 h2 不标记)
  let prev = 0;
  document.querySelectorAll('h1,h2,h3,h4,h5,h6').forEach(el => {
    const lv = +el.tagName[1];
    if (prev > 0 && lv > prev + 1)
      out.heading.push({ path: selPath(el), text: (el.textContent || '').trim().slice(0, 20) });
    prev = lv;
  });
  // 3. 链接/按钮无可见文本 (无 aria-label/title/带 alt 的 img)
  document.querySelectorAll('a, button').forEach(el => {
    if (el.getAttribute('aria-hidden') === 'true') return;
    const txt = (el.textContent || '').trim();
    const hasImgAlt = [...el.querySelectorAll('img')]
      .some(im => (im.getAttribute('alt') || '').trim());
    if (!txt && !el.getAttribute('aria-label') && !el.getAttribute('title') && !hasImgAlt)
      out.empty.push({ path: selPath(el), text: '' });
  });
  // 4. 表单控件无标签 (el.labels 覆盖 label[for] 与包裹式)
  document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=image]), select, textarea')
    .forEach(el => {
      const labelledby = el.getAttribute('aria-labelledby');
      const ok = el.getAttribute('aria-label')
        || (labelledby && document.getElementById(labelledby))
        || (el.labels && el.labels.length > 0);
      if (!ok) out.label.push({ path: selPath(el), text: (el.getAttribute('placeholder') || '').slice(0, 20) });
    });
  // 5. 重复 id
  const seen = {};
  document.querySelectorAll('[id]').forEach(el => (seen[el.id] = seen[el.id] || []).push(el));
  for (const id in seen) if (seen[id].length > 1)
    out.dupid.push({ path: seen[id].slice(0, 2).map(selPath).join(' / '), text: '#' + id });
  // 6. 歧义链接: 同文本映射到多个不同 href
  const byTxt = {};
  document.querySelectorAll('a[href]').forEach(el => {
    const href = (el.getAttribute('href') || '').trim();
    if (!href || href.startsWith('javascript:') || href === '#') return;
    const txt = (el.textContent || '').trim();
    if (!txt) return;
    (byTxt[txt] = byTxt[txt] || new Set()).add(href);
  });
  for (const txt in byTxt) if (byTxt[txt].size > 1)
    out.ambig.push({ path: '', text: txt.slice(0, 20) + ' (' + byTxt[txt].size + ' 个不同链接)' });
  return out;
}
"""

_AUDIT_LABELS = {
    "img": "图片缺少 alt",
    "heading": "标题层级跳级",
    "empty": "链接/按钮无文本",
    "label": "表单控件无标签",
    "dupid": "重复 id",
    "ambig": "歧义链接 (同文本多个目标)",
}


async def audit_page(url: str, timeout: int = 30) -> str:
    """纯程序化无障碍审计 (不调用视觉模型): alt/标题跳级/空文本/表单标签/重复 id/歧义链接。

    固定 1440x900 视口。返回中文报告, 每项检查列出计数与最多 5 个示例。
    """
    target = _normalize_url(url)
    pw = browser = None
    try:
        pw, browser = await _launch_browser()
        page = await _render_page(browser, target, 1440, 900, timeout)
        try:
            res = await page.evaluate(_A11Y_JS)
        except Exception as e:
            raise RuntimeError(f"页面脚本执行失败: {e}") from e
    finally:
        if browser is not None:
            await browser.close()
        if pw is not None:
            await pw.stop()

    parts = [f"【无障碍审计】{target}", f"页面标题: {res['title']}", ""]
    any_found = False
    for key, name in _AUDIT_LABELS.items():
        items = res[key]
        if not items:
            continue
        any_found = True
        parts.append(f"[{name}] {len(items)} 处:")
        for it in items[:5]:
            suffix = f"「{it['text']}」" if it.get("text") else ""
            parts.append(f"  - {it['path']}{suffix}")
        if len(items) > 5:
            parts.append(f"  - …共 {len(items)} 处")
        parts.append("")
    if not any_found:
        parts.append("未发现问题。")
    return "\n".join(parts)
