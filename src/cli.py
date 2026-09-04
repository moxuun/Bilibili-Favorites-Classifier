import asyncio
import os
import re
import sys
from collections import Counter
from math import ceil
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import click
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.text import Text
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from ai_classifier import AIClassifier, AIClassifierError
from bilibili_client import (
    BilibiliClient,
    BilibiliClientError,
    BilibiliGlobalError,
    BilibiliRateLimitError,
)
from config_manager import ConfigManager
from interactive_config import InteractiveConfig
from models import BiliCredential, FavoriteFolder, VideoInfo
from progress_tracker import ProgressLog, ProgressLogError


class AdaptiveTimeRemainingColumn(TimeRemainingColumn):
    """在 Rich 速度样本不足时，用已完成进度估算剩余时间。"""

    def render(self, task):
        if task.finished or task.total is None or task.time_remaining is not None:
            return super().render(task)

        elapsed = task.elapsed
        if not elapsed or task.completed <= 0:
            return super().render(task)

        estimated_remaining = ceil(task.remaining * elapsed / task.completed)
        minutes, seconds = divmod(int(estimated_remaining), 60)
        hours, minutes = divmod(minutes, 60)
        if self.compact and not hours:
            formatted = f"{minutes:02d}:{seconds:02d}"
        else:
            formatted = f"{hours:d}:{minutes:02d}:{seconds:02d}"
        return Text(formatted, style="progress.remaining")


@click.command()
@click.option(
    "--cleanup",
    is_flag=True,
    help="清理本地配置和整理进度文件（需要再次确认）。",
)
def cli(cleanup: bool):
    """
    Bilibili-Favorites-Classifier: 一个使用 AI 对 Bilibili 收藏夹进行分类的工具。
    主要功能是：对 Bilibili 收藏夹中的视频进行 AI 智能分类。
    """
    if cleanup:
        cleanup_local_data(Console())
        return

    try:
        asyncio.run(classify_async())
    except KeyboardInterrupt:
        console = Console()
        console.print("\n[yellow]操作被用户中断。[/yellow]")
        sys.exit(0)
    except Exception as e:
        console = Console()
        console.print(f"[bold red]执行过程中发生未处理的错误: {e}[/bold red]")
        sys.exit(1)


def cleanup_candidates(root: Path = Path(".")) -> List[Path]:
    """只返回项目根目录中明确属于本工具的本地数据文件。"""
    candidates = [root / ".env", root / "ai_config.json"]
    candidates.extend(sorted(root.glob("bilibili_favorites_progress_*.csv")))
    return [path for path in candidates if path.is_file()]


def cleanup_local_data(console: Console) -> None:
    files = cleanup_candidates()
    if not files:
        console.print("[green]没有发现需要清理的本地配置或进度文件。[/green]")
        return

    console.print("[bold yellow]以下文件将被永久删除：[/bold yellow]")
    for path in files:
        console.print(f"  - {path.name}")
    console.print("[yellow]此操作不会删除 .env.example、ai_config.json.example、代码或虚拟环境。[/yellow]")

    if not Confirm.ask("确认清理以上本地数据？", default=False):
        console.print("[dim]已取消清理。[/dim]")
        return

    for path in files:
        try:
            path.unlink()
        except OSError as exc:
            console.print(f"[red]删除失败 {path.name}: {exc}[/red]")
        else:
            console.print(f"[green]已删除 {path.name}[/green]")


async def ensure_config_is_ready(config_manager: Optional[ConfigManager] = None) -> Tuple[BiliCredential, Dict]:
    """确保所有配置都准备就绪，如果缺少配置则启动交互式向导。"""
    config_manager = config_manager or ConfigManager()
    console = Console()

    bili_config = config_manager.load_bili_credential()
    ai_config = config_manager.load_ai_config()

    if bili_config and ai_config:
        console.print("✅ 配置加载成功。", style="green")
        return bili_config, ai_config

    console.print("\n[bold yellow]⚠️  检测到配置不完整或缺失，启动交互式配置向导...[/bold yellow]")
    interactive_config = InteractiveConfig(config_manager)
    cookie, new_ai_config = await interactive_config.run_interactive_setup()

    if cookie and new_ai_config:
        config_manager.save_bili_credential_from_cookie(cookie)
        config_manager.save_ai_config(new_ai_config)
        console.print("\n[bold green]✅ 配置已成功保存。[/bold green]")
        bili_config = config_manager.load_bili_credential()
        ai_config = config_manager.load_ai_config()
        if bili_config and ai_config:
            return bili_config, ai_config

    console.print("\n[bold red]❌ 配置未完成，程序无法继续。[/bold red]")
    sys.exit(1)


