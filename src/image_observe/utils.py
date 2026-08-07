"""共用工具: Ark HTTP 调用、图片编码、文件下载、异步任务轮询。"""
import base64
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx

from .config import ARK_BASE_URL, require_api_key

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"

_DOWNLOAD_TIMEOUT = 60  # 单次下载超时 (秒)
_DOWNLOAD_MAX_BYTES = 100 * 1024 * 1024  # 下载大小上限 100MB

_ACTIVATION_CODES = ("ModelNotOpen", "InvalidEndpointOrModel.NotFound")
_ALLOWED_EXTS = (
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".zip", ".glb", ".obj", ".usdz",
)


def unique_filename(prefix: str, ext: str) -> str:
    """并发安全的产物文件名: uuid 前缀 + 毫秒时间戳, 避免同秒并发互相覆盖。"""
    return f"{prefix}_{uuid.uuid4().hex[:8]}_{int(time.time() * 1000)}{ext}"


def _raise_ark_error(path: str, resp: httpx.Response) -> None:
    code = None
    try:
        code = resp.json().get("error", {}).get("code")
    except Exception:
        pass
    if code in _ACTIVATION_CODES:
        raise RuntimeError(
            f"模型未开通: 请在火山方舟控制台 (console.volcengine.com/ark -> 开通管理) "
            f"开通该模型服务后再试。原始错误: {code}"
        ) from None
    raise RuntimeError(f"Ark API 调用失败 {path}: {resp.status_code} {resp.text[:300]}") from None


def ark_post(path: str, payload: dict, timeout: int = 180) -> dict:
    """POST Ark API (OpenAI 兼容), 返回 JSON。"""
    resp = httpx.post(
        ARK_BASE_URL + path,
        headers={"Authorization": f"Bearer {require_api_key()}"},
        json=payload,
        timeout=timeout,
    )
    if resp.status_code >= 400:
        _raise_ark_error(path, resp)
    return resp.json()


def ark_get(path: str, timeout: int = 30) -> dict:
    """GET Ark API, 返回 JSON。"""
    resp = httpx.get(
        ARK_BASE_URL + path,
        headers={"Authorization": f"Bearer {require_api_key()}"},
        timeout=timeout,
    )
    if resp.status_code >= 400:
        _raise_ark_error(path, resp)
    return resp.json()


def image_to_data_url(image: str) -> str:
    """本地路径或 http(s) URL -> OpenAI 兼容的 image_url。"""
    if urlparse(image).scheme in ("http", "https"):
        return image
    with open(image, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    mime = mimetypes.guess_type(image)[0] or "image/png"
    return f"data:{mime};base64,{data}"


def download(url: str, subdir: str | None = None) -> Path:
    """下载文件到 output/ (或子目录), 按 URL 实际扩展名保存。

    httpx 流式下载: 带超时与大小上限, 失败时清理半成品并抛中文错误, 避免永久挂起。
    """
    target = OUTPUT_DIR / subdir if subdir else OUTPUT_DIR
    target.mkdir(parents=True, exist_ok=True)
    ext = Path(urlparse(url).path).suffix or ".bin"
    if ext.lower() not in _ALLOWED_EXTS:
        ext = ".bin"
    path = target / unique_filename(subdir or "img", ext)
    try:
        with httpx.stream("GET", url, timeout=_DOWNLOAD_TIMEOUT) as resp:
            resp.raise_for_status()
            total = 0
            with open(path, "wb") as f:
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > _DOWNLOAD_MAX_BYTES:
                        raise RuntimeError(
                            f"下载超过大小上限 ({_DOWNLOAD_MAX_BYTES // (1024 * 1024)}MB): {url}"
                        )
                    f.write(chunk)
    except Exception as e:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"下载失败 {url}: {e}") from e
    return path


def wait_task(fetch: Callable, task_id: str, max_wait: int = 900, interval: int = 10) -> dict:
    """轮询异步任务直到终态 (succeeded 或失败), 返回任务数据。

    瞬时错误 (网络抖动/偶发 5xx) 带退避重试, 仅 failed/expired/cancelled 终态终止。
    """
    waited = 0
    attempts = 0
    while waited < max_wait:
        try:
            data = fetch(task_id)
        except Exception as e:
            attempts += 1
            if attempts > 3:
                raise RuntimeError(f"查询任务 {task_id} 状态连续失败: {e}") from e
            time.sleep(interval * attempts)
            waited += interval * attempts
            continue
        attempts = 0
        status = data.get("status")
        if status == "succeeded":
            return data
        if status in ("failed", "expired", "cancelled"):
            raise RuntimeError(f"任务 {task_id} 失败: status={status} error={data.get('error')}")
        time.sleep(interval)
        waited += interval
    raise RuntimeError(f"任务 {task_id} 超时 (>{max_wait}s)")
