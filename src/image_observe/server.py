"""image_observe MCP server: 为任何 MCP 兼容 Agent 提供视觉能力。

运行: uv run python -m image_observe.server (默认 stdio 传输)
"""
import asyncio

from mcp.server.mcpserver.server import MCPServer

from . import edit, generate, search, three_d, video, vision

server = MCPServer(name="image-observe", version="0.4.0")


@server.tool()
def describe_image(image: str, prompt: str | None = None) -> str:
    """理解图片内容并返回文字描述 (需要账号开通豆包视觉模型)。

    Args:
        image: 本地图片绝对路径, 或 http(s):// 图片 URL。
        prompt: 可选的自定义提问, 不填则输出详细描述。
    """
    return vision.describe_image(image, prompt)


@server.tool()
def generate_image(
    prompt: str,
    model: str | None = None,
    size: str = "2K",
    watermark: bool = True,
) -> str:
    """使用 Seedream 生成图片, 返回 URL 并保存到本地 output/ 目录。

    Args:
        prompt: 生图提示词 (中文/英文均可)。
        model: 可指定 doubao-seedream-3-0-t2i-250415 / 4-0-250828 /
               4-5-251128 / 5-0-lite-260128 / 5-0-pro-260628, 不填用默认。
        size: "2K" / "4K" (Seedream 3.0 需传像素值如 "2048x2048")。
        watermark: 是否添加 "AI生成" 水印。
    """
    return generate.generate_image(prompt, model, size, watermark)


@server.tool()
def edit_image(
    image: str,
    prompt: str,
    model: str | None = None,
    size: str | None = None,
    watermark: bool = True,
    scale: int | None = None,
) -> str:
    """使用 SeedEdit 按自然语言指令编辑图片, 返回 URL 并保存到本地。

    Args:
        image: 源图本地绝对路径或 http(s) URL。
        prompt: 编辑指令, 如 "把背景换成星空, 保持主体不变"。
        model: 不填用默认 doubao-seededit-3-0-i2i-250628。
        size: 可选 "1K"/"2K"/"4K" 或像素值, 不填保持源图比例 ("adaptive")。
        watermark: 是否添加 "AI生成" 水印。
        scale: 指令遵循强度 1~10。
    """
    return edit.edit_image(image, prompt, model, size, watermark, scale)


@server.tool()
def generate_video(
    prompt: str,
    model: str | None = None,
    image: str | None = None,
    ratio: str = "16:9",
    resolution: str = "720p",
    duration: int | None = None,
    generate_audio: bool = True,
    watermark: bool = False,
) -> str:
    """使用 Seedance 生成视频 (需等待 1~3 分钟), 返回 URL 并保存到 output/videos/。

    Args:
        prompt: 视频内容描述。
        model: 不填用默认 doubao-seedance-2-0-260128; 传 image 时可用 i2v 模型
               (doubao-seedance-1-0-lite-i2v-250428 / wan2-1-14b-i2v-250225)。
        image: 可选首帧图 (本地路径或 URL), 提供则为首帧图生视频。
        ratio: "16:9" / "4:3" / "1:1" / "3:4" / "9:16" / "21:9" / "adaptive"。
        resolution: "480p" / "720p" / "1080p" (fast 版不支持 1080p)。
        duration: 时长秒数 4~15 (2.0), 不传由模型自动决定。
        generate_audio: 是否生成同步音频。
        watermark: 是否添加水印。
    """
    return video.generate_video(
        prompt, model, image, ratio, resolution, duration, generate_audio, watermark
    )


@server.tool()
async def analyze_page(
    url: str,
    viewport_width: int = 1440,
    viewport_height: int = 900,
    timeout: int = 30,
    depth: str = "standard",
) -> str:
    """渲染并分析网页: 程序化布局诊断 (重叠/溢出/截断/字号/对比度) + 设计系统审查 (排版阶梯/间距/色彩/圆角/触摸目标/动效等) + 加载监控 (JS 错误/字体图片失败) + 豆包视觉模型设计描述 (自动复核程序化发现), 全部以文字返回, 截图保存到 output/pages/。约需 10~60 秒。

    Args:
        url: http(s):// 网址, 或本地 HTML 文件路径 / file:// URL。
        viewport_width / viewport_height: 浏览器视口大小。
        timeout: 页面加载超时秒数。
        depth: 审查深度 "quick" (基础布局+加载监控) / "standard" (默认, 全部静态设计检查) / "deep" (standard + hover 交互态采样)。
    """
    from . import page
    return await page.analyze_page(url, viewport_width, viewport_height, timeout, depth)


