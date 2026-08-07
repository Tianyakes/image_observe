"""美术审核: 渲染网页 -> 浏览器内像素统计 (配色/构图/留白) -> 调色板 -> 豆包美术审核员打分。

无新依赖: 像素统计在 about:blank 页 canvas 内完成 (data URL 规避 file:// 污染,
同 ui_diff); 视觉调用失败只降级不阻断; 所有结果以文字返回。

已知边界: 渐变/图片背景页面构图统计自动降级 (bg.stable=false); 摄影图页面显著色
偏多可能触发「色板破碎」warn (阈值取高缓解, 以美术评审为准); 只审首屏。
"""
import asyncio
import re

from . import vision
from .page import (
    _LEVEL_MARK,
    _launch_browser,
    _normalize_url,
    _render_page,
    _save_screenshot,
)
from .utils import image_to_data_url

# ── 像素统计常量 ────────────────────────────────────────────
_AESTHETIC_MAX_SIDE = 360    # 降采样最长边 (px)
_BG_EDGE_RING = 1            # 背景采样: 边缘环厚度 (px)
_BG_STABLE_SHARE = 0.55      # 边缘众数色占比下限, 低于视为渐变/图片背景
_CONTENT_DIST = 40           # 内容掩码: 加权 RGB 距离阈值
_MERGE_DIST = 144            # 色簇合并: 曼哈顿距离上限 (等价通道平均差 ≤48)
_SIGNIFICANT_SHARE = 0.02    # 显著色占比下限
_BIG_SHARE = 0.05            # 大面积色块占比下限
_SAT_HIGH = 0.8              # 高饱和判定 (HSL 饱和度)

# ── Python 判定阈值 ─────────────────────────────────────────
_TOP1_LOW = 0.35             # 主色占比低于此 → 缺乏主色
_TOP1_MONO = 0.85            # 主色占比高于此 → 色彩单调 (info)
_FRAGMENT_COUNT = 8          # 显著色超过此 → 色板破碎
_BIG_COLORS = 5              # ≥5% 面积色块超过此 → 大面积抢色
_SAT_WARN, _SAT_ERR = 0.08, 0.20
_ZONES_DISTINCT = 5          # 3x3 分区中与中心差异显著 (>60) 的格数阈值
_OCC_WARN, _OCC_ERR = 0.025, 0.01  # 文字笔画像素稀疏, 极简页首屏 2.5~3% 属正常
_OFFSET_WARN, _OFFSET_ERR = 0.35, 0.50
_LEFT_WARN, _LEFT_ERR = 0.65, 0.80
_WHITESPACE_RATIO = 3.0
_EMPTY_PAGE = 0.02           # 内容占比 <2% 视为空页, 抑制构图判定
_PALETTE_MIN_SHARE = 0.01    # 调色板去噪下限
_PALETTE_MAX = 8             # 调色板输出色数上限
_SEVERITY = {"error": 15, "warn": 8, "info": 3}  # 视觉失败时的程序化兜底分

