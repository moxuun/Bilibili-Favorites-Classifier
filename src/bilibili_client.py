import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

import httpx
from models import BiliCredential, FavoriteFolder, VideoInfo

# B站API常量
FAVORITE_FOLDERS_URL = "https://api.bilibili.com/x/v3/fav/folder/created/list-all"
VIDEOS_IN_FOLDER_URL = "https://api.bilibili.com/x/v3/fav/resource/list"
CREATE_FOLDER_URL = "https://api.bilibili.com/x/v3/fav/folder/add"
DEAL_WITH_RESOURCE_URL = "https://api.bilibili.com/x/v3/fav/resource/deal"
FAVORITE_RESOURCE_IDS_URL = "https://api.bilibili.com/x/v3/fav/resource/ids"
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


@dataclass(frozen=True)
class MoveResult:
    """一次移动请求的结果摘要。"""

    attempts: int


class BilibiliClientError(RuntimeError):
    """Bilibili 请求失败。"""

    def __init__(
        self,
        message: str,
        *,
        http_status: Optional[int] = None,
        api_code: Optional[int] = None,
        attempts: int = 1,
    ):
        super().__init__(message)
        self.http_status = http_status
        self.api_code = api_code
        self.attempts = attempts


class BilibiliRateLimitError(BilibiliClientError):
    """请求被限流或拦截，继续写入可能扩大问题。"""


class BilibiliGlobalError(BilibiliClientError):
    """鉴权、CSRF 或账号级别错误。"""


class BilibiliTransientError(BilibiliClientError):
    """可以有限重试的网络或服务端临时错误。"""


@dataclass(frozen=True)
class _RequestResult:
    data: Dict[str, Any]
    attempts: int