@server.tool()
async def analyze_responsive(
    url: str,
    viewports: list[list[int]] | None = None,
    timeout: int = 30,
) -> str:
    """多视口渲染同一页面: 逐档布局诊断 (含对比度) + 跨视口对比 + 最宽视口视觉描述, 全部以文字返回, 各视口截图保存到 output/pages/。约需 20~90 秒。

    Args:
        url: http(s):// 网址, 或本地 HTML 文件路径 / file:// URL。
        viewports: 视口列表, 默认 [[375,812],[768,1024],[1440,900]] 覆盖手机/平板/桌面。
        timeout: 页面加载超时秒数。
    """
    from . import page
    return await page.analyze_responsive(url, viewports, timeout)


@server.tool()
async def inspect_element(
    url: str,
    selector: str,
    viewport_width: int = 1440,
    viewport_height: int = 900,
    prompt: str | None = None,
    timeout: int = 30,
) -> str:
    """定位并特写分析页面中的单个元素: 几何信息 + 定位上下文 + 豆包组件级视觉评审, 元素截图保存到 output/pages/。

    Args:
        url: http(s):// 网址, 或本地 HTML 文件路径 / file:// URL。
        selector: CSS 选择器 (主文档内; iframe 内容不在范围), 如 "#hero" / ".card"。
        prompt: 可选的自定义评审要求。
        timeout: 页面加载超时秒数。
    """
    from . import page
    return await page.inspect_element(
        url, selector, viewport_width, viewport_height, prompt, timeout
    )


@server.tool()
async def diff_pages(
    page_a: str,
    page_b: str,
    viewport_width: int = 1440,
    viewport_height: int = 900,
    tolerance: int = 16,
    timeout: int = 30,
) -> str:
    """渲染两个页面做像素级新旧对比: 差异占比/区域 + 红标合成图 + 豆包描述新版变化, 截图保存到 output/pages/。适合静态/本地页面 (改代码前后对比), 动态内容会误报差异。

    Args:
        page_a / page_b: http(s):// 网址, 或本地 HTML 文件路径 / file:// URL。
        tolerance: 像素通道容差 (默认 16/255)。
        timeout: 页面加载超时秒数。
    """
    from . import ui_diff
    return await ui_diff.diff_pages(
        page_a, page_b, viewport_width, viewport_height, tolerance, timeout
    )


@server.tool()
async def audit_page(url: str, timeout: int = 30) -> str:
    """纯程序化无障碍审计 (不调用视觉模型): 图片 alt / 标题层级跳级 / 空文本链接按钮 / 表单控件标签 / 重复 id / 歧义链接。

    Args:
        url: http(s):// 网址, 或本地 HTML 文件路径 / file:// URL。
        timeout: 页面加载超时秒数。
    """
    from . import page
    return await page.audit_page(url, timeout)


@server.tool()
async def aesthetic_audit(
    url: str,
    viewport_width: int = 1440,
    viewport_height: int = 900,
    timeout: int = 30,
) -> str:
    """渲染网页并按美术标准审核视觉美感: 程序化像素统计 (配色/构图/留白/调色板) + 豆包美术审核员逐维度评审并打出 0-100 分, 截图保存到 output/pages/。约需 10~60 秒。适合排查配色与大小比例严重失衡的页面。

    Args:
        url: http(s):// 网址, 或本地 HTML 文件路径 / file:// URL。
        viewport_width / viewport_height: 浏览器视口大小。
        timeout: 页面加载超时秒数。
    """
    from . import aesthetic
    return await aesthetic.aesthetic_audit(url, viewport_width, viewport_height, timeout)


@server.tool()
def extract_text(image: str, language: str | None = None) -> str:
    """从图片中提取文字 (OCR), 逐行返回原文。

    Args:
        image: 本地图片绝对路径, 或 http(s):// 图片 URL。
        language: 可选目标语言, 提供时先提取原文再翻译为该语言。
    """
    return vision.extract_text(image, language)


@server.tool()
def search_images(query: str, count: int = 5, verify: bool = True) -> str:
    """搜索网络图片并返回验证过的链接列表 (适合无法直接看图的 agent)。

    Args:
        query: 图片需求描述, 如 "黑洞 电影 星际穿越"。
        count: 最多返回几张。
        verify: 是否用视觉模型逐张验证内容是否符合需求 (建议开启)。
    """
    if not isinstance(count, int) or count < 1 or count > 20:
        raise ValueError("参数错误: count 必须在 1~20 之间")
    return search.search_images(query, count, verify)


@server.tool()
def generate_3d(
    image: str,
    prompt: str | None = None,
    model: str | None = None,
    file_format: str = "glb",
) -> str:
    """使用 Seed3D 从图片生成 3D 模型 (需等待数分钟), 返回 URL 并保存到 output/3d/。

    Args:
        image: 参考图本地绝对路径或 http(s) URL。
        prompt: 可选的补充要求。
        model: 不填用默认 doubao-seed3d-2-0-260328, 也可用 1-0-250928。
        file_format: "glb" / "obj" / "usd" / "usdz"。
    """
    return three_d.generate_3d(image, prompt, model, file_format)


if __name__ == "__main__":
    asyncio.run(server.run_stdio_async())
