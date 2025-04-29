# backend/tasks.py
import logging
import re
import traceback
from datetime import datetime
from typing import Dict, Any
from flask import current_app

from celery import Task
from celery_init import celery_app
from celery.exceptions import Ignore

from database import db
from models import Novel, Chapter, WordStat, DownloadTask, TaskStatus

try:
    from novel_downloader.novel_src.base_system.context import GlobalContext
    from novel_downloader.novel_src.network_parser.network import NetworkClient
    from novel_downloader.novel_src.network_parser.downloader import ChapterDownloader
    from novel_downloader.novel_src.book_parser.book_manager import BookManager

    DOWNLOADER_AVAILABLE = True
except ImportError as e:
    logging.getLogger("celery.tasks.init").error(f"Failed novel_downloader import: {e}")
    DOWNLOADER_AVAILABLE = False

from analysis import update_word_stats
from config import get_downloader_config

try:
    from app import emit_task_update
except ImportError:

    def emit_task_update(user_id: int, task_data: dict):
        logger = logging.getLogger(f"celery.task.emit_fallback")
        logger.warning(
            f"emit_task_update fallback. User: {user_id}, Task: {task_data.get('id')}, Status: {task_data.get('status')}"
        )
        pass


# --- Helper Function to Update DB Task ---
def _update_db_task_status(
    db_task_id: int,
    user_id: int,
    status: TaskStatus,
    progress: int = None,
    message: str = None,
    celery_task_id: str = None,
):
    logger = logging.getLogger("celery.tasks")
    try:
        task = DownloadTask.query.get(db_task_id)
        if task:
            task.status = status
            if progress is not None:
                task.progress = max(0, min(100, progress))
            if message is not None:
                task.message = (
                    (message[:250] + "...") if len(message) > 253 else message
                )
            if celery_task_id:
                task.celery_task_id = celery_task_id
            task.updated_at = datetime.utcnow()

            db.session.commit()
            emit_task_update(user_id, task.to_dict())
            return True
        else:
            logger.error(
                f"DB Task ID {db_task_id} not found for update (User: {user_id}, Status: {status.name})."
            )
            return False
    except Exception as e:
        db.session.rollback()
        logger.error(
            f"Failed to update DB task {db_task_id} for user {user_id} to status {status.name}: {e}",
            exc_info=True,
        )
        return False