class BilibiliClient:
    """
    用于与Bilibili API交互的异步客户端。
    """

    def __init__(self, credential: BiliCredential, request_delay: float = 1.0, max_retries: int = 3):
        """
        初始化Bilibili客户端。

        Args:
            credential (BiliCredential): 包含用户认证信息的凭证对象。
        """
        cookies = {
            "SESSDATA": credential.sessdata,
            "bili_jct": credential.bili_jct,
            "DedeUserID": str(credential.dedeuserid),
        }
        headers = {"User-Agent": DEFAULT_USER_AGENT}
        
        self._client = httpx.AsyncClient(cookies=cookies, headers=headers, timeout=30.0)
        self._user_id = credential.dedeuserid
        self._csrf = credential.bili_jct
        self._request_delay = max(float(request_delay), 0.1)
        self._max_retries = max(int(max_retries), 0)
        self._last_request_started = 0.0

    async def _pace_request(self) -> None:
        """确保请求开始时间之间有最小间隔。"""
        elapsed = time.monotonic() - self._last_request_started
        wait_time = self._request_delay - elapsed
        if wait_time > 0:
            await asyncio.sleep(wait_time)
        self._last_request_started = time.monotonic()

    @staticmethod
    def _backoff_delay(attempt: int, rate_limited: bool) -> float:
        if rate_limited:
            return min(30.0 * (2 ** attempt), 300.0)
        return min(1.0 * (2 ** attempt), 30.0)

    async def _request_json_once(self, method: str, url: str, **kwargs: Any) -> Dict[str, Any]:
        await self._pace_request()
        try:
            response = await self._client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise BilibiliTransientError(f"Bilibili 请求超时: {method} {url}") from exc
        except httpx.RequestError as exc:
            raise BilibiliTransientError(f"Bilibili 网络请求失败: {method} {url}") from exc

        status_code = response.status_code
        if status_code in (412, 429):
            raise BilibiliRateLimitError(
                f"Bilibili 请求被限流或拦截 (HTTP {status_code})",
                http_status=status_code,
            )
        if status_code in (401, 403):
            raise BilibiliGlobalError(
                f"Bilibili 鉴权失败 (HTTP {status_code})",
                http_status=status_code,
            )
        if status_code >= 500:
            raise BilibiliTransientError(
                f"Bilibili 服务端临时错误 (HTTP {status_code})",
                http_status=status_code,
            )

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise BilibiliClientError(
                f"Bilibili HTTP 请求失败 (HTTP {status_code})",
                http_status=status_code,
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise BilibiliTransientError("Bilibili 返回了无法解析的 JSON") from exc

        if not isinstance(payload, dict):
            raise BilibiliClientError("Bilibili 返回格式不是 JSON 对象")

        raw_api_code = payload.get("code", 0)
        try:
            api_code = int(raw_api_code)
        except (TypeError, ValueError):
            api_code = raw_api_code
        if api_code != 0:
            message = str(payload.get("message") or payload.get("msg") or "未知错误")
            lowered_message = message.lower()
            if api_code in (-412, -799) or any(
                marker in message or marker in lowered_message
                for marker in ("频繁", "过于频繁", "被拦截", "rate limit", "too many")
            ):
                raise BilibiliRateLimitError(
                    f"Bilibili 请求被限流或拦截 (code {api_code}): {message}",
                    http_status=status_code,
                    api_code=api_code,
                )
            if api_code in (-101, -111):
                raise BilibiliGlobalError(
                    f"Bilibili 鉴权或 CSRF 错误 (code {api_code}): {message}",
                    http_status=status_code,
                    api_code=api_code,
                )
            raise BilibiliClientError(
                f"Bilibili API 错误 (code {api_code}): {message}",
                http_status=status_code,
                api_code=api_code,
            )

        return payload

    async def _request_json(self, method: str, url: str, **kwargs: Any) -> _RequestResult:
        for attempt in range(self._max_retries + 1):
            try:
                data = await self._request_json_once(method, url, **kwargs)
                return _RequestResult(data=data, attempts=attempt + 1)
            except BilibiliGlobalError:
                raise
            except (BilibiliRateLimitError, BilibiliTransientError) as exc:
                exc.attempts = attempt + 1
                if attempt >= self._max_retries:
                    raise
                await asyncio.sleep(
                    self._backoff_delay(
                        attempt,
                        rate_limited=isinstance(exc, BilibiliRateLimitError),
                    )
                )

        raise AssertionError("unreachable")

    async def get_favorite_folders(self) -> List[FavoriteFolder]:
        """
        异步获取指定用户的所有收藏夹列表。

        Returns:
            List[FavoriteFolder]: 一个包含收藏夹信息的FavoriteFolder对象列表。
        
        Raises:
            httpx.HTTPStatusError: 如果API请求返回一个错误的HTTP状态码。
            KeyError: 如果响应的JSON结构不符合预期。
        """
        result = await self._request_json(
            "GET",
            FAVORITE_FOLDERS_URL,
            params={"up_mid": self._user_id},
        )
        folders_data = (result.data.get("data") or {}).get("list", [])
        return [FavoriteFolder(**folder) for folder in folders_data]

    async def get_videos_in_folder(self, media_id: int) -> List[VideoInfo]:
        """
        异步获取指定收藏夹中的所有视频信息，自动处理分页。

        Args:
            media_id (int): 收藏夹的ID。

        Returns:
            List[VideoInfo]: 一个包含该收藏夹所有视频信息的VideoInfo对象列表。
        
        Raises:
            httpx.HTTPStatusError: 如果API请求返回一个错误的HTTP状态码。
        """
        all_videos: List[VideoInfo] = []
        page_number = 1
        page_size = 20

        while True:
            params = {
                "media_id": media_id,
                "pn": page_number,
                "ps": page_size,
            }
            result = await self._request_json("GET", VIDEOS_IN_FOLDER_URL, params=params)
            data = result.data.get("data") or {}

            if not data or not data.get("medias"):
                break  # 如果没有数据或视频列表为空，则停止

            medias = data["medias"]
            for video_data in medias:
                # 将API返回的字典转换为VideoInfo模型对象
                video_info = VideoInfo(
                    aid=video_data.get("id"),
                    bvid=video_data.get("bvid"),
                    title=video_data.get("title", "无标题"),
                    description=video_data.get("intro", ""),
                    owner_name=(video_data.get("upper") or {}).get("name", "未知UP主"),
                )
                all_videos.append(video_info)

            if not data.get("has_more", False):
                break  # 如果API表明没有更多页了，则停止

            page_number += 1
        return all_videos

    async def get_video_ids_in_folder(self, media_id: int) -> Set[int]:
        """读取收藏夹中的实际视频 AID 集合，用于移动后的回读核实。"""
        result = await self._request_json(
            "GET",
            FAVORITE_RESOURCE_IDS_URL,
            params={"media_id": media_id},
        )
        return self._extract_video_ids(result.data.get("data"))

    @staticmethod
    def _extract_video_ids(raw_data: Any) -> Set[int]:
        """兼容接口返回字符串、数组和对象三种常见形态。"""
        if raw_data is None:
            return set()

        if isinstance(raw_data, dict):
            for key in ("ids", "aids", "list", "resources"):
                if key in raw_data:
                    return BilibiliClient._extract_video_ids(raw_data[key])
            return set()

        if isinstance(raw_data, str):
            raw_data = re.split(r"[,\s]+", raw_data.strip()) if raw_data.strip() else []

        if isinstance(raw_data, (list, tuple, set)):
            values = raw_data
        else:
            values = [raw_data]

        result: Set[int] = set()
        for value in values:
            if isinstance(value, dict):
                value = value.get("id", value.get("aid"))
            try:
                if value is not None and str(value).strip():
                    result.add(int(value))
            except (TypeError, ValueError):
                continue
        return result

    async def create_favorite_folder(self, title: str) -> Optional[int]:
        """
        创建一个新的收藏夹。

        Args:
            title (str): 新收藏夹的标题。

        Returns:
            Optional[int]: 如果创建成功，返回新收藏夹的ID，否则返回None。
        """
        result = await self._request_json(
            "POST",
            CREATE_FOLDER_URL,
            data={"title": title, "csrf": self._csrf},
        )
        folder_data = result.data.get("data") or {}
        return folder_data.get("id")

    async def move_video(self, video_aid: int, source_folder_id: int, target_folder_id: int) -> MoveResult:
        """
        将视频从一个收藏夹移动到另一个。

        Args:
            video_aid (int): 视频的aid。
            source_folder_id (int): 源收藏夹的ID。
            target_folder_id (int): 目标收藏夹的ID。

        Returns:
            MoveResult: 请求成功及实际尝试次数。
        """
        payload = {
            "rid": video_aid,
            "type": 2,
            "add_media_ids": target_folder_id,
            "del_media_ids": source_folder_id,
            "csrf": self._csrf,
        }
        result = await self._request_json("POST", DEAL_WITH_RESOURCE_URL, data=payload)
        return MoveResult(attempts=result.attempts)

    async def close(self):
        """
        优雅地关闭httpx客户端会话。
        """
        await self._client.aclose()