_AESTHETIC_JS = r"""
async ({ img, maxSide }) => {
  const load = src => new Promise((res, rej) => {
    const im = new Image(); im.onload = () => res(im); im.onerror = rej; im.src = src;
  });
  const im = await load(img);
  const scale = Math.min(1, maxSide / Math.max(im.naturalWidth, im.naturalHeight));
  const w = Math.max(1, Math.round(im.naturalWidth * scale));
  const h = Math.max(1, Math.round(im.naturalHeight * scale));
  const cv = document.createElement('canvas'); cv.width = w; cv.height = h;
  const ctx = cv.getContext('2d');
  ctx.drawImage(im, 0, 0, w, h);
  const data = ctx.getImageData(0, 0, w, h).data;
  const keyOf = (r, g, b) => ((r >> 4) << 8) | ((g >> 4) << 4) | (b >> 4);
  const toHex = c => '#' + [c.r, c.g, c.b].map(v => Math.round(v).toString(16).padStart(2, '0')).join('');

  // 背景检测: 1px 边缘环量化众数色 (深色模式页面边缘即深色, 天然适配)
  const edgeBuckets = new Map();
  let edgeN = 0;
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    if (!(y < 1 || y >= h - 1 || x < 1 || x >= w - 1)) continue;
    const i = (y * w + x) * 4;
    const k = keyOf(data[i], data[i + 1], data[i + 2]);
    edgeBuckets.set(k, (edgeBuckets.get(k) || 0) + 1);
    edgeN++;
  }
  let bestKey = 0, bestN = -1;
  edgeBuckets.forEach((n, k) => { if (n > bestN) { bestN = n; bestKey = k; } });
  const bg = { r: ((bestKey >> 8 & 15) << 4) | 8, g: ((bestKey >> 4 & 15) << 4) | 8, b: ((bestKey & 15) << 4) | 8 };
  const edgeShare = edgeN ? bestN / edgeN : 0;
  const stable = edgeShare >= 0.55;

  // 单遍像素统计
  const buckets = new Map();
  let contentN = 0, sumX = 0, sumY = 0, leftN = 0;
  let minX = w, minY = h, maxX = -1, maxY = -1;
  let satHigh = 0, warm = 0, cool = 0, neutral = 0;
  const zones = Array.from({ length: 9 }, () => ({ r: 0, g: 0, b: 0, n: 0 }));
  const total = w * h;
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const i = (y * w + x) * 4;
    const r = data[i], g = data[i + 1], b = data[i + 2];
    buckets.set(keyOf(r, g, b), (buckets.get(keyOf(r, g, b)) || 0) + 1);
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
    if ((mx - mn) / 255 > 0.8) satHigh++;
    if (r - b > 30) warm++; else if (b - r > 30) cool++; else neutral++;
    const zx = Math.min(2, (x * 3 / w) | 0), zy = Math.min(2, (y * 3 / h) | 0);
    const z = zones[zy * 3 + zx];
    z.r += r; z.g += g; z.b += b; z.n++;
    const dr = r - bg.r, dg = g - bg.g, db = b - bg.b;
    if (Math.sqrt(0.299 * dr * dr + 0.587 * dg * dg + 0.114 * db * db) >= 40) {
      contentN++; sumX += x; sumY += y;
      if (x < w / 2) leftN++;
      if (x < minX) minX = x; if (x > maxX) maxX = x;
      if (y < minY) minY = y; if (y > maxY) maxY = y;
    }
  }

  // 色簇合并: 计数降序贪心并入已有簇 (曼哈顿 ≤144 ≈ 通道平均差 ≤48)
  const sorted = [...buckets.entries()].sort((a, b) => b[1] - a[1]);
  const clusters = [];
  const dist3 = (a, b) => Math.abs(a.r - b.r) + Math.abs(a.g - b.g) + Math.abs(a.b - b.b);
  for (const [k, n] of sorted) {
    const c = { r: ((k >> 8 & 15) << 4) | 8, g: ((k >> 4 & 15) << 4) | 8, b: ((k & 15) << 4) | 8 };
    let placed = false;
    for (const cl of clusters) {
      if (dist3(c, cl) <= _MERGE_) {
        const tn = cl.n + n;
        cl.r = Math.round((cl.r * cl.n + c.r * n) / tn);
        cl.g = Math.round((cl.g * cl.n + c.g * n) / tn);
        cl.b = Math.round((cl.b * cl.n + c.b * n) / tn);
        cl.n = tn;
        placed = true;
        break;
      }
    }
    if (!placed) clusters.push({ r: c.r, g: c.g, b: c.b, n });
  }
  clusters.sort((a, b) => b.n - a.n);
  const top = clusters.slice(0, 8).map(c => ({ hex: toHex(c), share: c.n / total }));

  const cx = contentN ? sumX / contentN / w : 0.5;
  const cy = contentN ? sumY / contentN / h : 0.5;
  return {
    w, h,
    bg: { hex: toHex(bg), edgeShare, stable },
    colors: {
      clusters: top,
      significant: clusters.filter(c => c.n / total >= 0.02).length,
      big: clusters.filter(c => c.n / total >= 0.05).length,
      satHigh: satHigh / total,
      warmth: { warm: warm / total, cool: cool / total, neutral: neutral / total },
      zones: zones.map(z => z.n ? toHex({ r: Math.round(z.r / z.n), g: Math.round(z.g / z.n), b: Math.round(z.b / z.n) }) : '#000000'),
    },
    content: {
      ratio: contentN / total,
      centroid: { x: cx, y: cy },
      centroidOffset: Math.sqrt((cx - 0.5) ** 2 + (cy - 0.5) ** 2),
      leftShare: contentN ? leftN / contentN : 0.5,
      bbox: {
        top: maxY < 0 ? 0 : minY / h, bottom: maxY < 0 ? 0 : (maxY + 1) / h,
        left: maxX < 0 ? 0 : minX / w, right: maxX < 0 ? 0 : (maxX + 1) / w,
      },
      spanX: maxX < 0 ? 0 : (maxX - minX + 1) / w,
      spanY: maxY < 0 ? 0 : (maxY - minY + 1) / h,
    },
  };
}
""".replace("_MERGE_", str(_MERGE_DIST))

