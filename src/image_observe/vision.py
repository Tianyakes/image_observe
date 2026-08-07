"""图片理解: 调用豆包视觉模型 (Ark /chat/completions, OpenAI 兼容)。

失败策略: 限流/服务端/网络错误带退避重试; 模型未开通/404/空内容自动切换备用模型
(VISION_MODEL_FALLBACK); 401/403/图片参数错误立即报错。成功输出标注实际使用的模型。
"""
import time
from pathlib import Path
from urllib.parse import urlparse

from openai import APITimeoutError, OpenAI

from .config import ARK_BASE_URL, VISION_MODEL, VISION_MODEL_FALLBACK, require_api_key
from .utils import image_to_data_url

_OCR_PROMPT = "请逐行提取图中所有文字, 保持原文顺序与内容, 不增不删, 不要添加任何解释或标题。"
_OCR_PROMPT_TRANSLATE = "\n若图内文字非 {lang}, 请先逐行提取原文, 再翻译为 {lang}。"

# 单次调用超时 / 退避间隔 / 总预算 (秒)
_CALL_TIMEOUT = 90
_RETRY_DELAYS = (1, 2, 4)
_TOTAL_BUDGET = 240

_MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 本地图片大小上限 20MB
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")

_FALLBACK_CODES = ("ModelNotOpen", "InvalidEndpointOrModel.NotFound")


class VisionInputError(ValueError):
    """本地输入错误 (文件不存在/超限/格式), 不重试不切换模型。"""


class VisionContentError(RuntimeError):
    """模型返回空内容, 视为失败 (可切换备用模型)。"""


def _classify_error(exc: Exception) -> str:
    """异常分类: "fallback" (换备用模型) / "retry" (带退避重试) / "stop" (立即报错)。"""
    if isinstance(exc, VisionInputError):
        return "stop"
    if isinstance(exc, VisionContentError):
        return "fallback"
    name = type(exc).__name__
    msg = str(exc)
    if isinstance(exc, APITimeoutError) or name in (
        "APIConnectionError", "RateLimitError", "InternalServerError",
        "ConflictError", "ServiceUnavailableError",
    ):
        return "retry"
    if name in ("AuthenticationError", "PermissionDeniedError"):
        return "stop"
    if name == "NotFoundError":
        return "fallback"
    if name == "BadRequestError":
        # 模型未开通 / 模型名错误可换备用; 图片参数问题换模型无用
        if any(c in msg for c in _FALLBACK_CODES) or "not found" in msg.lower() or "404" in msg:
            return "fallback"
        return "stop"
    # 兜底: 按消息子串判断
    if any(c in msg for c in _FALLBACK_CODES) or "404" in msg:
        return "fallback"
    if "429" in msg or "timeout" in msg.lower() or "connection" in msg.lower():
        return "retry"
    return "stop"


def _short(exc: Exception) -> str:
    """精简错误消息 (不回显 key/请求头), 截断到 200 字。"""
    return str(exc)[:200] or type(exc).__name__


def _validate_image(image: str) -> None:
    """本地图片前置校验: 存在/大小/格式, 输入错误与 API 错误分开报。"""
    if urlparse(image).scheme in ("http", "https"):
        return
    p = Path(image)
    if not p.exists():
        raise VisionInputError(f"图片文件不存在: {image}")
    if p.stat().st_size > _MAX_IMAGE_BYTES:
        raise VisionInputError(f"图片文件超过大小上限 (20MB): {image}")
    if p.suffix.lower() not in _IMAGE_EXTS:
        raise VisionInputError(
            f"不支持的图片格式: {p.suffix or '无扩展名'} (支持 png/jpg/jpeg/webp/gif/bmp)"
        )


def _chat_once(messages: list, model: str, timeout: float = _CALL_TIMEOUT) -> str:
    """单次调用, 返回文本内容; 空响应/空内容视为失败 (可 fallback)。"""
    client = OpenAI(base_url=ARK_BASE_URL, api_key=require_api_key(), timeout=timeout)
    response = client.chat.completions.create(model=model, messages=messages)
    if not response.choices:
        raise VisionContentError(f"{model} 返回空响应")
    content = response.choices[0].message.content
    if not content or not content.strip():
        raise VisionContentError(f"{model} 返回空内容")
    return content


def _chat_with_policy(messages: list) -> tuple[str, str]:
    """带策略的调用: 主模型退避重试 -> 备用模型, 总预算 _TOTAL_BUDGET 秒。

    返回 (content, model_used); 全部失败时抛中文错误, 列出各模型原因, 不回显 key。
    """
    models = [m for m in (VISION_MODEL, VISION_MODEL_FALLBACK) if m and m.strip()]
    errors: list[str] = []
    deadline = time.monotonic() + _TOTAL_BUDGET
    for idx, model in enumerate(models):
        # 主模型 1 次 + 3 次退避重试; 备用模型 1 次 + 1 次重试
        max_attempts = len(_RETRY_DELAYS) + 1 if idx == 0 else 2
        for attempt in range(max_attempts):
            remaining = deadline - time.monotonic()
            if remaining < 10:
                errors.append(f"{model}: 超过总预算 {_TOTAL_BUDGET}s")
                break
            try:
                content = _chat_once(messages, model, timeout=min(_CALL_TIMEOUT, remaining))
                return content, model
            except Exception as e:
                kind = _classify_error(e)
                errors.append(f"{model}: {_short(e)}")
                if kind == "stop":
                    if idx == 0:
                        # API key 无效 / 图片参数错误: 换模型无用, 立即报错
                        raise RuntimeError(f"视觉模型调用失败: {_short(e)}") from e
                    break
                if kind == "fallback":
                    break  # 切下一个模型
                if attempt < max_attempts - 1:
                    time.sleep(_RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)])
    raise RuntimeError("视觉模型调用失败: " + "; ".join(errors))


def describe_image(image: str, prompt: str | None = None) -> str:
    """理解图片内容并返回文字描述, 失败自动重试/切换备用模型。

    image: 本地绝对路径或 http(s) URL。
    prompt: 自定义提问, 默认要求详细描述。
    """
    _validate_image(image)
    user_prompt = prompt or "请详细描述这张图片的内容，包括主体、场景、颜色、细节等。"
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_prompt},
                {"type": "image_url", "image_url": {"url": image_to_data_url(image)}},
            ],
        }
    ]
    content, model = _chat_with_policy(messages)
    return f"{content}\n\n(视觉模型: {model})"


def extract_text(image: str, language: str | None = None) -> str:
    """从图片中提取文字 (OCR), 逐行返回原文。

    image: 本地绝对路径或 http(s) URL。
    language: 可选目标语言, 提供时先提取原文再翻译为该语言。
    """
    prompt = _OCR_PROMPT
    if language:
        if not language.strip():
            raise ValueError("参数错误: language 不能为空白")
        prompt += _OCR_PROMPT_TRANSLATE.replace("{lang}", language)
    return describe_image(image, prompt)
