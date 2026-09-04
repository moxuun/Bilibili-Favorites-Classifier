import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_classifier import AIClassifier, AIClassifierError
from bilibili_client import (
    BilibiliClient,
    BilibiliTransientError,
)
import cli as cli_module
from cli import (
    AdaptiveTimeRemainingColumn,
    cleanup_candidates,
    find_resume_candidates,
    load_runtime_settings,
    membership_status,
    normalize_category,
    rows_needing_reconciliation,
    target_ids_for_rows,
    verify_batch,
)
from models import BiliCredential, FavoriteFolder, VideoInfo
from progress_tracker import ProgressLog


class ProgressLogTests(unittest.TestCase):
    def test_find_resume_candidate_by_folder_ids(self):
        folders = [
            FavoriteFolder(id=10, title="来源", media_count=0),
            FavoriteFolder(id=20, title="目标A", media_count=0),
            FavoriteFolder(id=30, title="目标B", media_count=0),
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "bilibili_favorites_progress_10_20-30.csv"
            path.touch()
            candidates = find_resume_candidates(folders, Path(directory))

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0][0].name, path.name)
        self.assertEqual(candidates[0][1].id, 10)
        self.assertEqual([folder.id for folder in candidates[0][2]], [20, 30])

    def test_progress_round_trip(self):
        video = VideoInfo(aid=123, bvid="BV123", title="测试视频", description="简介", owner_name="UP")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "progress.csv"
            progress = ProgressLog(path)
            progress.ensure_videos([video], 456, "鬼畜")
            progress.update(
                video.aid,
                target_folder_id=789,
                target_folder_name="AI",
                classify_status="classified",
                move_status="succeeded",
                verify_status="confirmed",
            )
            progress.save()

            restored = ProgressLog(path)
            row = restored.get(video.aid)
            self.assertIsNotNone(row)
            self.assertEqual(row["target_folder_name"], "AI")
            self.assertEqual(row["verify_status"], "confirmed")

    def test_cleanup_candidates_are_scoped(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").touch()
            (root / "ai_config.json").touch()
            (root / "bilibili_favorites_progress_1_2.csv").touch()
            (root / "ai_config.json.example").touch()
            (root / "notes.txt").touch()

            self.assertEqual(
                {path.name for path in cleanup_candidates(root)},
                {".env", "ai_config.json", "bilibili_favorites_progress_1_2.csv"},
            )


class ClassificationTests(unittest.TestCase):
    def test_pipeline_runtime_defaults(self):
        setting_names = [
            "REQUEST_DELAY",
            "MAX_RETRIES",
            "AI_BATCH_SIZE",
            "VERIFY_BATCH_SIZE",
            "AI_CONCURRENCY",
        ]
        saved = {name: os.environ.get(name) for name in setting_names}
        try:
            for name in setting_names:
                os.environ.pop(name, None)
            self.assertEqual(load_runtime_settings(), (1.0, 3, 50, 50, 4))
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_parse_classifications_accepts_uncertain_item(self):
        result = AIClassifier._parse_classifications(
            '{"classifications": ["AI", null]}',
            expected_length=2,
        )
        self.assertEqual(result, ["AI", None])

    def test_parse_classifications_rejects_wrong_length(self):
        with self.assertRaises(AIClassifierError):
            AIClassifier._parse_classifications('["AI"]', expected_length=2)

    def test_normalize_category(self):
        self.assertEqual(normalize_category("```AI```"), "AI")
        self.assertEqual(normalize_category("网络安全：基础"), "网络安全/基础")

    def test_time_remaining_uses_first_completed_batch(self):
        class TaskWithoutRichSpeed:
            total = 100
            completed = 10
            remaining = 90
            elapsed = 20
            time_remaining = None
            finished = False

        rendered = AdaptiveTimeRemainingColumn().render(TaskWithoutRichSpeed())
        self.assertEqual(rendered.plain, "0:03:00")


class MembershipTests(unittest.TestCase):
    def test_target_ids_only_include_rows_in_this_verification_group(self):
        rows = [
            {"target_folder_id": "30"},
            {"target_folder_id": "20"},
            {"target_folder_id": "30"},
            {"target_folder_id": "10"},
        ]
        self.assertEqual(target_ids_for_rows(rows, source_folder_id=10), [20, 30])

    def test_reconciliation_includes_nonterminal_classified_rows(self):
        progress = ProgressLog(Path("reconciliation-test.csv"))
        progress.update(
            1,
            source_folder_id="10",
            classify_status="classified",
            target_folder_id="20",
            move_status="api_accepted",
        )
        progress.update(
            2,
            source_folder_id="10",
            classify_status="classified",
            target_folder_id="20",
            move_status="blocked",
        )
        progress.update(
            3,
            source_folder_id="10",
            classify_status="classified",
            target_folder_id="20",
            move_status="succeeded",
        )
        progress.update(
            4,
            source_folder_id="10",
            classify_status="uncertain",
            move_status="not_required",
        )

        self.assertEqual(
            [row["aid"] for row in rows_needing_reconciliation(progress, 10)],
            ["1", "2"],
        )

    def test_membership_states(self):
        memberships = {20: {1}, 30: {2}}
        self.assertEqual(membership_status(1, set(), 20, memberships), "confirmed")
        self.assertEqual(membership_status(1, {1}, 20, memberships), "conflict")
        self.assertEqual(membership_status(2, set(), 20, memberships), "wrong_target")
        self.assertEqual(membership_status(3, {3}, 20, memberships), "still_in_source")
        self.assertEqual(membership_status(4, set(), 20, memberships), "missing")


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_prompt_allows_json_example(self):
        class FakeCompletions:
            def __init__(self):
                self.kwargs = None

            async def create(self, **kwargs):
                self.kwargs = kwargs
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content='{"classifications": ["AI"]}')
                        )
                    ]
                )

        completions = FakeCompletions()
        classifier = AIClassifier(
            {"openai_api_key": "test", "openai_base_url": "https://example.invalid"},
            max_retries=0,
        )
        classifier.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        video = VideoInfo(aid=1, bvid="BV1", title="测试", description="简介", owner_name="UP")

        result = await classifier.batch_classify_videos([video], ["AI"])

        self.assertEqual(result, ["AI"])
        self.assertIn('{"classifications": ["分类A", "分类B", null]}', completions.kwargs["messages"][1]["content"])

    async def test_transient_request_is_retried(self):
        credential = BiliCredential(bili_jct="csrf", sessdata="session", dedeuserid="1")
        client = BilibiliClient(credential, request_delay=0.1, max_retries=2)
        client._request_json_once = AsyncMock(
            side_effect=[BilibiliTransientError("temporary"), {"code": 0, "data": {}}]
        )
        try:
            with patch("bilibili_client.asyncio.sleep", new=AsyncMock()):
                result = await client._request_json("GET", "https://example.invalid")
            self.assertEqual(result.attempts, 2)
            self.assertEqual(client._request_json_once.await_count, 2)
        finally:
            await client.close()

    async def test_verify_batch_requires_consistent_reads(self):
        class FakeClient:
            async def get_video_ids_in_folder(self, media_id):
                return {
                    10: {3},
                    20: {1},
                    30: {2},
                }.get(media_id, set())

        rows = [
            {
                "aid": "1",
                "classify_status": "classified",
                "target_folder_id": "20",
                "move_status": "api_accepted",
            },
            {
                "aid": "2",
                "classify_status": "classified",
                "target_folder_id": "20",
                "move_status": "api_accepted",
            },
            {
                "aid": "3",
                "classify_status": "classified",
                "target_folder_id": "20",
                "move_status": "failed",
            },
            {
                "aid": "4",
                "classify_status": "classified",
                "target_folder_id": "20",
                "move_status": "api_accepted",
            },
        ]
        result = await verify_batch(FakeClient(), 10, rows, [20, 30], verification_delay=0)
        self.assertEqual(
            result,
            {"1": "confirmed", "2": "wrong_target", "3": "still_in_source", "4": "missing"},
        )

    async def test_resume_reconciles_progress_when_source_is_empty(self):
        class SilentConsole:
            def print(self, *_args, **_kwargs):
                return None

        class SilentProgress:
            def __init__(self, *_args, **_kwargs):
                return None

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def add_task(self, *_args, **_kwargs):
                return 1

            def update(self, *_args, **_kwargs):
                return None

        source_folder = FavoriteFolder(id=10, title="来源", media_count=0)
        target_folder = FavoriteFolder(id=20, title="目标A", media_count=1)

        class FakeBilibiliClient:
            instances = []

            def __init__(self, *_args, **_kwargs):
                self.read_folder_ids = []
                self.move_aids = []
                self.__class__.instances.append(self)

            async def get_favorite_folders(self):
                return [source_folder, target_folder]

            async def get_videos_in_folder(self, _folder_id):
                return []

            async def get_video_ids_in_folder(self, folder_id):
                self.read_folder_ids.append(folder_id)
                return {99} if folder_id == 20 else set()

            async def move_video(self, video_aid, source_folder_id, target_folder_id):
                self.move_aids.append((video_aid, source_folder_id, target_folder_id))
                raise AssertionError("已在目标收藏夹的视频不应再次移动")

            async def close(self):
                return None

        class FakeAIClassifier:
            instances = []

            def __init__(self, *_args, **_kwargs):
                self.__class__.instances.append(self)

            async def close(self):
                return None

        with TemporaryDirectory() as directory:
            progress_path = Path(directory) / "progress.csv"
            progress = ProgressLog(progress_path)
            progress.update(
                99,
                bvid="BV99",
                title="已移动",
                source_folder_id="10",
                source_folder_name="来源",
                target_folder_id="20",
                target_folder_name="目标A",
                classify_status="classified",
                move_status="api_accepted",
                verify_status="pending",
            )
            progress.save()

            settings = {
                "REQUEST_DELAY": "0.1",
                "MAX_RETRIES": "0",
                "AI_BATCH_SIZE": "2",
                "VERIFY_BATCH_SIZE": "2",
                "AI_CONCURRENCY": "1",
            }
            with patch.dict(os.environ, settings, clear=False), patch.object(
                cli_module,
                "ensure_config_is_ready",
                new=AsyncMock(return_value=(object(), {})),
            ), patch.object(cli_module, "Console", SilentConsole), patch.object(
                cli_module, "BilibiliClient", FakeBilibiliClient
            ), patch.object(
                cli_module, "AIClassifier", FakeAIClassifier
            ), patch.object(cli_module, "Progress", SilentProgress), patch.object(
                cli_module,
                "find_resume_candidates",
                return_value=[(progress_path, source_folder, [target_folder])],
            ), patch.object(
                cli_module.IntPrompt,
                "ask",
                side_effect=AssertionError("检测到进度后不应再次询问来源收藏夹"),
            ), patch.object(
                cli_module.Prompt,
                "ask",
                side_effect=AssertionError("检测到进度后不应再次询问目标收藏夹"),
            ), patch.object(cli_module.asyncio, "sleep", new=AsyncMock()):
                await cli_module.classify_async()

            restored = ProgressLog(progress_path)

        self.assertEqual(restored.get(99)["move_status"], "succeeded")
        self.assertEqual(restored.get(99)["verify_status"], "confirmed")
        self.assertEqual(FakeBilibiliClient.instances[-1].move_aids, [])
        self.assertEqual(FakeBilibiliClient.instances[-1].read_folder_ids, [10, 20, 10, 20])


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_batches_feed_serial_bilibili_worker(self):
        class SilentConsole:
            def print(self, *_args, **_kwargs):
                return None

        class SilentProgress:
            def __init__(self, *_args, **_kwargs):
                return None

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def add_task(self, *_args, **_kwargs):
                return 1

            def update(self, *_args, **_kwargs):
                return None

        source_folder = FavoriteFolder(id=10, title="来源", media_count=4)
        target_folder = FavoriteFolder(id=20, title="目标A", media_count=0)
        unused_folder = FavoriteFolder(id=30, title="未使用目标", media_count=0)

        class FakeBilibiliClient:
            instances = []

            def __init__(self, *_args, **_kwargs):
                self.source_ids = {1, 2, 3, 4}
                self.target_ids = set()
                self.move_aids = []
                self.read_folder_ids = []
                self.__class__.instances.append(self)

            async def get_favorite_folders(self):
                return [source_folder, target_folder, unused_folder]

            async def get_videos_in_folder(self, _folder_id):
                return [
                    VideoInfo(
                        aid=aid,
                        bvid=f"BV{aid}",
                        title=f"视频{aid}",
                        description="简介",
                        owner_name="UP",
                    )
                    for aid in (1, 2, 3, 4)
                ]

            async def move_video(self, video_aid, source_folder_id, target_folder_id):
                self.assert_source_folder(source_folder_id)
                self.source_ids.remove(video_aid)
                self.target_ids.add(video_aid)
                self.move_aids.append(video_aid)
                return SimpleNamespace(attempts=1)

            def assert_source_folder(self, source_folder_id):
                if source_folder_id != 10:
                    raise AssertionError(f"unexpected source folder: {source_folder_id}")

            async def get_video_ids_in_folder(self, folder_id):
                self.read_folder_ids.append(folder_id)
                if folder_id == 10:
                    return set(self.source_ids)
                if folder_id == 20:
                    return set(self.target_ids)
                if folder_id == 30:
                    raise AssertionError("未使用的目标收藏夹不应被回读")
                return set()

            async def close(self):
                return None

        class FakeAIClassifier:
            instances = []

            def __init__(self, *_args, **_kwargs):
                self.batch_sizes = []
                self.__class__.instances.append(self)

            async def batch_classify_videos(self, videos, target_folders):
                self.batch_sizes.append((len(videos), tuple(target_folders)))
                return ["目标A"] * len(videos)

            async def close(self):
                return None

        with TemporaryDirectory() as directory:
            progress_path = Path(directory) / "progress.csv"
            settings = {
                "REQUEST_DELAY": "0.1",
                "MAX_RETRIES": "0",
                "AI_BATCH_SIZE": "2",
                "VERIFY_BATCH_SIZE": "4",
                "AI_CONCURRENCY": "2",
            }
            with patch.dict(os.environ, settings, clear=False), patch.object(
                cli_module,
                "ensure_config_is_ready",
                new=AsyncMock(return_value=(object(), {})),
            ), patch.object(cli_module, "Console", SilentConsole), patch.object(
                cli_module, "BilibiliClient", FakeBilibiliClient
            ), patch.object(
                cli_module, "AIClassifier", FakeAIClassifier
            ), patch.object(cli_module, "Progress", SilentProgress), patch.object(
                cli_module, "progress_path_for", return_value=progress_path
            ), patch.object(
                cli_module,
                "find_resume_candidates",
                return_value=[(progress_path, source_folder, [target_folder])],
            ), patch.object(
                cli_module.IntPrompt,
                "ask",
                side_effect=AssertionError("检测到进度后不应再次询问来源收藏夹"),
            ), patch.object(
                cli_module.Prompt,
                "ask",
                side_effect=AssertionError("检测到进度后不应再次询问目标收藏夹"),
            ):
                await cli_module.classify_async()

        bili_client = FakeBilibiliClient.instances[-1]
        ai_classifier = FakeAIClassifier.instances[-1]
        self.assertEqual([size for size, _ in ai_classifier.batch_sizes], [2, 2])
        self.assertEqual(len(bili_client.move_aids), 4)
        self.assertEqual(bili_client.read_folder_ids, [10, 20, 10, 20])


class ParsingTests(unittest.TestCase):
    def test_extract_video_ids_supports_api_shapes(self):
        self.assertEqual(BilibiliClient._extract_video_ids("1, 2,3"), {1, 2, 3})
        self.assertEqual(BilibiliClient._extract_video_ids({"ids": "4,5"}), {4, 5})
        self.assertEqual(BilibiliClient._extract_video_ids([{"id": 6}, {"aid": 7}]), {6, 7})


if __name__ == "__main__":
    unittest.main()