# --- Download and Process Task ---
@celery_app.task(bind=True, name="tasks.process_novel")
def process_novel_task(
    self, novel_id: int, user_id: int, db_task_id: int
) -> Dict[str, Any]:
    logger = current_app.logger
    celery_task_id = self.request.id
    logger.info(
        f"Celery Task {celery_task_id}: Starting processing for Novel ID: {novel_id}, User ID: {user_id}, DB Task ID: {db_task_id}"
    )

    if not _update_db_task_status(
        db_task_id,
        user_id,
        TaskStatus.DOWNLOADING,
        progress=0,
        message="Initializing download...",
        celery_task_id=celery_task_id,
    ):
        logger.error(
            f"Task {celery_task_id}: Failed initial DB status update for DB Task {db_task_id}. Aborting."
        )
        return {"status": "FAILURE", "message": "Failed initial DB update"}

    if self.is_revoked():
        logger.warning(f"Task {celery_task_id} termination requested before start.")
        _update_db_task_status(
            db_task_id,
            user_id,
            TaskStatus.TERMINATED,
            message="Task terminated before start.",
        )
        return {"status": "TERMINATED", "message": "Task terminated before start"}

    if not DOWNLOADER_AVAILABLE:
        logger.error(
            f"Task {celery_task_id}: novel_downloader components not available. Aborting."
        )
        _update_db_task_status(
            db_task_id,
            user_id,
            TaskStatus.FAILED,
            message="Downloader components missing.",
        )
        return {"status": "FAILURE", "message": "Downloader components missing"}

    try:
        downloader_config = get_downloader_config()
        GlobalContext.initialize(config_data=downloader_config, logger=logger)
    except Exception as init_e:
        logger.critical(
            f"Task {celery_task_id}: Failed novel_downloader context init: {init_e}",
            exc_info=True,
        )
        _update_db_task_status(
            db_task_id,
            user_id,
            TaskStatus.FAILED,
            message=f"Downloader context init failed: {init_e}",
        )
        return {
            "status": "FAILURE",
            "message": f"Downloader context init failed: {init_e}",
        }

    network_client = NetworkClient()
    book_manager = None

    try:
        logger.info(f"Task {celery_task_id}: Fetching book info...")
        _update_db_task_status(
            db_task_id,
            user_id,
            TaskStatus.DOWNLOADING,
            progress=5,
            message="Fetching book info...",
        )
        book_info_tuple = network_client.get_book_info(str(novel_id))
        if book_info_tuple is None:
            raise ValueError(f"Failed to fetch book info for ID {novel_id}.")
        book_name, author, description, tags, chapter_count_src = book_info_tuple

        if self.is_revoked():
            raise Ignore("Terminated during info fetch.")

        cover_url = None
        try:
            cfg = GlobalContext.get_config()
            status_folder = cfg.status_folder_path(book_name, str(novel_id))
            safe_book_name = re.sub(r'[\\/*?:"<>|]', "_", book_name)
            cover_path = status_folder / f"{safe_book_name}.jpg"
            cover_url_api = f"/api/novels/{novel_id}/cover"
            if cover_path.exists():
                cover_url = cover_url_api
        except Exception as cover_path_err:
            logger.warning(
                f"Task {celery_task_id}: Could not determine cover path: {cover_path_err}"
            )

        logger.info(
            f"Task {celery_task_id}: Book Info: Name='{book_name}', Author='{author}', Chapters='{chapter_count_src}'"
        )

        novel = Novel.query.get(novel_id)
        if not novel:
            novel = Novel(id=novel_id)
            db.session.add(novel)
        novel.title = book_name or f"小说 {novel_id}"
        novel.author = author
        novel.description = description
        novel.tags = "|".join(tags) if isinstance(tags, list) else tags
        novel.status = tags[0] if isinstance(tags, list) and tags else "未知"
        novel.total_chapters = chapter_count_src
        novel.cover_image_url = cover_url
        novel.last_crawled_at = datetime.utcnow()
        db.session.commit()

        logger.info(f"Task {celery_task_id}: Fetching chapter list...")
        _update_db_task_status(
            db_task_id,
            user_id,
            TaskStatus.DOWNLOADING,
            progress=10,
            message="Fetching chapter list...",
        )
        chapters_list = network_client.fetch_chapter_list(str(novel_id))
        if chapters_list is None:
            raise ValueError(f"Failed to fetch chapter list for ID {novel_id}.")
        total_chapters_src = len(chapters_list)
        logger.info(f"Task {celery_task_id}: Found {total_chapters_src} chapters.")

        if self.is_revoked():
            raise Ignore("Terminated during chapter list fetch.")

        if not chapters_list:
            _update_db_task_status(
                db_task_id,
                user_id,
                TaskStatus.COMPLETED,
                progress=100,
                message="No chapters found in source.",
            )
            return {
                "status": "SUCCESS",
                "message": "No chapters found",
                "chapters_processed": 0,
            }

        # Define the callback function
        chapters_to_download_count = (
            total_chapters_src  # Assume all need download initially
        )

        def report_download_progress(completed: int, total: int):
            if total > 0:
                download_progress_percentage = 15 + int((completed / total) * (85 - 15))
                progress = max(15, min(85, download_progress_percentage))
                message = f"Downloading {completed}/{total} chapters..."
                _update_db_task_status(
                    db_task_id,
                    user_id,
                    TaskStatus.DOWNLOADING,
                    progress=progress,
                    message=message,
                )
            # logger.debug(f"Progress callback: {completed}/{total} chapters done.")

        book_manager = BookManager(
            book_id=str(novel_id),
            book_name=book_name,
            author=author,
            tags=tags,
            description=description,
        )
        downloader = ChapterDownloader(str(novel_id), network_client)

        logger.info(f"Task {celery_task_id}: Starting chapter download...")
        _update_db_task_status(
            db_task_id,
            user_id,
            TaskStatus.DOWNLOADING,
            progress=15,
            message=f"Downloading 0/{chapters_to_download_count} chapters...",
        )

        # Pass the callback to download_book
        download_results = downloader.download_book(
            book_manager=book_manager,
            book_name=book_name,
            chapters=chapters_list,
            progress_callback=report_download_progress,  # Pass the function
        )

        logger.info(f"Task {celery_task_id}: Download results: {download_results}")
        _update_db_task_status(
            db_task_id,
            user_id,
            TaskStatus.PROCESSING,
            progress=85,
            message="Processing downloaded chapters...",
        )

        if self.is_revoked():
            raise Ignore("Terminated after download.")

        downloaded_data = book_manager.get_downloaded_data()
        downloaded_count_mgr = len(downloaded_data)
        logger.info(
            f"Task {celery_task_id}: Retrieved {downloaded_count_mgr} chapters from BookManager."
        )
        _update_db_task_status(
            db_task_id,
            user_id,
            TaskStatus.PROCESSING,
            progress=90,
            message="Saving chapters to database...",
        )

        saved_count = 0
        chapters_with_errors = []
        try:
            with db.session.begin_nested():
                deleted_count = Chapter.query.filter_by(novel_id=novel_id).delete()
                if deleted_count > 0:
                    logger.info(
                        f"Task {celery_task_id}: Deleted {deleted_count} old chapters."
                    )

                for index, chapter_meta in enumerate(chapters_list):
                    if index % 50 == 0 and self.is_revoked():
                        raise Ignore("Terminated during DB save.")

                    chapter_id_str = chapter_meta["id"]
                    chapter_data = downloaded_data.get(chapter_id_str)

                    if (
                        chapter_data
                        and isinstance(chapter_data, list)
                        and len(chapter_data) == 2
                    ):
                        ch_title, ch_content = chapter_data
                        ch_title = (
                            ch_title
                            or chapter_meta.get("title")
                            or f"Chapter {chapter_id_str}"
                        )
                        is_error_content = isinstance(ch_content, str) and (
                            "Error" in ch_content or ch_content == "Empty Content"
                        )

                        if ch_content and not is_error_content:
                            try:
                                chapter_id_db = int(chapter_id_str)
                                chapter_entry = Chapter(
                                    id=chapter_id_db,
                                    novel_id=novel_id,
                                    chapter_index=index,
                                    title=ch_title,
                                    content=ch_content,
                                    fetched_at=datetime.utcnow(),
                                )
                                db.session.merge(chapter_entry)
                                saved_count += 1
                            except ValueError:
                                chapters_with_errors.append(
                                    {
                                        "id": chapter_id_str,
                                        "title": ch_title,
                                        "reason": "Invalid ID",
                                    }
                                )
                            except Exception as chapter_save_err:
                                chapters_with_errors.append(
                                    {
                                        "id": chapter_id_str,
                                        "title": ch_title,
                                        "reason": f"DB Error: {chapter_save_err}",
                                    }
                                )
                        else:
                            chapters_with_errors.append(
                                {
                                    "id": chapter_id_str,
                                    "title": ch_title,
                                    "reason": ch_content
                                    if isinstance(ch_content, str)
                                    else "DL Error/Empty",
                                }
                            )
                    else:
                        chapters_with_errors.append(
                            {
                                "id": chapter_id_str,
                                "title": chapter_meta.get(
                                    "title", f"Unknown {chapter_id_str}"
                                ),
                                "reason": "Missing/Bad Format",
                            }
                        )

            db.session.commit()
            logger.info(
                f"Task {celery_task_id}: Saved/Merged {saved_count} chapters. {len(chapters_with_errors)} errors."
            )

            total_failed_count = (
                download_results.get("failed", 0)
                if isinstance(download_results, dict)
                else 0
            ) + len(chapters_with_errors)
            if book_manager:
                book_manager.finalize_download(chapters_list, total_failed_count)

            analysis_message = ""
            if saved_count > 0:
                _update_db_task_status(
                    db_task_id,
                    user_id,
                    TaskStatus.PROCESSING,
                    progress=95,
                    message="Analyzing content...",
                )
                try:
                    analyze_novel_task(novel_id)  # Sync call
                    analysis_message = "Analysis done."
                except Exception as analysis_err:
                    logger.error(
                        f"Task {celery_task_id}: Analysis failed: {analysis_err}",
                        exc_info=True,
                    )
                    analysis_message = "Analysis failed."
            else:
                analysis_message = "Analysis skipped (no chapters)."
                try:
                    WordStat.query.filter_by(novel_id=novel_id).delete()
                    db.session.commit()
                except Exception as e_stat:
                    logger.error(f"Task {celery_task_id}: Failed clear stats: {e_stat}")
                    db.session.rollback()

            final_message = f"Completed. Saved {saved_count}/{total_chapters_src}. {analysis_message}"
            if chapters_with_errors:
                final_message += f" ({len(chapters_with_errors)} errors)"
            _update_db_task_status(
                db_task_id,
                user_id,
                TaskStatus.COMPLETED,
                progress=100,
                message=final_message,
            )
            logger.info(f"Task {celery_task_id}: Processing successful.")
            return {
                "status": "SUCCESS",
                "message": final_message,
                "chapters_processed_db": saved_count,
                "errors": len(chapters_with_errors),
            }

        except Ignore as term_signal:
            logger.warning(
                f"Task {celery_task_id} processing stopped due to termination: {term_signal}"
            )
            _update_db_task_status(
                db_task_id,
                user_id,
                TaskStatus.TERMINATED,
                message=f"Task terminated: {term_signal}",
            )
            return {
                "status": "TERMINATED",
                "message": f"Task terminated: {term_signal}",
            }
        except Exception as db_commit_err:
            logger.error(
                f"Task {celery_task_id}: DB transaction failed during chapter save: {db_commit_err}",
                exc_info=True,
            )
            _update_db_task_status(
                db_task_id,
                user_id,
                TaskStatus.FAILED,
                message=f"DB transaction failed: {db_commit_err}",
            )
            raise

    except Ignore as term_signal:
        logger.warning(
            f"Task {celery_task_id} processing stopped due to termination: {term_signal}"
        )
        _update_db_task_status(
            db_task_id,
            user_id,
            TaskStatus.TERMINATED,
            message=f"Task terminated: {term_signal}",
        )
        return {"status": "TERMINATED", "message": f"Task terminated: {term_signal}"}

    except Exception as e:
        error_type = type(e).__name__
        error_msg = str(e)
        logger.error(
            f"Task {celery_task_id}: FAILED processing novel {novel_id}. Error: {error_type} - {error_msg}",
            exc_info=True,
        )
        _update_db_task_status(
            db_task_id, user_id, TaskStatus.FAILED, message=f"{error_type}: {error_msg}"
        )
        raise


# --- Analysis Task ---
@celery_app.task(bind=True, name="tasks.analyze_novel")
def analyze_novel_task(self, novel_id: int) -> Dict[str, Any]:
    logger = current_app.logger
    task_id = self.request.id
    logger.info(f"Analysis Task {task_id}: Starting analysis for novel ID: {novel_id}")

    try:
        image_path = update_word_stats(novel_id)
        message = (
            "Analysis complete. Wordcloud generated."
            if image_path
            else "Analysis complete. Wordcloud not generated (no data or error)."
        )
        logger.info(f"Analysis Task {task_id}: {message}")
        return {"status": "SUCCESS", "message": message}
    except Exception as e:
        error_type = type(e).__name__
        error_msg = str(e)
        logger.error(
            f"Analysis Task {task_id}: FAILED analysis for novel {novel_id}. Error: {error_type} - {error_msg}",
            exc_info=True,
        )
        raise
