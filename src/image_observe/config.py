"""环境变量配置读取。

无论 server 从哪个目录被拉起 (跨项目复用是核心场景),
都优先加载本项目根目录的 .env, 再读环境变量。
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# src/image_observe/config.py -> 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

load_dotenv(PROJECT_ROOT / ".env")

ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
ARK_API_KEY = os.environ.get("ARK_API_KEY")
VISION_MODEL = os.environ.get("VISION_MODEL", "doubao-seed-2-0-pro-260215")
VISION_MODEL_FALLBACK = os.environ.get("VISION_MODEL_FALLBACK", "doubao-seed-evolving")
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "doubao-seedream-4-5-251128")
EDIT_MODEL = os.environ.get("EDIT_MODEL", "doubao-seededit-3-0-i2i")
VIDEO_MODEL = os.environ.get("VIDEO_MODEL", "doubao-seedance-2-0-260128")
MODEL3D = os.environ.get("MODEL3D", "doubao-seed3d-2-0-260328")


def require_api_key() -> str:
    if not ARK_API_KEY:
        raise RuntimeError("缺少 ARK_API_KEY, 请在 .env 或环境变量中配置")
    return ARK_API_KEY
