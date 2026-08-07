"""页面视觉回归对比: 两次渲染截图 -> 浏览器内 canvas 像素 diff + 合成图 + 视觉描述。

canvas 污染规避: file:// 文档加载 file:// 图片会使 canvas 变成 opaque origin,
getImageData 抛 SecurityError; data: URL 继承文档 origin 不污染, 故以 base64 data URL 传入。
所有结果以文字返回 (适配无视觉能力的 agent)。
"""
import asyncio
import base64
from pathlib import Path

from . import vision
from .page import _launch_browser, _normalize_url, _render_page
from .utils import OUTPUT_DIR, unique_filename

DIFF_COLS = 16           # 变更区域网格列数
DIFF_ROWS = 10           # 变更区域网格行数
DIFF_GAP = 40            # 合成图左右两半之间的间隔 (px)
DIFF_MAX_SIDE = 1280     # 像素计算降采样: 最长边上限 (px), 减小计算量与 base64 体积
DIFF_TOLERANCE = 16      # 像素差异容忍度 (每通道 0-255), 默认值

_DIFF_JS = """
async ({ a, b, tol, cols, rows, gap, maxSide }) => {
  const load = src => new Promise((res, rej) => {
    const im = new Image(); im.onload = () => res(im); im.onerror = rej; im.src = src;
  });
  const [imA0, imB0] = await Promise.all([load(a), load(b)]);
  if (imA0.naturalWidth !== imB0.naturalWidth || imA0.naturalHeight !== imB0.naturalHeight)
    throw new Error('图像尺寸不一致: A=' + imA0.naturalWidth + 'x' + imA0.naturalHeight
      + ' B=' + imB0.naturalWidth + 'x' + imB0.naturalHeight);
  // 降采样: 最长边不超过 maxSide, 减轻主线程像素计算与 base64 传输压力
  const scale = Math.min(1, maxSide / Math.max(imA0.naturalWidth, imA0.naturalHeight));
  const w = Math.max(1, Math.round(imA0.naturalWidth * scale));
  const h = Math.max(1, Math.round(imA0.naturalHeight * scale));
  const cA = document.createElement('canvas'); cA.width = w; cA.height = h;
  cA.getContext('2d').drawImage(imA0, 0, 0, w, h);
  const cB = document.createElement('canvas'); cB.width = w; cB.height = h;
  cB.getContext('2d').drawImage(imB0, 0, 0, w, h);
  const dA = new Uint8ClampedArray(cA.getContext('2d').getImageData(0, 0, w, h).data);
  const dB = cB.getContext('2d').getImageData(0, 0, w, h).data;
  const cellW = w / cols, cellH = h / rows;
  const cells = Array.from({ length: rows }, () => new Array(cols).fill(false));
  const mask = new Uint8ClampedArray(w * h);   // 变更像素掩码, 合成图红标用
  let diff = 0;
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const i = (y * w + x) * 4;
    if (Math.abs(dA[i] - dB[i]) > tol || Math.abs(dA[i+1] - dB[i+1]) > tol
        || Math.abs(dA[i+2] - dB[i+2]) > tol) {
      diff++; mask[y * w + x] = 1;
      cells[Math.min(rows - 1, (y / cellH) | 0)][Math.min(cols - 1, (x / cellW) | 0)] = true;
    }
  }
  let minX = cols, minY = rows, maxX = -1, maxY = -1;
  for (let cy = 0; cy < rows; cy++) for (let cx = 0; cx < cols; cx++)
    if (cells[cy][cx]) { minX = Math.min(minX, cx); maxX = Math.max(maxX, cx);
                         minY = Math.min(minY, cy); maxY = Math.max(maxY, cy); }
  // 合成图: 左旧右新 + 灰色间隔, 差异像素在两半都标红
  const comp = document.createElement('canvas');
  comp.width = w * 2 + gap; comp.height = h;
  const cc = comp.getContext('2d');
  cc.fillStyle = '#333'; cc.fillRect(0, 0, comp.width, comp.height);
  cc.drawImage(cA, 0, 0); cc.drawImage(cB, w + gap, 0);
  const hl = cc.getImageData(0, 0, comp.width, comp.height);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    if (!mask[y * w + x]) continue;
    const j = (y * comp.width + x) * 4;
    const k = (y * comp.width + x + w + gap) * 4;
    for (const idx of [j, k]) { hl.data[idx] = 255; hl.data[idx+1] = 0; hl.data[idx+2] = 0; hl.data[idx+3] = 255; }
  }
  cc.putImageData(hl, 0, 0);
  comp.id = 'diff-comp';
  comp.style.position = 'fixed'; comp.style.left = '0'; comp.style.top = '0';
  document.body.innerHTML = ''; document.body.appendChild(comp);
  return { w, h, ratio: diff / (w * h),
           changedCells: cells.flat().filter(Boolean).length,
           box: maxX < 0 ? null : { x0: Math.round(minX * cellW), y0: Math.round(minY * cellH),
                                    x1: Math.round((maxX + 1) * cellW), y1: Math.round((maxY + 1) * cellH) } };
}
"""