_AESTHETIC_VISION_PROMPT = (
    "你是一位资深美术审核员,正在评审一个网页截图的美术质量。"
    "请严格按美术标准评审,不要只看功能或信息完整度。\n"
    "\n"
    "评审维度 (每个维度给一句结论: 良好/一般/失衡):\n"
    "1. 构图平衡: 元素在画面中的分布是否均衡,有无内容挤在角落或偏侧。\n"
    "2. 色彩协调: 主色/辅色/点缀色关系是否和谐,有无刺眼或冲突的配色。\n"
    "3. 比例和谐: 大块面与细节元素的比例关系 (如超大色块 vs 微小按钮)。\n"
    "4. 留白与呼吸感: 留白是否适度,有无拥挤或空旷到失衡。\n"
    "5. 视觉重心: 视线自然落点是否合理,有无被意外拉向空白区域。\n"
    "\n"
    "程序化指标 (来自像素统计,供佐证约束,判断以你看到的画面为准):\n"
    "{metrics}\n"
    "\n"
    "输出要求:\n"
    "1. 按五个维度各给一行中文点评。\n"
    "2. 给出总分,最后一行格式必须为: 总分: NN/100\n"
    "3. 给出发现摘要 2-4 条 (最需要修改的地方,每条一行)。\n"
    "控制在 400 字以内。只基于截图内容评审,不要编造画面中不存在的元素;"
    "若程序化指标与画面明显矛盾,以画面为准并在点评中说明差异。"
)


def _fmt(x: float) -> str:
    return f"{x * 100:.1f}%"


def _qualify(score: int) -> str:
    if score < 40:
        return "严重失衡"
    if score < 60:
        return "失衡"
    if score < 80:
        return "一般"
    return "良好"


def _hex2rgb(h: str) -> tuple:
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _zone_distinct(zones: list) -> int:
    """3x3 分区中与中心分区通道平均距离 >60 的格数。"""
    if len(zones) != 9:
        return 0
    center = _hex2rgb(zones[4])
    n = 0
    for z in zones:
        c = _hex2rgb(z)
        if (abs(c[0] - center[0]) + abs(c[1] - center[1]) + abs(c[2] - center[2])) / 3 > 60:
            n += 1
    return n


