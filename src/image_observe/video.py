"""视频生成: 调用 Seedance / Wan (Ark 异步任务 API)。

流程: 创建任务 -> 轮询 -> 取视频直链并下载到 output/videos/。
生成通常需要 1~3 分钟。
"""
from . import config
from .utils import ark_get, ark_post, download, image_to_data_url, wait_task

TASK_PATH = "/contents/generations/tasks"


def generate_video(
    prompt: str,
    model: str | None = None,
    image: str | None = None,
    ratio: str = "16:9",
    resolution: str = "720p",
    duration: int | None = None,
    generate_audio: bool = True,
    watermark: bool = False,
    max_wait: int = 600,
) -> str:
    """使用 Seedance 生成视频, 返回 URL 并保存到本地 output/videos/ 目录。

    Args:
        prompt: 视频内容描述。
        model: 默认 doubao-seedance-2-0-260128; 传图时可用 i2v 模型
               (如 doubao-seedance-1-0-lite-i2v-250428 / wan2-1-14b-i2v-250225)。
        image: 可选首帧图 (本地路径或 URL), 提供则为首帧图生视频。
        ratio: "16:9" / "4:3" / "1:1" / "3:4" / "9:16" / "21:9" / "adaptive"。
        resolution: "480p" / "720p" / "1080p" (fast 版不支持 1080p)。
        duration: 时长秒数, 2.0 支持 4~15; 不传由模型自动决定。
        generate_audio: 是否生成同步音频。
        watermark: 是否添加水印。
        max_wait: 轮询最长等待秒数。
    """
    content: list[dict] = [{"type": "text", "text": prompt}]
    if image:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": image_to_data_url(image)},
                "role": "first_frame",
            }
        )
    payload: dict = {
        "model": model or config.VIDEO_MODEL,
        "content": content,
        "ratio": ratio,
        "resolution": resolution,
        "generate_audio": generate_audio,
        "watermark": watermark,
    }
    if duration is not None:
        payload["duration"] = duration

    created = ark_post(TASK_PATH, payload, timeout=300)
    task_id = created["id"]
    print(f"[video] 任务已创建: {task_id}")

    data = wait_task(
        lambda tid: ark_get(f"{TASK_PATH}/{tid}"),
        task_id,
        max_wait=max_wait,
        interval=10,
    )
    url = data["content"]["video_url"]
    local_path = download(url, subdir="videos")
    return f"视频生成成功\nURL: {url}\n已保存到: {local_path}"