def load_runtime_settings() -> Tuple[float, int, int, int, int]:
    """读取请求节流、重试和流水线设置，配置格式错误时明确停止。"""
    try:
        request_delay = float(os.getenv("REQUEST_DELAY", "1.0"))
        max_retries = int(os.getenv("MAX_RETRIES", "3"))
        ai_batch_size = int(os.getenv("AI_BATCH_SIZE", "50"))
        verify_batch_size = int(os.getenv("VERIFY_BATCH_SIZE", "50"))
        ai_concurrency = int(os.getenv("AI_CONCURRENCY", "4"))
    except ValueError as exc:
        raise ValueError(
            "REQUEST_DELAY、MAX_RETRIES、AI_BATCH_SIZE、VERIFY_BATCH_SIZE 和 AI_CONCURRENCY 必须是数字"
        ) from exc

    if request_delay < 0.1:
        request_delay = 0.1
    if max_retries < 0:
        max_retries = 0
    if ai_batch_size < 1:
        ai_batch_size = 1
    if verify_batch_size < 1:
        verify_batch_size = 1
    if ai_concurrency < 1:
        ai_concurrency = 1
    return request_delay, max_retries, ai_batch_size, verify_batch_size, ai_concurrency


def normalize_category(category: Optional[str]) -> str:
    if not category:
        return ""
    return category.strip().strip("`\"'").replace("：", "/").replace(":", "/")


def progress_path_for(source_folder: FavoriteFolder, target_folders: List[FavoriteFolder]) -> Path:
    target_key = "-".join(str(folder.id) for folder in sorted(target_folders, key=lambda item: item.id))
    return Path(f"bilibili_favorites_progress_{source_folder.id}_{target_key}.csv")


def find_resume_candidates(
    folders: List[FavoriteFolder],
    root: Path = Path("."),
) -> List[Tuple[Path, FavoriteFolder, List[FavoriteFolder]]]:
    """根据进度文件名匹配当前账号的来源和目标收藏夹。"""
    folder_by_id = {folder.id: folder for folder in folders}
    candidates: List[Tuple[Path, FavoriteFolder, List[FavoriteFolder]]] = []
    pattern = re.compile(r"bilibili_favorites_progress_(\d+)_(\d+(?:-\d+)*)\.csv$")

    for path in sorted(root.glob("bilibili_favorites_progress_*.csv")):
        match = pattern.fullmatch(path.name)
        if not match:
            continue

        source_folder = folder_by_id.get(int(match.group(1)))
        target_ids = [int(value) for value in match.group(2).split("-")]
        target_folders = [folder_by_id.get(folder_id) for folder_id in target_ids]
        if source_folder is None or any(folder is None for folder in target_folders):
            continue
        candidates.append((path, source_folder, [folder for folder in target_folders if folder is not None]))

    return candidates


def is_terminal_classification(row: Dict[str, str]) -> bool:
    return row.get("classify_status") in {"uncertain", "invalid"}


def is_terminal_move(row: Dict[str, str]) -> bool:
    return row.get("move_status") in {"succeeded", "skipped_same_folder", "conflict", "wrong_target"}


def membership_status(
    video_aid: int,
    source_ids: Set[int],
    expected_target_id: int,
    target_memberships: Dict[int, Set[int]],
) -> str:
    """根据两次回读的实际归属给出可操作的状态。"""
    in_source = video_aid in source_ids
    in_expected_target = video_aid in target_memberships.get(expected_target_id, set())

    if in_source and in_expected_target:
        return "conflict"
    if not in_source and in_expected_target:
        return "confirmed"

    in_other_target = any(
        folder_id != expected_target_id and video_aid in video_ids
        for folder_id, video_ids in target_memberships.items()
    )
    if in_other_target:
        return "wrong_target"
    if in_source:
        return "still_in_source"
    return "missing"


def target_ids_for_rows(rows: List[Dict[str, str]], source_folder_id: int) -> List[int]:
    """只返回本次核实实际涉及的目标收藏夹，减少无关回读。"""
    # ponytail: 只扫描本组目标；如需穷举所有错误归属，再扩展为全候选文件夹回读。
    return sorted(
        {
            int(row["target_folder_id"])
            for row in rows
            if row.get("target_folder_id") and int(row["target_folder_id"]) != source_folder_id
        }
    )


