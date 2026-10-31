"""Optional image-recognition channel for chat attachments.

Calls Zhipu's OpenAI-compatible chat/completions endpoint with a vision model
(the same HTTP surface the official @z_ai/mcp-server vision MCP wraps, minus
the Node subprocess): the image rides the chat request as a base64 data URL,
never touches disk, and the transcription is injected into the QA pipeline as
untrusted background context. Strictly opt-in per deployment: without
STUDY_VISION_API_KEY (or a STUDY_SEARCH_API_KEY to fall back to) nothing here
is reachable from the chat flow; failures surface as one SSE status frame and
the answer continues without the image.
"""

import logging
import os

import requests

LOG = logging.getLogger(__name__)

DEFAULT_VISION_BASE = "https://open.bigmodel.cn/api/paas/v4"
# The npm vision MCP defaults to glm-5.3-flash, which GLM Coding Plans sold
# before that model existed reject with 1113; glm-4.6v is plan-covered.
DEFAULT_VISION_MODEL = "glm-4.6v"
MAX_RESULT_CHARS = 8000


class VisionError(Exception):
    """Raised for any transport, protocol or configuration failure."""


# Faithful transcription first: the study use case is photographed statutes,
# exam questions and case digests, where the wording is the point. Description
# only when there is no text to transcribe.
_RECOGNIZE_PROMPT = (
    "请识别这张图片中的全部文字内容，逐字忠实转写，保留原有的段落、列表、层级与编号结构；"
    "转写之外不要添加任何解释、评论或总结。图片中的表格按行用「｜」分隔转写。"
    "模糊、被遮挡或被截断而无法确定的内容，用「（模糊无法辨认）」或「（被截断）」标注，不要猜测补全。"
    "仅当图片中完全没有文字（如照片、图画）时，才用一两句话客观描述图片内容，不要推测图中人物身份或拍摄背景。"
)


class VisionClient:
    def __init__(self):
        self.base = (os.getenv("STUDY_VISION_BASE_URL") or DEFAULT_VISION_BASE).strip().rstrip("/")
        # Deployers usually share one GLM Coding Plan key across channels;
        # the search key is the documented fallback.
        self.key = (os.getenv("STUDY_VISION_API_KEY") or os.getenv("STUDY_SEARCH_API_KEY") or "").strip()
        self.model = (os.getenv("STUDY_VISION_MODEL") or DEFAULT_VISION_MODEL).strip()
        self.configured = bool(self.key and self.base)
        self.session = requests.Session()

    def recognize(self, data_url: str) -> str:
        """Transcribe one image (as a data: URL) to text. Raises VisionError."""
        if not self.configured:
            raise VisionError("未配置图片识别服务。")
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": _RECOGNIZE_PROMPT},
                ],
            }],
        }
        try:
            with self.session.post(self.base + "/chat/completions", json=payload,
                                   headers={"Authorization": "Bearer " + self.key},
                                   timeout=(5, 120), allow_redirects=False) as response:
                if response.status_code != 200:
                    detail = response.text[:200].replace("\n", " ")
                    raise VisionError(f"图片识别服务返回错误（HTTP {response.status_code}）：{detail}")
                data = response.json()
        except requests.RequestException as exc:
            raise VisionError("图片识别服务请求失败或超时。") from exc
        except ValueError as exc:
            raise VisionError("图片识别服务返回了无法解析的内容。") from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, TypeError, IndexError) as exc:
            raise VisionError("图片识别服务返回了无效响应。") from exc
        if not isinstance(content, str) or not content.strip():
            # Reasoning models can exhaust the budget mid-thought and leave
            # the answer in the reasoning field instead of content.
            try:
                content = data["choices"][0]["message"].get("reasoning_content") or ""
            except (KeyError, TypeError, IndexError):
                content = ""
        if not isinstance(content, str) or not content.strip():
            raise VisionError("图片识别服务没有返回有效内容。")
        return content.strip()[:MAX_RESULT_CHARS]
