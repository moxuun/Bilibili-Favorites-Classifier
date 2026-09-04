"""持久化记录一次整理任务的进度。"""

from __future__ import annotations

import csv
import os
import tempfile
from pathlib import Path
from typing import Dict, Iterable, Optional, Union

from models import VideoInfo


CSV_FIELDS = [
    "aid",
    "bvid",
    "title",
    "source_folder_id",
    "source_folder_name",
    "target_folder_id",
    "target_folder_name",
    "classify_status",
    "move_status",
    "attempts",
    "last_error",
    "verify_status",
]


class ProgressLogError(RuntimeError):
    """进度文件无法安全读取或写入。"""


class ProgressLog:
    """以视频 AID 为键保存可恢复的 CSV 进度。"""

    def __init__(self, path: Union[Path, str]):
        self.path = Path(path)
        self.rows: Dict[str, Dict[str, str]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return

        try:
            with self.path.open("r", newline="", encoding="utf-8-sig") as file:
                reader = csv.DictReader(file)
                if not reader.fieldnames or "aid" not in reader.fieldnames:
                    raise ProgressLogError(f"进度文件缺少 aid 列: {self.path}")

                for raw_row in reader:
                    aid = str(raw_row.get("aid", "")).strip()
                    if not aid:
                        continue
                    row = {field: str(raw_row.get(field, "") or "") for field in CSV_FIELDS}
                    self.rows[aid] = row
        except ProgressLogError:
            raise
        except (OSError, csv.Error, UnicodeError) as exc:
            raise ProgressLogError(f"无法读取进度文件 {self.path}: {exc}") from exc

    def ensure_videos(self, videos: Iterable[VideoInfo], source_folder_id: int, source_folder_name: str) -> None:
        """为本次读取到的视频建立初始记录，同时保留已有进度。"""
        for video in videos:
            aid = str(video.aid)
            if aid in self.rows:
                row = self.rows[aid]
                old_source_id = row.get("source_folder_id", "")
                if old_source_id and old_source_id != str(source_folder_id):
                    raise ProgressLogError(
                        f"进度文件中的视频 {aid} 属于其他来源收藏夹，拒绝混用: {self.path}"
                    )
                continue

            self.rows[aid] = {
                "aid": aid,
                "bvid": video.bvid,
                "title": video.title,
                "source_folder_id": str(source_folder_id),
                "source_folder_name": source_folder_name,
                "target_folder_id": "",
                "target_folder_name": "",
                "classify_status": "pending",
                "move_status": "pending",
                "attempts": "0",
                "last_error": "",
                "verify_status": "pending",
            }

    def get(self, video_aid: Union[int, str]) -> Optional[Dict[str, str]]:
        return self.rows.get(str(video_aid))

    def update(self, video_aid: int | str, **values: object) -> None:
        aid = str(video_aid)
        row = self.rows.setdefault(aid, {field: "" for field in CSV_FIELDS})
        row["aid"] = aid
        for field, value in values.items():
            if field not in CSV_FIELDS:
                raise ValueError(f"未知的进度字段: {field}")
            row[field] = "" if value is None else str(value)

    def save(self) -> None:
        """先写临时文件，再替换正式文件，避免中断时留下半个 CSV。"""
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Optional[str] = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                newline="",
                encoding="utf-8-sig",
                delete=False,
                dir=parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
            ) as file:
                temporary_path = file.name
                writer = csv.DictWriter(file, fieldnames=CSV_FIELDS, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(self.rows.values())
                file.flush()

            os.replace(temporary_path, self.path)
            temporary_path = None
        except (OSError, csv.Error) as exc:
            raise ProgressLogError(f"无法写入进度文件 {self.path}: {exc}") from exc
        finally:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass
