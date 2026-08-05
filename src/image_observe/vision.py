"""图片理解: 调用豆包视觉模型 (Ark /chat/completions, OpenAI 兼容)。"""
from openai import OpenAI

from .config import ARK_BASE_URL, VISION_MODEL, require_api_key
from .utils import image_to_data_url

_OCR_PROMPT = "请逐行提取图中所有文字, 保持原文顺序与内容, 不增不删, 不要添加任何解释或标题。"
_OCR_PROMPT_TRANSLATE = "\n若图内文字非 {lang}, 请先逐行提取原文, 再翻译为 {lang}。"


def describe_image(image: str, prompt: str | None = None) -> str:
    """理解图片内容并返回文字描述。

    image: 本地绝对路径或 http(s) URL。
    prompt: 自定义提问, 默认要求详细描述。
    """
    client = OpenAI(base_url=ARK_BASE_URL, api_key=require_api_key(), timeout=120)
    user_prompt = prompt or "请详细描述这张图片的内容，包括主体、场景、颜色、细节等。"
    try:
        response = client.chat.completions.create(
            model=VISION_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {"type": "image_url", "image_url": {"url": image_to_data_url(image)}},
                    ],
                }
            ],
        )
    except Exception as e:
        code = getattr(e, "code", None)
        if code in ("ModelNotOpen", "InvalidEndpointOrModel.NotFound"):
            raise RuntimeError(
                f"视觉模型 {VISION_MODEL} 未开通: 请在火山方舟控制台 "
                "(console.volcengine.com/ark -> 开通管理) 开通该模型, "
                f"或在 .env 中把 VISION_MODEL 换成已开通的模型。原始错误: {e}"
            ) from e
        raise RuntimeError(f"视觉模型调用失败: {e}") from e
    return response.choices[0].message.content or ""


def extract_text(image: str, language: str | None = None) -> str:
    """从图片中提取文字 (OCR), 逐行返回原文。

    image: 本地绝对路径或 http(s) URL。
    language: 可选目标语言, 提供时先提取原文再翻译为该语言。
    """
    prompt = _OCR_PROMPT
    if language:
        prompt += _OCR_PROMPT_TRANSLATE.format(lang=language)
    return describe_image(image, prompt)
