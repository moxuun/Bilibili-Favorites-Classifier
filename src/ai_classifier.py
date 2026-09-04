import asyncio
import json
import re
from typing import Any, List, Optional

import openai
from models import VideoInfo


class AIClassifierError(RuntimeError):
    """AI 请求或返回格式无法用于本批次分类。"""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        global_error: bool = False,
        attempts: int = 1,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.global_error = global_error
        self.attempts = attempts

class AIClassifier:
    """
    使用AI模型对Bilibili视频进行分类。
    """

    def __init__(self, ai_config: dict, max_retries: int = 3):
        """
        初始化AIClassifier。

        Args:
            ai_config (dict): 包含 AI 相关配置的字典。
        """
        self.config = ai_config
        self.client = openai.AsyncOpenAI(
            api_key=self.config.get("openai_api_key"),
            base_url=self.config.get("openai_base_url"),
        )
        self._max_retries = max(int(max_retries), 0)

    async def classify_video(self, video: VideoInfo, target_folders: List[str]) -> Optional[str]:
        """
        对单个视频进行分类。

        Args:
            video (VideoInfo): 包含视频标题和描述的视频信息对象。
            target_folders (List[str]): 预设的收藏夹列表。

        Returns:
            Optional[str]: AI模型返回的分类结果，如果发生API错误则返回None。
        """
        prompt = f"""
请根据以下Bilibili视频信息，从我提供的分类列表中选择一个最合适的。
视频标题：{video.title}
视频描述：{video.desc}

请从以下列表中选择一个最匹配的分类，并只返回分类的名称，不要添加任何解释或无关文字。
分类列表：{', '.join(target_folders)}
"""
        
        messages = [
            {"role": "system", "content": "你是一个Bilibili视频分类助手，你的任务是根据视频信息返回最精准的分类路径。"},
            {"role": "user", "content": prompt}
        ]

        try:
            response = await self.client.chat.completions.create(
                model=self.config.get("model_name", "gpt-3.5-turbo"),
                messages=messages,
            )
            if response.choices and response.choices[0].message.content:
                classification = response.choices[0].message.content
                return classification.strip()
            return None
        except openai.APIError as e:
            print(f"An OpenAI API error occurred: {e}")
            return None
        except Exception as e:
            print(f"An unexpected error occurred: {e}")
            return None

    @staticmethod
    def _error_from_exception(error: Exception) -> AIClassifierError:
        status_code = getattr(error, "status_code", None)
        retryable = status_code is None or status_code in (408, 409, 429) or status_code >= 500
        global_error = status_code in (400, 401, 403, 404, 429) or not retryable
        if status_code in (401, 403):
            message = "AI 鉴权失败"
        elif status_code == 429:
            message = "AI 服务限流或配额不足"
        elif status_code is not None and status_code >= 500:
            message = f"AI 服务端临时错误 (HTTP {status_code})"
        else:
            message = "AI 请求失败"
        return AIClassifierError(message, retryable=retryable, global_error=global_error)

    @staticmethod
    def _parse_classifications(content: str, expected_length: int) -> List[Optional[str]]:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", content, re.IGNORECASE)
        json_str = match.group(1) if match else content

        try:
            result_data: Any = json.loads(json_str)
        except json.JSONDecodeError as exc:
            raise AIClassifierError("AI 返回的内容不是有效 JSON") from exc

        classifications: Any = result_data
        if isinstance(result_data, dict):
            classifications = result_data.get("classifications")
            if classifications is None:
                classifications = next(
                    (value for value in result_data.values() if isinstance(value, list)),
                    None,
                )

        if not isinstance(classifications, list) or len(classifications) != expected_length:
            raise AIClassifierError("AI 返回的分类数量或格式不正确")

        normalized: List[Optional[str]] = []
        for classification in classifications:
            if classification is None:
                normalized.append(None)
            elif isinstance(classification, str) and classification.strip():
                normalized.append(classification.strip())
            else:
                normalized.append(None)
        return normalized

    async def _batch_classify_once(self, videos: List[VideoInfo], target_folders: List[str]) -> List[Optional[str]]:
        """
        对一批视频进行分类。

        Args:
            videos (List[VideoInfo]): 包含多个视频信息的列表。
            target_folders (List[str]): 预设的收藏夹列表。

        Returns:
            List[Optional[str]]: AI模型返回的分类结果列表，顺序与输入一致。发生错误时对应位置为None。
        """
        video_list_formatted = [
            {"index": i, "title": v.title, "desc": v.desc}
            for i, v in enumerate(videos)
        ]

        prompt = f"""
请根据以下Bilibili视频信息列表（JSON格式），从我提供的分类列表中为每个视频选择一个最合适的分类。

分类列表：{', '.join(target_folders)}

视频信息列表：
{json.dumps(video_list_formatted, ensure_ascii=False, indent=2)}

你的任务是：
1. 仔细阅读每个视频的标题和描述。
2. 从“分类列表”中为每个视频选择一个最匹配的分类。
3. 严格按照输入视频的顺序，返回一个 JSON 对象，其中的 classifications 数组只包含每个视频对应的分类名称。数组的长度必须与输入的视频列表完全一致。
4. 如果信息不足以可靠分类，对应位置返回 null；不要猜测一个不合适的分类。

例如，如果输入了3个视频，你的回答应该是这样的格式：
{{"classifications": ["分类A", "分类B", null]}}

请不要添加任何解释、序号或无关文字，只返回这个 JSON 对象。
"""
        messages = [
            {"role": "system", "content": "你是一个高效的Bilibili视频分类助手，专门处理批量分类请求并严格按照要求的JSON格式返回结果。"},
            {"role": "user", "content": prompt}
        ]

        try:
            response = await self.client.chat.completions.create(
                model=self.config.get("model_name", "gpt-3.5-turbo"),
                messages=messages,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            raise self._error_from_exception(exc) from exc

        if not response.choices or not response.choices[0].message.content:
            raise AIClassifierError("AI 返回为空")
        return self._parse_classifications(
            response.choices[0].message.content,
            expected_length=len(videos),
        )

    async def batch_classify_videos(self, videos: List[VideoInfo], target_folders: List[str]) -> List[Optional[str]]:
        """分类一批视频；临时错误有限重试，最终错误交给调用方处理。"""
        last_error: Optional[AIClassifierError] = None
        for attempt in range(self._max_retries + 1):
            try:
                return await self._batch_classify_once(videos, target_folders)
            except AIClassifierError as exc:
                exc.attempts = attempt + 1
                last_error = exc
                if not exc.retryable or attempt >= self._max_retries:
                    raise
                await asyncio.sleep(min(2.0 * (2 ** attempt), 30.0))

        raise last_error or AIClassifierError("AI 分类失败")

    async def close(self):
        """
        关闭OpenAI客户端会话。
        """
        await self.client.close()
