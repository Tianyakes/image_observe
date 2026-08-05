"""3D 模型生成: 调用 Seed3D (Ark 异步任务 API, 图生 3D)。

流程: 创建任务 -> 轮询 -> 取文件直链 (zip) 并下载到 output/3d/。
生成通常需要数分钟。
"""
from . import config
from .utils import ark_get, ark_post, download, image_to_data_url, wait_task

TASK_PATH = "/contents/generations/tasks"


def generate_3d(
    image: str,
    prompt: str | None = None,
    model: str | None = None,
    file_format: str = "glb",
    max_wait: int = 900,
) -> str:
    """使用 Seed3D 从图片生成 3D 模型, 返回 URL 并保存到本地 output/3d/ 目录。

    Args:
        image: 参考图本地绝对路径或 http(s) URL (图生 3D)。
        prompt: 可选的补充要求。
        model: 默认 doubao-seed3d-2-0-260328, 也可用 1-0-250928。
        file_format: "glb" / "obj" / "usd" / "usdz"。
        max_wait: 轮询最长等待秒数。
    """
    text = f"--subdivisionlevel medium --fileformat {file_format}"
    if prompt:
        text += f" {prompt}"
    payload = {
        "model": model or config.MODEL3D,
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": image_to_data_url(image)}},
        ],
    }
    created = ark_post(TASK_PATH, payload, timeout=300)
    task_id = created["id"]
    print(f"[3d] 任务已创建: {task_id}")

    data = wait_task(
        lambda tid: ark_get(f"{TASK_PATH}/{tid}"),
        task_id,
        max_wait=max_wait,
        interval=15,
    )
    url = data["content"]["file_url"]
    local_path = download(url, subdir="3d")
    return f"3D 模型生成成功\nURL: {url}\n已保存到: {local_path}"