def analyze_aesthetics(m: dict) -> list[dict]:
    """把 `_AESTHETIC_JS` 的像素统计判定为 Finding 列表 (分区: 配色 / 构图与留白)。"""
    f: list[dict] = []
    add = lambda x: f.append(x)  # noqa: E731
    stable = m["bg"]["stable"]
    clusters = m["colors"]["clusters"]
    c = m["content"]
    ratio = c["ratio"]

    # C1 主色集中度
    if clusters:
        top1 = clusters[0]["share"]
        if top1 < _TOP1_LOW:
            add({"分区": "配色", "名称": "缺乏主色", "等级": "info" if not stable else "warn",
                 "依据": f"最大色簇仅占 {_fmt(top1)}, 没有视觉主色",
                 "建议": "确立一个主色 (占画面 40% 以上), 其余作为辅色与点缀"})
        elif top1 >= _TOP1_MONO and ratio < 0.15:
            add({"分区": "配色", "名称": "色彩单调", "等级": "info",
                 "依据": f"主色占 {_fmt(top1)}, 页面近乎单色",
                 "建议": "极简风可接受; 若显沉闷, 增加一个语义强调色"})

    # C2 色板破碎
    sig = m["colors"]["significant"]
    if sig > _FRAGMENT_COUNT:
        add({"分区": "配色", "名称": "色板破碎", "等级": "warn",
             "依据": f"显著色 {sig} 种 (>8), 配色无主次",
             "建议": "收敛到主色/辅色/点缀色体系; 含照片的页面可能误报, 以美术评审为准"})

    # C2b 大面积抢色
    big = m["colors"]["big"]
    if big >= _BIG_COLORS:
        add({"分区": "配色", "名称": "大面积抢色", "等级": "warn",
             "依据": f"{big} 种颜色各占 ≥5% 画面, 无主次之分",
             "建议": "压缩大面积色块数量, 让一个主色主导画面"})

    # C3 高饱和刺眼
    sat = m["colors"]["satHigh"]
    if sat >= _SAT_ERR:
        add({"分区": "配色", "名称": "高饱和刺眼", "等级": "error",
             "依据": f"高饱和 (饱和度>80%) 面积 {_fmt(sat)} (≥20%)",
             "建议": "降低大面积高饱和色的明度/面积占比, 饱和度控制 <80%"})
    elif sat >= _SAT_WARN:
        add({"分区": "配色", "名称": "高饱和偏多", "等级": "warn",
             "依据": f"高饱和面积 {_fmt(sat)} (≥8%)",
             "建议": "高饱和色只用于点缀, 控制其面积占比"})

    # C4 色彩分区过碎
    distinct = _zone_distinct(m["colors"]["zones"])
    if distinct >= _ZONES_DISTINCT:
        add({"分区": "配色", "名称": "色彩分区过碎", "等级": "info" if not stable else "warn",
             "依据": f"3x3 分区中 {distinct} 个分区与中心色彩差异显著",
             "建议": "让配色在画面中连贯分布, 避免局部浓墨重彩其余苍白"})

    # 全局 A: 渐变/图片背景 → 构图统计降 info
    degraded = not stable
    # 全局 B: 空页 → 抑制构图判定
    if ratio < _EMPTY_PAGE:
        # G1 内容占用率
        if ratio < _OCC_ERR:
            add({"分区": "构图与留白", "名称": "页面近乎空白", "等级": "error",
                 "依据": f"内容像素仅占 {_fmt(ratio)} (<1%)",
                 "建议": "页面几乎无内容, 检查渲染是否失败或内容是否确实过少"})
        elif ratio < _OCC_WARN:
            add({"分区": "构图与留白", "名称": "内容占用过低", "等级": "warn",
                 "依据": f"内容像素仅占 {_fmt(ratio)} (<3%)",
                 "建议": "内容过少导致画面空洞, 增加内容密度或收紧留白"})
        return f  # 空页无构图可言

    # G1 内容占用率
    if ratio < _OCC_ERR:
        add({"分区": "构图与留白", "名称": "内容占用过低", "等级": "error",
             "依据": f"内容像素仅占 {_fmt(ratio)} (<1%)",
             "建议": "内容过少, 画面严重空洞"})
    elif ratio < _OCC_WARN:
        add({"分区": "构图与留白", "名称": "内容占用偏低", "等级": "warn" if not degraded else "info",
             "依据": f"内容像素仅占 {_fmt(ratio)} (<3%)",
             "建议": "内容偏少, 检查是否挤压在局部区域"})

    # G2 视觉重心偏移
    off = c["centroidOffset"]
    long_page = c["spanY"] > 0.8
    if off > _OFFSET_ERR and not long_page:
        add({"分区": "构图与留白", "名称": "视觉重心严重偏移", "等级": "error" if not degraded else "info",
             "依据": f"内容质心距画面中心 {off:.2f} (对角线最大 ~0.71)",
             "建议": "内容几乎全部集中在画面一侧/一角, 重新平衡构图"})
    elif off > _OFFSET_WARN and not long_page:
        add({"分区": "构图与留白", "名称": "视觉重心偏移", "等级": "warn" if not degraded else "info",
             "依据": f"内容质心距画面中心 {off:.2f}",
             "建议": "内容明显偏向一侧, 考虑居中或对称排布"})

    # G3 左右失衡
    ls = c["leftShare"]
    if ls >= _LEFT_ERR or ls <= 1 - _LEFT_ERR:
        add({"分区": "构图与留白", "名称": "左右严重失衡", "等级": "error" if not degraded else "info",
             "依据": f"左半内容占 {_fmt(ls)}",
             "建议": "内容几乎全部在画面一侧, 平衡左右分布"})
    elif ls >= _LEFT_WARN or ls <= 1 - _LEFT_WARN:
        add({"分区": "构图与留白", "名称": "左右偏置", "等级": "warn" if not degraded else "info",
             "依据": f"左半内容占 {_fmt(ls)}",
             "建议": "有意非对称设计可忽略, 否则平衡左右内容量"})

    # G4/G5 上下留白失衡 / 内容偏上偏下 (spanY<0.5 时)
    if c["spanY"] < 0.5:
        b = c["bbox"]
        top_margin, bottom_margin = b["top"], 1 - b["bottom"]
        both_spacious = top_margin > 0.2 and bottom_margin > 0.2  # 居中留白豁免
        if not both_spacious and max(top_margin, bottom_margin) / max(min(top_margin, bottom_margin), 0.001) > _WHITESPACE_RATIO:
            add({"分区": "构图与留白", "名称": "上下留白失衡", "等级": "warn" if not degraded else "info",
                 "依据": f"上留白 {_fmt(top_margin)} vs 下留白 {_fmt(bottom_margin)} (>3:1)",
                 "建议": "垂直方向平衡留白, 或明确表达设计意图"})
        cy = c["centroid"]["y"]
        if cy < 0.32 or cy > 0.68:
            add({"分区": "构图与留白", "名称": "内容偏上/偏下", "等级": "warn" if not degraded else "info",
                 "依据": f"内容质心纵向 {cy:.2f} (0=顶部 1=底部)",
                 "建议": "内容明显偏向垂直方向一端, 重新平衡"})

    return f