def rows_needing_reconciliation(progress: ProgressLog, source_folder_id: int) -> List[Dict[str, str]]:
    """返回已分类但尚未最终确认的记录，供重启时先核对实际归属。"""
    return [
        row
        for row in progress.rows.values()
        if row.get("source_folder_id") == str(source_folder_id)
        and row.get("classify_status") == "classified"
        and row.get("target_folder_id")
        and int(row["target_folder_id"]) != source_folder_id
        and not is_terminal_move(row)
    ]


async def verify_batch(
    bili_client: BilibiliClient,
    source_folder_id: int,
    rows: List[Dict[str, str]],
    target_folder_ids: List[int],
    verification_delay: float,
) -> Dict[str, str]:
    """批量读取来源和目标收藏夹，并要求两次结果一致。"""
    rows_to_verify = [
        row
        for row in rows
        if row.get("classify_status") == "classified"
        and row.get("target_folder_id")
        and int(row["target_folder_id"]) != source_folder_id
        and not is_terminal_move(row)
    ]
    if not rows_to_verify:
        return {}

    folder_ids = [source_folder_id]
    folder_ids.extend(
        folder_id
        for folder_id in target_folder_ids
        if folder_id != source_folder_id and folder_id not in folder_ids
    )

    async def read_snapshot() -> Dict[int, Set[int]]:
        return {
            folder_id: await bili_client.get_video_ids_in_folder(folder_id)
            for folder_id in folder_ids
        }

    first_snapshot = await read_snapshot()
    await asyncio.sleep(verification_delay)
    second_snapshot = await read_snapshot()

    statuses: Dict[str, str] = {}
    for row in rows_to_verify:
        aid = int(row["aid"])
        expected_target_id = int(row["target_folder_id"])
        first_status = membership_status(
            aid,
            first_snapshot[source_folder_id],
            expected_target_id,
            {
                folder_id: video_ids
                for folder_id, video_ids in first_snapshot.items()
                if folder_id != source_folder_id
            },
        )
        second_status = membership_status(
            aid,
            second_snapshot[source_folder_id],
            expected_target_id,
            {
                folder_id: video_ids
                for folder_id, video_ids in second_snapshot.items()
                if folder_id != source_folder_id
            },
        )
        statuses[row["aid"]] = second_status if first_status == second_status else "unstable"
    return statuses


def apply_verification_status(progress: ProgressLog, aid: str, status: str) -> None:
    messages = {
        "still_in_source": "回读确认视频仍在来源收藏夹",
        "conflict": "回读发现视频同时存在于来源和目标收藏夹",
        "wrong_target": "回读发现视频位于其他目标收藏夹",
        "missing": "回读未在来源或预期目标收藏夹中找到视频",
        "unstable": "两次回读结果不一致，暂不确认",
    }

    if status == "confirmed":
        progress.update(aid, move_status="succeeded", verify_status="confirmed", last_error="")
    elif status == "still_in_source":
        progress.update(aid, move_status="failed", verify_status=status, last_error=messages[status])
    elif status in {"conflict", "wrong_target"}:
        progress.update(aid, move_status=status, verify_status=status, last_error=messages[status])
    else:
        progress.update(aid, move_status="unverified", verify_status=status, last_error=messages[status])


def print_summary(console: Console, progress: ProgressLog, progress_path: Path) -> None:
    """汇总整个进度文件，而不是只汇总本次仍在来源中的视频。"""
    rows = list(progress.rows.values())
    counts = Counter(
        f"{row.get('classify_status', 'unknown')}/{row.get('move_status', 'unknown')}/{row.get('verify_status', 'unknown')}"
        for row in rows
    )

    table = Table(title="本次整理汇总", show_header=True, header_style="bold cyan")
    table.add_column("状态组合", style="yellow")
    table.add_column("数量", justify="right", style="green")
    for status, count in sorted(counts.items()):
        table.add_row(status, str(count))
    console.print(table)
    console.print(f"\n进度文件已保存：{progress_path}")


