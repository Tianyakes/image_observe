"""图像生成: 调用 Seedream (Ark /images/generations), 并下载保存到 output/。

生成的图片 URL 24 小时后失效, 所以自动下载到本地留存。
"""
from . import config
from .utils import ark_post, download


def generate_image(
    prompt: str,
    model: str | None = None,
    size: str = "2K",
    watermark: bool = True,
) -> str:
    """使用 Seedream 生成图片, 返回 URL 并保存到本地 output/ 目录。

    model: 支持 doubao-seedream-3-0-t2i-250415 / 4-0-250828 / 4-5-251128 /
           5-0-lite-260128 / 5-0-pro-260628, 默认 doubao-seedream-4-5-251128。
    size: 4.x/5.x 支持 "2K"/"4K"; 3.0 需传像素值如 "2048x2048"。
    """
    data = ark_post(
        "/images/generations",
        {
            "model": model or config.IMAGE_MODEL,
            "prompt": prompt,
            "size": size,
            "response_format": "url",
            "watermark": watermark,
            "sequential_image_generation": "disabled",
        },
        timeout=600,
    )
    url = data["data"][0]["url"]
    local_path = download(url)
    return f"生成成功\nURL: {url}\n已保存到: {local_path}"