def _build_palette(m: dict) -> list[str]:
    """从色簇提取主色/辅色/点缀色 (去噪 <1%, 点缀色需与主/辅有区分度)。"""
    clusters = [c for c in m["colors"]["clusters"] if c["share"] >= _PALETTE_MIN_SHARE]
    if m["content"]["ratio"] < _EMPTY_PAGE or not clusters:
        return ["主色: 背景色, 页面内容极少, 调色板参考价值有限"]

    def dist(a: dict, b: dict) -> float:
        ca, cb = _hex2rgb(a["hex"]), _hex2rgb(b["hex"])
        return (abs(ca[0] - cb[0]) + abs(ca[1] - cb[1]) + abs(ca[2] - cb[2])) / 3

    lines = [f"主色: {clusters[0]['hex']} (占比 {round(clusters[0]['share'] * 100)}%)"]
    sec = [c for c in clusters[1:3] if c["share"] >= _SIGNIFICANT_SHARE]
    if sec:
        lines.append("辅色: " + ", ".join(f"{c['hex']} (占比 {round(c['share'] * 100)}%)" for c in sec))
    accents: list = []
    for c in clusters[3:]:
        if len(accents) >= _PALETTE_MAX - 1 - len(sec):
            break
        if dist(c, clusters[0]) > 60 and all(dist(c, s) > 60 for s in sec):
            accents.append(c)
    if accents:
        lines.append("点缀色: " + ", ".join(f"{c['hex']} (占比 {round(c['share'] * 100)}%)" for c in accents))
    return lines