async def classify_async():
    """以 AI 生产者和 Bilibili 单通道消费者流水线完成整理。"""
    console = Console()
    bili_client: Optional[BilibiliClient] = None
    ai_classifier: Optional[AIClassifier] = None
    progress_log: Optional[ProgressLog] = None
    progress_path: Optional[Path] = None

    try:
        config_manager = ConfigManager()
        bili_config, ai_config = await ensure_config_is_ready(config_manager)
        (
            request_delay,
            max_retries,
            ai_batch_size,
            verify_batch_size,
            ai_concurrency,
        ) = load_runtime_settings()

        console.print("[cyan]正在初始化客户端...[/cyan]")
        bili_client = BilibiliClient(
            bili_config,
            request_delay=request_delay,
            max_retries=max_retries,
        )
        ai_classifier = AIClassifier(ai_config, max_retries=max_retries)

        console.print("[cyan]正在获取您的收藏夹列表...[/cyan]")
        folders: List[FavoriteFolder] = await bili_client.get_favorite_folders()
        if not folders:
            console.print("[yellow]您没有任何收藏夹，或者无法获取收藏夹列表。[/yellow]")
            return

        folder_table = Table(title="您的 Bilibili 收藏夹", show_header=True, header_style="bold magenta")
        folder_table.add_column("序号", style="dim", width=6)
        folder_table.add_column("收藏夹名称", min_width=20)
        folder_table.add_column("视频数量", justify="right")
        for index, folder in enumerate(folders, 1):
            folder_table.add_row(str(index), folder.title, str(folder.media_count))
        console.print(folder_table)

        resume_path: Optional[Path] = None
        resume_candidates = find_resume_candidates(folders)
        if len(resume_candidates) == 1:
            resume_path, selected_folder, target_folders = resume_candidates[0]
            console.print(
                f"[cyan]检测到现有进度，自动继续："
                f"{selected_folder.title} → {', '.join(folder.title for folder in target_folders)}[/cyan]"
            )
        else:
            if len(resume_candidates) > 1:
                console.print("[yellow]检测到多个可继续的进度文件，请手动选择本次任务。[/yellow]")

            choice = IntPrompt.ask(
                "[bold green]请输入要分类的收藏夹序号[/bold green]",
                choices=[str(index) for index in range(1, len(folders) + 1)],
                show_choices=False,
            )
            selected_folder = folders[choice - 1]
            console.print(f"您选择了: [bold yellow]{selected_folder.title}[/bold yellow]")

            while True:
                target_choices_str = Prompt.ask(
                    "\n[bold green]请输入目标收藏夹序号（多个请用英文逗号隔开，例如 1,3,4）[/bold green]",
                    default=str(choice),
                )
                try:
                    target_indices = [int(item.strip()) for item in target_choices_str.split(",")]
                    if target_indices and all(1 <= index <= len(folders) for index in target_indices):
                        target_folders = [folders[index - 1] for index in target_indices]
                        break
                    console.print("[red]输入包含无效的序号，请重新输入。[/red]")
                except ValueError:
                    console.print("[red]输入格式不正确，请输入数字并用逗号隔开。[/red]")

        target_folder_names = [folder.title for folder in target_folders]
        target_by_name = {
            normalize_category(folder.title): folder
            for folder in target_folders
        }
        console.print(f"您选择的目标收藏夹是: [bold yellow]{', '.join(target_folder_names)}[/bold yellow]")

        progress_path = resume_path or progress_path_for(selected_folder, target_folders)
        progress_log = ProgressLog(progress_path)
        console.print(f"\n[cyan]正在获取 “{selected_folder.title}” 中的所有视频...[/cyan]")
        videos: List[VideoInfo] = await bili_client.get_videos_in_folder(selected_folder.id)
        progress_log.ensure_videos(videos, selected_folder.id, selected_folder.title)
        progress_log.save()
        console.print(f"[dim]进度记录：{progress_path}[/dim]")

        reconciliation_rows = rows_needing_reconciliation(progress_log, selected_folder.id)
        if not videos and not reconciliation_rows:
            console.print(f"[yellow]收藏夹 “{selected_folder.title}” 中没有待处理视频。[/yellow]")
            print_summary(console, progress_log, progress_path)
            return

        video_batches = [
            videos[index:index + ai_batch_size]
            for index in range(0, len(videos), ai_batch_size)
        ]
        if videos:
            console.print(
                f"\n[cyan]准备处理 {len(videos)} 个视频："
                f"AI 每批 {ai_batch_size} 个、并发 {ai_concurrency}，"
                f"每累计 {verify_batch_size} 个移动后核实，请稍候...[/cyan]"
            )
        else:
            console.print(
                f"\n[cyan]来源收藏夹当前为空，先核实 {len(reconciliation_rows)} 条未完成进度，请稍候...[/cyan]"
            )

        global_stop_reason: Optional[str] = None
        stop_event = asyncio.Event()
        classified_queue: asyncio.Queue = asyncio.Queue()
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            AdaptiveTimeRemainingColumn(),
            console=console,
        ) as progress:
            ai_task = progress.add_task("[cyan]AI 分类...[/cyan]", total=len(videos))
            move_task = progress.add_task("[green]B站移动/核实...[/green]", total=len(videos))
            ai_semaphore = asyncio.Semaphore(ai_concurrency)
            verification_rows: List[Dict[str, str]] = []

            async def classify_batch(
                batch_index: int,
                batch: List[VideoInfo],
            ) -> Optional[Tuple[int, List[VideoInfo]]]:
                nonlocal global_stop_reason
                async with ai_semaphore:
                    if stop_event.is_set():
                        return None

                    pending_classification: List[VideoInfo] = []
                    for video in batch:
                        row = progress_log.get(video.aid)
                        if row is None or is_terminal_classification(row) or is_terminal_move(row):
                            continue
                        if row.get("classify_status") != "classified":
                            pending_classification.append(video)

                    if pending_classification:
                        try:
                            classifications = await ai_classifier.batch_classify_videos(
                                pending_classification,
                                target_folders=target_folder_names,
                            )
                        except AIClassifierError as exc:
                            for video in pending_classification:
                                progress_log.update(
                                    video.aid,
                                    classify_status="blocked" if exc.global_error else "failed",
                                    attempts=exc.attempts,
                                    last_error=str(exc),
                                )
                            progress_log.save()
                            if exc.global_error:
                                global_stop_reason = f"AI 全局错误：{exc}"
                                stop_event.set()
                                return None
                            console.print(
                                f"[yellow]第 {batch_index} 批 AI 分类失败，已记录并继续后续批次：{exc}[/yellow]"
                            )
                        else:
                            for video, raw_category in zip(pending_classification, classifications):
                                category = normalize_category(raw_category)
                                target_folder = target_by_name.get(category)
                                if not category:
                                    progress_log.update(
                                        video.aid,
                                        classify_status="uncertain",
                                        move_status="not_required",
                                        verify_status="not_required",
                                        last_error="AI 无法可靠确定分类",
                                    )
                                elif target_folder is None:
                                    progress_log.update(
                                        video.aid,
                                        classify_status="invalid",
                                        move_status="not_required",
                                        verify_status="not_required",
                                        last_error=f"AI 返回的分类不在目标列表中: {category}",
                                    )
                                else:
                                    progress_log.update(
                                        video.aid,
                                        target_folder_id=target_folder.id,
                                        target_folder_name=target_folder.title,
                                        classify_status="classified",
                                        move_status=(
                                            "skipped_same_folder"
                                            if target_folder.id == selected_folder.id
                                            else "pending"
                                        ),
                                        verify_status=(
                                            "not_required"
                                            if target_folder.id == selected_folder.id
                                            else "pending"
                                        ),
                                        attempts="0",
                                        last_error="",
                                    )
                            progress_log.save()

                    progress.update(ai_task, advance=len(batch))
                    return batch_index, batch

            async def produce_classified_batches() -> None:
                tasks = [
                    asyncio.create_task(classify_batch(batch_index, batch))
                    for batch_index, batch in enumerate(video_batches, 1)
                ]
                try:
                    for task in asyncio.as_completed(tasks):
                        result = await task
                        if result is not None and not stop_event.is_set():
                            await classified_queue.put(result)
                        if stop_event.is_set():
                            break
                finally:
                    if stop_event.is_set():
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    classified_queue.put_nowait(None)

            async def flush_verification(force: bool = False) -> bool:
                nonlocal global_stop_reason
                if not verification_rows or (not force and len(verification_rows) < verify_batch_size):
                    return True

                rows_to_verify = verification_rows[:]
                verification_rows.clear()
                try:
                    verification = await verify_batch(
                        bili_client,
                        selected_folder.id,
                        rows_to_verify,
                        target_ids_for_rows(rows_to_verify, selected_folder.id),
                        verification_delay=max(request_delay, 1.0),
                    )
                except BilibiliClientError as exc:
                    for row in rows_to_verify:
                        progress_log.update(
                            row["aid"],
                            verify_status="error",
                            last_error=f"批次回读失败：{exc}",
                        )
                    progress_log.save()
                    global_stop_reason = f"Bilibili 回读失败：{exc}"
                    stop_event.set()
                    return False

                for aid, status in verification.items():
                    apply_verification_status(progress_log, aid, status)
                progress_log.save()
                return True

            async def consume_classified_batches() -> None:
                nonlocal global_stop_reason
                while not stop_event.is_set():
                    item = await classified_queue.get()
                    if item is None:
                        break

                    _, batch = item
                    for video in batch:
                        if stop_event.is_set():
                            break

                        row = progress_log.get(video.aid)
                        if row is None or row.get("classify_status") != "classified":
                            continue

                        if is_terminal_move(row):
                            continue
                        if row.get("move_status") == "api_accepted":
                            verification_rows.append(row)
                            if not await flush_verification():
                                break
                            continue

                        target_folder_id = int(row["target_folder_id"])
                        if target_folder_id == selected_folder.id:
                            progress_log.update(
                                video.aid,
                                move_status="skipped_same_folder",
                                verify_status="not_required",
                            )
                            progress_log.save()
                            continue

                        try:
                            move_result = await bili_client.move_video(
                                video_aid=video.aid,
                                source_folder_id=selected_folder.id,
                                target_folder_id=target_folder_id,
                            )
                        except BilibiliRateLimitError as exc:
                            progress_log.update(
                                video.aid,
                                move_status="blocked",
                                verify_status="error",
                                attempts=exc.attempts,
                                last_error=str(exc),
                            )
                            progress_log.save()
                            global_stop_reason = f"Bilibili 限流：{exc}"
                            stop_event.set()
                            break
                        except BilibiliGlobalError as exc:
                            progress_log.update(
                                video.aid,
                                move_status="blocked",
                                verify_status="error",
                                attempts=exc.attempts,
                                last_error=str(exc),
                            )
                            progress_log.save()
                            global_stop_reason = f"Bilibili 全局错误：{exc}"
                            stop_event.set()
                            break
                        except BilibiliClientError as exc:
                            progress_log.update(
                                video.aid,
                                move_status="failed",
                                verify_status="pending",
                                attempts=exc.attempts,
                                last_error=str(exc),
                            )
                            progress_log.save()
                            console.print(f"[yellow]视频 {video.aid} 移动失败，已记录并继续：{exc}[/yellow]")
                        else:
                            progress_log.update(
                                video.aid,
                                move_status="api_accepted",
                                verify_status="pending",
                                attempts=move_result.attempts,
                                last_error="",
                            )
                            progress_log.save()

                        row = progress_log.get(video.aid)
                        if row and row.get("move_status") in {"api_accepted", "failed", "unverified"}:
                            verification_rows.append(row)
                        if not await flush_verification():
                            break

                    if stop_event.is_set():
                        break
                    progress.update(move_task, advance=len(batch))

                if not stop_event.is_set():
                    await flush_verification(force=True)

            for index in range(0, len(reconciliation_rows), verify_batch_size):
                verification_rows.extend(reconciliation_rows[index:index + verify_batch_size])
                if not await flush_verification(force=True):
                    break

            if not stop_event.is_set() and videos:
                producer_task = asyncio.create_task(produce_classified_batches())
                try:
                    await consume_classified_batches()
                finally:
                    if stop_event.is_set() and not producer_task.done():
                        producer_task.cancel()
                        await asyncio.gather(producer_task, return_exceptions=True)
                    else:
                        await producer_task

        if global_stop_reason:
            console.print(f"\n[bold red]已安全停止：{global_stop_reason}[/bold red]")
        else:
            console.print("\n[bold green]🎉 本次批次处理完成。[/bold green]")
        if progress_log and progress_path:
            print_summary(console, progress_log, progress_path)

    except ProgressLogError as exc:
        console.print(f"\n[bold red]进度文件错误，已停止：{exc}[/bold red]")
    except Exception as exc:
        console.print(f"\n[bold red]发生错误，已停止：{exc}[/bold red]")
    finally:
        if bili_client:
            console.print("[cyan]正在关闭 Bilibili 客户端...[/cyan]")
            await bili_client.close()
        if ai_classifier:
            console.print("[cyan]正在关闭 AI 客户端...[/cyan]")
            await ai_classifier.close()