_DIFF_VISION_PROMPT = (
    "这是两张网页截图的新旧对比图: 左半为旧版, 右半为新版, 两图上红色高亮为差异区域。"
    "请用中文描述新版相对旧版的可见变化 (布局/内容/样式), 并指出可能的问题。"
    "注意: 如果两半完全相同、没有可见差异, 请直接说'无可见变化', 不要编造差异。"
    "控制在 200 字以内。"
)


def _data_url(path: Path) -> str:
    data = base64.b64encode(path.read_bytes()).decode()
    return f"data:image/png;base64,{data}"


async def _describe_image(path: Path, prompt: str) -> str:
    try:
        return await asyncio.to_thread(vision.describe_image, str(path), prompt)
    except Exception as e:
        return f"(视觉描述不可用: {e})"


async def diff_pages(
    page_a: str,
    page_b: str,
    viewport_width: int = 1440,
    viewport_height: int = 900,
    tolerance: int = DIFF_TOLERANCE,
    timeout: int = 30,
) -> str:
    """渲染两个页面 (URL 或本地文件) 并做像素级新旧对比, 返回差异报告 + 合成图。

    适合静态/本地页面; 动态内容 (广告/时间戳/动画) 会造成差异误报。
    像素 diff 在浏览器内 canvas 完成 (降采样最长边 1280, data URL 规避 file:// 污染)。
    截图与合成图保存到 output/pages/。
    """
    if not (0 < viewport_width <= 4096):
        raise ValueError(f"参数错误: viewport_width 需 >0 且 ≤4096, 当前 {viewport_width}")
    if not (0 < viewport_height <= 4096):
        raise ValueError(f"参数错误: viewport_height 需 >0 且 ≤4096, 当前 {viewport_height}")
    if tolerance < 0:
        raise ValueError(f"参数错误: tolerance 需 ≥0, 当前 {tolerance}")
    if not (1 <= timeout <= 300):
        raise ValueError(f"参数错误: timeout 需 1~300 秒, 当前 {timeout}")
    a_url = _normalize_url(page_a)
    b_url = _normalize_url(page_b)
    pw = browser = None
    diff_shot = None
    parts: list[str] = []
    try:
        pw, browser = await _launch_browser()
        shot_a = shot_b = None
        for label, u in (("A", a_url), ("B", b_url)):
            page = await _render_page(browser, u, viewport_width, viewport_height, timeout)
            shot_dir = OUTPUT_DIR / "pages"
            shot_dir.mkdir(parents=True, exist_ok=True)
            path = shot_dir / unique_filename(f"page_{label}", ".png")
            await page.screenshot(path=str(path), full_page=False)
            await page.context.close()
            if label == "A":
                shot_a = path
            else:
                shot_b = path

        parts = [f"【页面A】{a_url}  截图: {shot_a}",
                 f"【页面B】{b_url}  截图: {shot_b}", ""]
        # 独立页面承载合成画布 (about:blank, 与页面 A/B 的 DOM 隔离)
        dctx = await browser.new_context(
            viewport={"width": viewport_width * 2 + DIFF_GAP, "height": viewport_height}
        )
        dpage = await dctx.new_page()
        await dpage.goto("about:blank")
        result = await dpage.evaluate(
            _DIFF_JS,
            {"a": _data_url(shot_a), "b": _data_url(shot_b),
             "tol": tolerance, "cols": DIFF_COLS, "rows": DIFF_ROWS, "gap": DIFF_GAP,
             "maxSide": DIFF_MAX_SIDE},
        )
        diff_shot = OUTPUT_DIR / "pages" / unique_filename("diff", ".png")
        await dpage.locator("#diff-comp").screenshot(path=str(diff_shot))
        await dctx.close()
        parts.append("【像素差异】")
        parts.append(
            f"- 差异像素占比: {result['ratio'] * 100:.2f}% "
            f"(共 {int(result['ratio'] * result['w'] * result['h'])} px, 降采样最长边 {DIFF_MAX_SIDE}px)"
        )
        parts.append(f"- 变更区域 ({DIFF_COLS}x{DIFF_ROWS} 网格): {result['changedCells']} 个单元格")
        if result["box"]:
            b = result["box"]
            parts.append(f"- 变更包围盒: x {b['x0']}~{b['x1']}px, y {b['y0']}~{b['y1']}px")
        parts += ["", f"【差异合成图】{diff_shot} (左旧右新, 红色为差异)", ""]
    except Exception as e:
        parts.append(f"像素对比失败: {e} (已保留两张原截图)")
    finally:
        if browser is not None:
            await browser.close()
        if pw is not None:
            await pw.stop()
    # 浏览器已关, 再做视觉描述 (M7: 视觉调用期间不占浏览器内存)
    if diff_shot is not None:
        parts += ["【视觉描述】", await _describe_image(diff_shot, _DIFF_VISION_PROMPT)]
    return "\n".join(parts)