def _format_color_stats(m: dict) -> list[str]:
    bg = m["bg"]
    cols = m["colors"]
    lines = ["(统计基于首屏截图, 像素降采样最长边 360px)"]
    stable_note = "" if bg["stable"] else " [渐变/图片背景]"
    lines.append(f"- 背景色: {bg['hex']} (边缘一致度 {_fmt(bg['edgeShare'])}){stable_note}")
    if cols["clusters"]:
        main = cols["clusters"][0]
        sub = ", ".join(f"{c['hex']} ({_fmt(c['share'])})" for c in cols["clusters"][1:3])
        lines.append(f"- 主色: {main['hex']} ({_fmt(main['share'])})" + (f"  |  辅色: {sub}" if sub else ""))
    lines.append(f"- 显著色 {cols['significant']} 种; 大面积色块 {cols['big']} 种")
    wt = cols["warmth"]
    lines.append(f"- 高饱和面积 {_fmt(cols['satHigh'])}; 色温 暖 {_fmt(wt['warm'])} / 冷 {_fmt(wt['cool'])} / 中性 {_fmt(wt['neutral'])}")
    lines.append(f"- 色彩分区 (3x3): {_zone_distinct(cols['zones'])} 个分区与中心差异显著")
    return lines


def _format_composition(m: dict) -> list[str]:
    c = m["content"]
    b = c["bbox"]
    return [
        f"- 内容占用 {_fmt(c['ratio'])}",
        f"- 视觉重心 ({c['centroid']['x']:.2f}, {c['centroid']['y']:.2f}), 偏移中心 {c['centroidOffset']:.2f}",
        f"- 左右内容占比 {_fmt(c['leftShare'])} : {_fmt(1 - c['leftShare'])}",
        f"- 内容包围盒: 上 {_fmt(b['top'])} / 下 {_fmt(b['bottom'])} / 左 {_fmt(b['left'])} / 右 {_fmt(b['right'])}",
    ]


def _vision_evidence(m: dict, findings: list[dict]) -> str:
    """供美术评审 prompt 内嵌的程序化指标串。"""
    c = m["content"]
    cols = m["colors"]
    main = cols["clusters"][0] if cols["clusters"] else None
    lines = [
        f"- 内容占用率: {_fmt(c['ratio'])}   视觉重心偏移: {c['centroidOffset']:.2f} (距画面中心)",
        f"- 左右内容占比: {_fmt(c['leftShare'])} : {_fmt(1 - c['leftShare'])}"
        + (f"   主色 {main['hex']} 占 {_fmt(main['share'])}" if main else ""),
        f"- 显著色 {cols['significant']} 种, 高饱和面积 {_fmt(cols['satHigh'])}",
        f"- 内容包围盒: 上 {_fmt(c['bbox']['top'])} / 下 {_fmt(c['bbox']['bottom'])}",
    ]
    if findings:
        marks = {"error": "❌", "warn": "⚠", "info": ""}
        lines.append("- 程序化发现: " + " ".join(f"[{marks.get(f['等级'], '')}{f['名称']}]" for f in findings[:4]))
    return "\n".join(lines)


