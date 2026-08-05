"""图像编辑: 调用 SeedEdit 3.0 (经 /images/generations, 模型别名 doubao-seededit-3-0-i2i)。

用自然语言指令修改图片: 消除/替换/风格转换/光影调整等。
注意: SeedEdit 在 Ark 上没有独立 /images/edits 端点, 需走 generations 接口
并传 image 入参, size 用 "adaptive" 保持源图宽高比。
"""
from . import config
from .utils import ark_post, download, image_to_data_url


def edit_image(
    image: str,
    prompt: str,
    model: str | None = None,
    size: str = "adaptive",
    watermark: bool = True,
    scale: int | None = None,
) -> str:
    """使用 SeedEdit 编辑图片, 返回 URL 并保存到本地 output/ 目录。

    Args:
        image: 源图本地绝对路径或 http(s) URL。
        prompt: 编辑指令, 如 "把背景换成星空, 保持主体不变"。
        model: 默认 doubao-seededit-3-0-i2i。
        size: 默认 "adaptive" (保持源图比例); 也可传 "1K"/"2K"/"4K" 或像素值。
        watermark: 是否添加 "AI生成" 水印。
        scale: 指令遵循强度 (SeedEdit 用 guidance_scale, 范围 1~10)。
    """
    payload = {
        "model": model or config.EDIT_MODEL,
        "image": image_to_data_url(image),
        "prompt": prompt,
        "size": size,
        "response_format": "url",
        "watermark": watermark,
    }
    if scale is not None:
        payload["guidance_scale"] = scale
    data = ark_post("/images/generations", payload, timeout=300)
    url = data["data"][0]["url"]
    local_path = download(url)
    return f"编辑成功\nURL: {url}\n已保存到: {local_path}"