def _parse_score(text: str) -> int | None:
    """从美术评审输出解析 "总分: NN/100"。"""
    m = re.search(r"总分[：:]\s*(\d{1,3})", text)
    if not m:
        return None
    s = int(m.group(1))
    return s if 0 <= s <= 100 else None


def _fallback_score(findings: list[dict]) -> int:
    """视觉失败时的程序化兜底分: 100 - 发现严重度加权。"""
    if not findings:
        return 88  # 无证据不轻易给满分
    return max(10, 100 - sum(_SEVERITY.get(f.get("等级"), 0) for f in findings))


async def _describe(path, prompt: str) -> str:
    """豆包美术审核员代看截图, 失败只降级提示, 不阻断像素统计结果。"""
    try:
        return await asyncio.to_thread(vision.describe_image, str(path), prompt)
    except Exception as e:
        return f"(视觉描述不可用: {e})"


async def aesthetic_audit(
    url: str,
    viewport_width: int = 1440,
    viewport_height: int = 900,
    timeout: int = 30,
) -> str:
    """渲染网页并按美术标准审核: 像素统计 (配色/构图/留白/调色板) + 美术审核员 0-100 分。

    只审首屏截图 (构图/留白以首屏为分析单元); 视觉调用失败降级为程序化估算分。
    """
    if not (0 < viewport_width <= 4096):
        raise ValueError(f"参数错误: viewport_width 需 >0 且 ≤4096, 当前 {viewport_width}")
    if not (0 < viewport_height <= 4096):
        raise ValueError(f"参数错误: viewport_height 需 >0 且 ≤4096, 当前 {viewport_height}")
    if not (1 <= timeout <= 300):
        raise ValueError(f"参数错误: timeout 需 1~300 秒, 当前 {timeout}")
    target = _normalize_url(url)
    pw = browser = None
    shot = None
    metrics = None
    try:
        pw, browser = await _launch_browser()
        page = await _render_page(browser, target, viewport_width, viewport_height, timeout)
        shot = await _save_screenshot(page, full_page=False)
        await page.context.close()
        # 独立页面承载像素统计 (about:blank, 与目标页 DOM 隔离)
        dctx = await browser.new_context(
            viewport={"width": viewport_width, "height": viewport_height}
        )
        dpage = await dctx.new_page()
        await dpage.goto("about:blank")
        metrics = await dpage.evaluate(
            _AESTHETIC_JS, {"img": image_to_data_url(str(shot)), "maxSide": _AESTHETIC_MAX_SIDE}
        )
        await dctx.close()
    finally:
        # 视觉调用前浏览器必须已关 (M7 约定)
        if browser is not None:
            await browser.close()
        if pw is not None:
            await pw.stop()

    if shot is None or metrics is None:
        return "美术审核失败: 未能获取截图或像素统计"

    findings = analyze_aesthetics(metrics)
    parts = ["【色彩统计】", *_format_color_stats(metrics),
             "", "【构图统计】", *_format_composition(metrics)]

    prompt = _AESTHETIC_VISION_PROMPT.replace("{metrics}", _vision_evidence(metrics, findings))
    review = await _describe(shot, prompt)
    score = _parse_score(review)
    if score is None:
        score = _fallback_score(findings)
        parts += ["", "【美术评审】", review,
                  "", f"总分: {score}/100 (定性: {_qualify(score)}) (程序化估算)"]
    else:
        parts += ["", "【美术评审】", review, "", f"总分: {score}/100 (定性: {_qualify(score)})"]

    parts += ["", "【发现摘要】"]
    if findings:
        for f in findings:
            parts.append(f"- {_LEVEL_MARK.get(f['等级'], '')}{f['名称']}: {f['依据']}")
            parts.append(f"  建议: {f['建议']}")
    else:
        parts.append("- 未发现明显美术失衡。")
    parts += ["", "【调色板】", *_build_palette(metrics)]
    parts += ["", f"【截图已保存】{shot}"]
    return "\n".join(parts)
