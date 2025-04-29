# backend/app.py
import logging
import sys
import os
import re
import traceback
from datetime import datetime

from flask import Flask, request, jsonify, send_file, current_app, abort
from flask_jwt_extended import (
    JWTManager,
    jwt_required,
    get_jwt_identity,
    verify_jwt_in_request,
)
from flask_socketio import SocketIO, emit, join_room, leave_room, disconnect
from celery.result import AsyncResult
from sqlalchemy import desc, asc, text as sql_text  # Import text explicitly
from werkzeug.exceptions import HTTPException

# Database and Models
from database import db as _db
from models import Novel, Chapter, WordStat, User, DownloadTask, TaskStatus

# Config and Auth
from config import settings
from auth import auth_bp

# Celery
from celery_init import celery_app, configure_celery

# --- Logging Configuration ---
log_level_str = os.getenv("FLASK_LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_str, logging.INFO)
formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - [%(threadName)s] - %(message)s"
)
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(log_level)
handler.setFormatter(formatter)

# --- Flask App Initialization ---
app = Flask(__name__)
app.config.from_object(settings)

app.logger.addHandler(handler)
app.logger.setLevel(log_level)
app.logger.propagate = False
app.logger.info(f"Flask application starting with log level {log_level_str}")

# --- Extensions Initialization ---
_db.init_app(app)
jwt = JWTManager(app)
socketio = SocketIO(
    app, message_queue=settings.CELERY_BROKER_URL, async_mode="eventlet"
)

app.register_blueprint(auth_bp)

# --- Celery Configuration Linking ---
configure_celery(app)

# --- Database Creation ---
try:
    with app.app_context():
        app.logger.info("Creating database tables if they don't exist...")
        _db.create_all()
        app.logger.info("Database tables checked/created.")
except Exception as e:
    app.logger.error(f"Error during _db.create_all(): {e}", exc_info=True)


# --- Import task functions ---
try:
    from tasks import process_novel_task, analyze_novel_task

    app.logger.info("Task functions imported successfully")
except ImportError as e:
    app.logger.error(f"Failed to import task functions: {e}", exc_info=True)

# ---------- SocketIO Event Handlers ----------

connected_users = {}  # {user_id: sid}


@socketio.on("connect")
def handle_connect():
    app.logger.info(f"Client connected: {request.sid}")
    emit("request_auth", {"message": "Please authenticate with your JWT token."})


@socketio.on("authenticate")
def handle_authenticate(data):
    token = data.get("token")
    sid = request.sid
    if not token:
        app.logger.warning(
            f"Authentication attempt failed for SID {sid}: No token provided."
        )
        emit(
            "auth_response",
            {"success": False, "message": "Authentication token required."},
        )
        disconnect(sid)
        return

    try:
        with app.app_context():
            from flask_jwt_extended.exceptions import NoAuthorizationError

            original_headers = request.headers  # Store original headers if any
            try:
                # Temporarily set header for verify_jwt_in_request
                request.headers = {"Authorization": f"Bearer {token}"}
                verify_jwt_in_request(optional=True)  # Check token validity
                user_identity = get_jwt_identity()
                if user_identity is None:
                    raise NoAuthorizationError("Invalid Token")

                user_id = int(user_identity)
                app.logger.info(
                    f"SID {sid} authenticated successfully for user_id {user_id}."
                )

                # Manage room membership
                leave_room(
                    f"user_{user_id}", sid=sid
                )  # Leave previous room if re-authenticating
                join_room(f"user_{user_id}", sid=sid)
                connected_users[user_id] = sid
                app.logger.info(
                    f"User {user_id} (SID: {sid}) joined room 'user_{user_id}'."
                )

                emit(
                    "auth_response",
                    {"success": True, "message": "Authentication successful."},
                )

            except Exception as e:
                app.logger.error(f"JWT Authentication failed for SID {sid}: {e}")
                emit(
                    "auth_response",
                    {"success": False, "message": f"Invalid token: {e}"},
                )
                disconnect(sid)
            finally:
                # Restore original headers
                request.headers = original_headers

    except Exception as e:
        app.logger.error(
            f"Error during WebSocket authentication for SID {sid}: {e}", exc_info=True
        )
        emit("auth_response", {"success": False, "message": "Authentication failed."})
        disconnect(sid)


@socketio.on("disconnect")
def handle_disconnect():
    sid = request.sid
    user_id = None
    for uid, stored_sid in list(connected_users.items()):  # Iterate over a copy
        if stored_sid == sid:
            user_id = uid
            del connected_users[user_id]
            app.logger.info(
                f"User {user_id} (SID: {sid}) disconnected and left room 'user_{user_id}'."
            )
            break  # Assume one user per sid
    if user_id is None:
        app.logger.info(f"Unauthenticated client disconnected: {sid}")


def emit_task_update(user_id: int, task_data: dict):
    """Helper function to emit task updates via SocketIO"""
    room = f"user_{user_id}"
    if user_id in connected_users:
        sid = connected_users[user_id]
        # Use logger from current_app context
        logger = current_app.logger
        # logger.debug(f"Emitting task_update to room {room} (SID: {sid}) with data: {task_data}")
        socketio.emit("task_update", task_data, room=room)
    # else:
    #     # Log if user is not connected (optional)
    #     logger = current_app.logger
    #     logger.debug(f"User {user_id} not connected, skipping task_update emit.")


# ---------- API Endpoints ----------


@app.route("/api/search", methods=["GET"])
@jwt_required()
def search_novels_api():
    logger = current_app.logger
    query = request.args.get("query")
    if not query:
        return jsonify(error="Missing 'query' parameter"), 400
    logger.info(f"Received search request with query: '{query}'")

    try:
        # Ensure downloader context is initialized
        from novel_downloader.novel_src.base_system.context import GlobalContext
        from novel_downloader.novel_src.network_parser.network import NetworkClient
        from config import get_downloader_config

        if not GlobalContext.is_initialized():
            logger.warning(
                "Downloader context not initialized for search, attempting init..."
            )
            try:
                # Pass the current Flask logger to the downloader context
                GlobalContext.initialize(get_downloader_config(), logger)
            except Exception as init_e:
                logger.error(
                    f"Failed to initialize downloader context for search: {init_e}",
                    exc_info=True,
                )
                return jsonify(error="Downloader initialization failed"), 500

        network_client = NetworkClient()
        search_results = network_client.search_book(query)
        logger.info(
            f"Search for '{query}' returned {len(search_results)} results from source."
        )

        # Format results, ensuring IDs are strings
        formatted_results = [
            {
                "id": str(res.get("book_id"))
                if res.get("book_id") is not None
                else None,
                "title": res.get("title"),
                "author": res.get("author"),
            }
            for res in search_results
        ]
        # Filter out results with no ID
        formatted_results = [res for res in formatted_results if res["id"] is not None]

        return jsonify({"results": formatted_results}), 200

    except ImportError:
        logger.error("Novel downloader components could not be imported for search.")
        return jsonify(error="Search functionality unavailable"), 503
    except Exception as e:
        logger.error(
            f"Error during novel search for query '{query}': {e}", exc_info=True
        )
        return jsonify(error="Internal server error during search"), 500


@app.route("/api/novels", methods=["POST"])
@jwt_required()
def add_novel_and_crawl():
    logger = current_app.logger
    user_id_str = get_jwt_identity()
    if not user_id_str:
        return jsonify(msg="Invalid user identity in token"), 401
    user_id = int(user_id_str)

    data = request.get_json()
    if not data or "novel_id" not in data:
        return jsonify(error="Missing 'novel_id' in request body"), 400

    try:
        novel_id_int = int(data["novel_id"])
    except (ValueError, TypeError):
        return jsonify(error="'novel_id' must be a valid integer"), 400

    logger.info(f"User {user_id} requested add/crawl for novel ID: {novel_id_int}")

    # Check if an active task already exists for this user and novel
    existing_task = DownloadTask.query.filter_by(
        user_id=user_id, novel_id=novel_id_int
    ).first()
    if existing_task and existing_task.status not in [
        TaskStatus.FAILED,
        TaskStatus.TERMINATED,
    ]:
        logger.warning(
            f"User {user_id} already has an active/completed task for novel {novel_id_int} (Task ID: {existing_task.id}, Status: {existing_task.status.name})"
        )
        return jsonify(
            error=f"A task for this novel already exists with status {existing_task.status.name}.",
            task=existing_task.to_dict(),
        ), 409

    # Create a preliminary task record in the DB
    new_db_task = DownloadTask(
        user_id=user_id, novel_id=novel_id_int, status=TaskStatus.PENDING, progress=0
    )
    try:
        _db.session.add(new_db_task)
        _db.session.commit()
        db_task_id = new_db_task.id  # Get the ID after committing
        logger.info(
            f"Created preliminary DB task record with ID: {db_task_id} for novel {novel_id_int}, user {user_id}"
        )
    except Exception as db_err:
        _db.session.rollback()
        logger.error(
            f"Failed to create DB task record for novel {novel_id_int}, user {user_id}: {db_err}",
            exc_info=True,
        )
        return jsonify(error="Failed to create task record in database"), 500

    # Queue the Celery task
    try:
        # Use send_task to call the task by name
        celery_task = celery_app.send_task(
            "tasks.process_novel",  # Name defined in tasks.py
            kwargs={
                "novel_id": novel_id_int,
                "user_id": user_id,
                "db_task_id": db_task_id,  # Pass the created DB task ID
            },
        )
        logger.info(
            f"Queued Celery task for novel ID: {novel_id_int}, user {user_id}, DB Task ID: {db_task_id} -> Celery Task ID: {celery_task.id}"
        )

        # Update the DB task record with the Celery task ID
        new_db_task.celery_task_id = celery_task.id
        _db.session.commit()
        logger.info(
            f"Updated DB Task {db_task_id} with Celery Task ID: {celery_task.id}"
        )

        # Emit initial task status via WebSocket
        emit_task_update(user_id, new_db_task.to_dict())
        return jsonify(new_db_task.to_dict()), 202  # Accepted

    except Exception as e:
        logger.error(
            f"Failed to queue Celery task or update DB for novel {novel_id_int}, user {user_id}, DB Task {db_task_id}: {e}",
            exc_info=True,
        )
        # Clean up the preliminary DB task record if Celery queuing fails
        try:
            _db.session.delete(new_db_task)
            _db.session.commit()
        except:
            _db.session.rollback()
        return jsonify(error="Failed to queue background task"), 500


@app.route("/api/novels", methods=["GET"])
@jwt_required()
def list_novels():
    logger = current_app.logger
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 10, type=int)
    per_page = min(per_page, 50)  # Limit per page

    logger.info(f"Listing novels: page={page}, per_page={per_page}")
    try:
        pagination = Novel.query.order_by(
            Novel.last_crawled_at.is_(None),  # Show novels never crawled first
            Novel.last_crawled_at.desc(),
            Novel.created_at.desc(),
        ).paginate(page=page, per_page=per_page, error_out=False)

        novels_data = [
            {
                "id": str(novel.id),
                "title": novel.title,
                "author": novel.author,
                "status": novel.status,
                "tags": novel.tags,
                "total_chapters": novel.total_chapters,  # From source
                "last_crawled_at": novel.last_crawled_at.isoformat()
                if novel.last_crawled_at
                else None,
                "created_at": novel.created_at.isoformat()
                if novel.created_at
                else None,
                "cover_image_url": novel.cover_image_url,  # URL to fetch cover
            }
            for novel in pagination.items
        ]
        return jsonify(
            {
                "novels": novels_data,
                "total": pagination.total,
                "page": pagination.page,
                "pages": pagination.pages,
                "per_page": pagination.per_page,
            }
        )
    except Exception as e:
        logger.error(f"Error listing novels: {e}", exc_info=True)
        return jsonify(error="Database error fetching novels"), 500


@app.route("/api/novels/<int:novel_id>", methods=["GET"])
@jwt_required()
def get_novel_details(novel_id):
    logger = current_app.logger
    logger.info(f"Fetching details for novel ID: {novel_id}")
    try:
        novel = Novel.query.get_or_404(novel_id)
        # Get actual chapter count from DB
        chapter_count = Chapter.query.filter_by(novel_id=novel.id).count()
        return jsonify(
            {
                "id": str(novel.id),
                "title": novel.title,
                "author": novel.author,
                "description": novel.description,
                "status": novel.status,
                "tags": novel.tags,
                "total_chapters_source": novel.total_chapters,  # Chapters reported by source
                "chapters_in_db": chapter_count,  # Chapters actually in DB
                "last_crawled_at": novel.last_crawled_at.isoformat()
                if novel.last_crawled_at
                else None,
                "created_at": novel.created_at.isoformat()
                if novel.created_at
                else None,
                "cover_image_url": novel.cover_image_url,
            }
        )
    except HTTPException as e:
        # Specifically catch the 404 from get_or_404
        if e.code == 404:
            return jsonify(error=f"Novel with ID {novel_id} not found"), 404
        raise e  # Re-raise other HTTP exceptions
    except Exception as e:
        logger.error(f"Error fetching details for novel {novel_id}: {e}", exc_info=True)
        return jsonify(error="Database error fetching novel details"), 500


@app.route("/api/novels/<int:novel_id>/chapters", methods=["GET"])
@jwt_required()
def get_novel_chapters(novel_id):
    logger = current_app.logger
    logger.info(f"Fetching chapter list for novel ID: {novel_id}")
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    per_page = min(per_page, 200)  # Limit chapters per page

    try:
        # Check if novel exists first to provide better 404
        novel_exists = (
            _db.session.query(Novel.id).filter_by(id=novel_id).scalar() is not None
        )
        if not novel_exists:
            abort(404, description=f"Novel with ID {novel_id} not found.")

        pagination = (
            Chapter.query.filter_by(novel_id=novel_id)
            .order_by(Chapter.chapter_index)  # Order by index
            .paginate(page=page, per_page=per_page, error_out=False)
        )
        chapters_data = [
            {
                "id": str(chapter.id),
                "index": chapter.chapter_index,
                "title": chapter.title,
                "fetched_at": chapter.fetched_at.isoformat()
                if chapter.fetched_at
                else None,
            }
            for chapter in pagination.items
        ]
        return jsonify(
            {
                "chapters": chapters_data,
                "total": pagination.total,
                "page": pagination.page,
                "pages": pagination.pages,
                "per_page": pagination.per_page,
                "novel_id": str(novel_id),
            }
        )
    except HTTPException as e:
        if e.code == 404:
            return jsonify(error=getattr(e, "description", "Not Found")), 404
        raise e
    except Exception as e:
        logger.error(
            f"Error fetching chapters for novel {novel_id}: {e}", exc_info=True
        )
        return jsonify(error="Database error fetching chapters"), 500


@app.route("/api/novels/<int:novel_id>/chapters/<int:chapter_id>", methods=["GET"])
@jwt_required()
def get_chapter_content(novel_id, chapter_id):
    logger = current_app.logger
    logger.info(f"Fetching content for novel ID: {novel_id}, chapter ID: {chapter_id}")
    try:
        chapter = Chapter.query.filter_by(
            novel_id=novel_id, id=chapter_id
        ).first_or_404()
        return jsonify(
            {
                "id": str(chapter.id),
                "novel_id": str(chapter.novel_id),
                "index": chapter.chapter_index,
                "title": chapter.title,
                "content": chapter.content,
            }
        )
    except HTTPException as e:
        if e.code == 404:
            return jsonify(
                error=f"Chapter {chapter_id} for novel {novel_id} not found"
            ), 404
        raise e
    except Exception as e:
        logger.error(
            f"Error fetching chapter content for novel {novel_id}, chapter {chapter_id}: {e}",
            exc_info=True,
        )
        return jsonify(error="Database error fetching chapter content"), 500


@app.route("/api/novels/<int:novel_id>/cover", methods=["GET"])
# No JWT required for cover images, assuming they can be public if link is known
def get_novel_cover(novel_id):
    logger = current_app.logger
    novel = Novel.query.get(novel_id)
    if not novel:
        abort(404, description="Novel not found")
    if not novel.title:
        abort(404, description="Cannot determine cover path (missing title)")

    try:
        # --- Access downloader config for paths ---
        from novel_downloader.novel_src.base_system.context import GlobalContext
        from config import get_downloader_config

        # Ensure context is initialized (idempotent)
        if not GlobalContext.is_initialized():
            try:
                GlobalContext.initialize(get_downloader_config(), logger)
            except Exception as init_err:
                logger.error(
                    f"Failed to initialize downloader context for cover: {init_err}"
                )
                abort(503, description="Cover functionality unavailable (init failed)")

        cfg = GlobalContext.get_config()
        status_folder = cfg.status_folder_path(
            novel.title, str(novel.id)
        )  # Get specific folder
        # Sanitize book name for filename consistency
        safe_book_name = re.sub(r'[\\/*?:"<>|]', "_", novel.title)
        cover_filename = f"{safe_book_name}.jpg"
        cover_path = status_folder / cover_filename
        # --- End path logic ---

        if cover_path.exists() and cover_path.is_file():
            return send_file(str(cover_path), mimetype="image/jpeg")
        else:
            abort(404, description="Cover image not found locally")

    except ImportError:
        abort(503, description="Cover functionality unavailable")
    except HTTPException as e:
        raise e  # Re-raise aborts
    except Exception as e:
        logger.error(f"Error serving cover for novel {novel_id}: {e}", exc_info=True)
        abort(500, description="Error serving cover image")


# --- Task API Routes (Merged from tasks_api.py) ---


@app.route("/api/tasks/list", methods=["GET"])
@jwt_required()
def list_user_tasks():
    logger = current_app.logger
    user_id_str = get_jwt_identity()
    if not user_id_str:
        return jsonify(msg="Invalid user identity"), 401
    user_id = int(user_id_str)
    logger.info(f"Fetching task list for user ID: {user_id}")

    try:
        # Eager load novel details to avoid N+1 queries in to_dict()
        tasks = (
            DownloadTask.query.filter_by(user_id=user_id)
            .options(db.joinedload(DownloadTask.novel))
            .order_by(DownloadTask.created_at.desc())
            .all()
        )
        task_list = [task.to_dict() for task in tasks]
        return jsonify(tasks=task_list)
    except Exception as e:
        logger.error(f"Error fetching task list for user {user_id}: {e}", exc_info=True)
        return jsonify(error="Database error fetching task list"), 500


@app.route("/api/tasks/<int:db_task_id>/terminate", methods=["POST"])
@jwt_required()
def terminate_task(db_task_id):
    logger = current_app.logger
    user_id_str = get_jwt_identity()
    if not user_id_str:
        return jsonify(msg="Invalid user identity"), 401
    user_id = int(user_id_str)
    logger.info(f"User {user_id} requesting termination for DB Task ID: {db_task_id}")

    try:
        task = DownloadTask.query.filter_by(id=db_task_id, user_id=user_id).first()
        if not task:
            return jsonify(error="Task not found or access denied"), 404

        # If task has no celery ID (e.g., failed before queuing)
        if not task.celery_task_id:
            if task.status in [
                TaskStatus.PENDING,
                TaskStatus.DOWNLOADING,
                TaskStatus.PROCESSING,
            ]:
                # Just mark as failed/terminated in DB
                task.status = (
                    TaskStatus.FAILED
                )  # Or TERMINATED? FAILED seems more accurate
                task.message = "No Celery task ID found to terminate."
                _db.session.commit()
                emit_task_update(user_id, task.to_dict())
            return jsonify(error="Task has no associated process ID"), 400

        # If task is already finished
        if task.status in [
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.TERMINATED,
        ]:
            return jsonify(
                message="Task is already finished or terminated.", task=task.to_dict()
            ), 200

        # Attempt to revoke the Celery task
        logger.info(
            f"Attempting to terminate Celery task: {task.celery_task_id} for DB Task: {db_task_id}"
        )
        celery_app.control.revoke(task.celery_task_id, terminate=True, signal="SIGTERM")

        # Update DB status immediately
        task.status = TaskStatus.TERMINATED
        task.progress = 0  # Reset progress
        task.message = "Task terminated by user."
        _db.session.commit()

        logger.info(f"DB Task {db_task_id} status updated to TERMINATED.")
        emit_task_update(user_id, task.to_dict())  # Notify client
        return jsonify(message="Task termination signal sent.", task=task.to_dict())

    except Exception as e:
        _db.session.rollback()
        logger.error(
            f"Error terminating task {db_task_id} for user {user_id}: {e}",
            exc_info=True,
        )
        return jsonify(error="Failed to terminate task"), 500


@app.route("/api/tasks/<int:db_task_id>", methods=["DELETE"])
@jwt_required()
def delete_task(db_task_id):
    logger = current_app.logger
    user_id_str = get_jwt_identity()
    if not user_id_str:
        return jsonify(msg="Invalid user identity"), 401
    user_id = int(user_id_str)
    logger.info(f"User {user_id} requesting deletion for DB Task ID: {db_task_id}")

    try:
        task = DownloadTask.query.filter_by(id=db_task_id, user_id=user_id).first()
        if not task:
            return jsonify(error="Task not found or access denied"), 404

        celery_id_to_forget = task.celery_task_id

        # Delete from DB
        _db.session.delete(task)
        _db.session.commit()
        logger.info(
            f"Successfully deleted DB Task ID: {db_task_id} for user {user_id}."
        )

        # Forget Celery result if applicable
        if celery_id_to_forget:
            try:
                AsyncResult(celery_id_to_forget, app=celery_app).forget()
                logger.debug(f"Forgot result for Celery task {celery_id_to_forget}")
            except Exception as forget_err:
                logger.warning(
                    f"Could not forget Celery result for {celery_id_to_forget}: {forget_err}"
                )

        # Emit deletion event via WebSocket
        emit_task_update(user_id, {"id": db_task_id, "deleted": True})
        return jsonify(message="Task deleted successfully.")

    except Exception as e:
        _db.session.rollback()
        logger.error(
            f"Error deleting task {db_task_id} for user {user_id}: {e}", exc_info=True
        )
        return jsonify(error="Failed to delete task"), 500


@app.route("/api/tasks/<int:db_task_id>/redownload", methods=["POST"])
@jwt_required()
def redownload_task(db_task_id):
    logger = current_app.logger
    user_id_str = get_jwt_identity()
    if not user_id_str:
        return jsonify(msg="Invalid user identity"), 401
    user_id = int(user_id_str)
    logger.info(f"User {user_id} requesting re-download for DB Task ID: {db_task_id}")

    try:
        task = DownloadTask.query.filter_by(id=db_task_id, user_id=user_id).first()
        if not task:
            return jsonify(error="Task not found or access denied"), 404

        # Prevent re-downloading an active task
        if task.status in [
            TaskStatus.DOWNLOADING,
            TaskStatus.PROCESSING,
            TaskStatus.PENDING,
        ]:
            return jsonify(
                error=f"Cannot re-download an active task ({task.status.name}). Terminate it first if needed."
            ), 409

        novel_id_to_redownload = task.novel_id
        logger.info(
            f"Re-downloading Novel ID: {novel_id_to_redownload} for User: {user_id} (from DB Task: {db_task_id})"
        )

        # Reset task state in DB
        task.status = TaskStatus.PENDING
        task.progress = 0
        task.message = "Re-download requested."
        task.celery_task_id = None  # Clear old Celery ID
        _db.session.commit()
        logger.info(f"Reset DB Task {db_task_id} to PENDING state.")
        emit_task_update(user_id, task.to_dict())  # Notify client of reset

        # Queue a new Celery task
        try:
            celery_task = celery_app.send_task(
                "tasks.process_novel",  # Name of the task
                kwargs={
                    "novel_id": novel_id_to_redownload,
                    "user_id": user_id,
                    "db_task_id": db_task_id,  # Re-use the DB task ID
                },
            )
            logger.info(
                f"Queued new Celery task for re-download: DB Task ID: {db_task_id} -> New Celery Task ID: {celery_task.id}"
            )

            # Update DB task with the new Celery ID
            task.celery_task_id = celery_task.id
            _db.session.commit()
            logger.info(
                f"Updated DB Task {db_task_id} with new Celery Task ID: {celery_task.id}"
            )

            # emit_task_update(user_id, task.to_dict()) # Optional: emit again with celery ID

            return jsonify(
                message="Re-download task queued successfully.", task=task.to_dict()
            ), 202

        except Exception as queue_err:
            logger.error(
                f"Failed to queue Celery task for re-download (DB Task {db_task_id}): {queue_err}",
                exc_info=True,
            )
            # Revert status to FAILED if queuing fails
            task.status = TaskStatus.FAILED
            task.message = f"Failed to queue re-download: {queue_err}"
            _db.session.commit()
            emit_task_update(user_id, task.to_dict())
            return jsonify(error="Failed to queue background task for re-download"), 500

    except Exception as e:
        _db.session.rollback()
        logger.error(
            f"Error during re-download process for task {db_task_id}: {e}",
            exc_info=True,
        )
        return jsonify(error="Failed to initiate re-download"), 500


# --- END Merged Task API Routes ---


@app.route("/api/tasks/status/<string:task_id>", methods=["GET"])
@jwt_required()
def get_task_status(task_id):
    logger = current_app.logger
    # logger.info(f"Checking status for Celery task ID: {task_id}")

    # Basic validation for task_id format
    if not task_id or len(task_id) > 64 or not task_id.replace("-", "").isalnum():
        return jsonify(error="Invalid Celery task ID format"), 400

    task_result = AsyncResult(task_id, app=celery_app)
    status = task_result.status
    result = task_result.result

    response = {
        "task_id": task_id,
        "status": status,
        "result": None,  # Generic status message
        "meta": None,  # Specific result/error details
    }

    # Provide user-friendly status descriptions
    if status == "PENDING":
        response["result"] = "Task is waiting to be processed."
    elif status == "STARTED":
        response["result"] = "Task has started processing."
    elif status == "PROGRESS":
        response["result"] = "Task is in progress."
    elif status == "SUCCESS":
        response["result"] = "Task completed successfully."
    elif status == "FAILURE":
        response["result"] = "Task failed during execution."
    elif status == "REVOKED":
        response["result"] = "Task was terminated."
    else:
        response["result"] = f"Unknown state: {status}"

    # Extract meaningful result/error information
    if isinstance(result, dict):
        response["meta"] = result  # If task returns a dict
    elif isinstance(result, Exception):
        response["meta"] = {
            "exc_type": type(result).__name__,
            "exc_message": str(result),
        }
        # Optionally include traceback if needed and available
        # if hasattr(task_result, 'traceback'):
        #     response['traceback'] = task_result.traceback
    else:
        # Use task_result.info which often holds metadata during PROGRESS
        response["meta"] = task_result.info

    # If status is FAILURE but meta is still None, capture traceback if possible
    if (
        status == "FAILURE"
        and response["meta"] is None
        and hasattr(task_result, "traceback")
    ):
        response["traceback"] = task_result.traceback

    # logger.debug(f"Celery Task {task_id} status: {status}, Response: {response}")
    return jsonify(response)


@app.get("/api/stats/upload")
@jwt_required()
def upload_stats():
    # Get stats for the last 30 days (including today)
    sql = """
    SELECT DATE(created_at) as d, COUNT(*) as c
    FROM novel
    WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL 29 DAY)
    GROUP BY d
    ORDER BY d;
    """
    try:
        rows = _db.session.execute(sql_text(sql)).fetchall()
        # Convert to list of dicts {date: YYYY-MM-DD, count: N}
        return jsonify([{"date": str(r[0]), "count": r[1]} for r in rows])
    except Exception as e:
        current_app.logger.error(f"Error fetching upload stats: {e}", exc_info=True)
        _db.session.rollback()
        return jsonify(error="Database error fetching upload stats"), 500


@app.get("/api/stats/genre")
@jwt_required()
def genre_stats():
    sql = "SELECT tags, COUNT(*) as c FROM novel GROUP BY tags;"
    try:
        rows = _db.session.execute(sql_text(sql)).fetchall()
        tag_counts = {}
        for tags_str, count in rows:
            # Use the first tag as the primary genre, handle empty/None tags
            primary_tag = (tags_str.split("|")[0].strip() if tags_str else "") or "未知"
            tag_counts[primary_tag] = tag_counts.get(primary_tag, 0) + count
        # Convert to list of dicts {name: genre, value: count} for charting
        return jsonify(
            [{"name": tag, "value": value} for tag, value in tag_counts.items()]
        )
    except Exception as e:
        current_app.logger.error(f"Error fetching genre stats: {e}", exc_info=True)
        _db.session.rollback()
        return jsonify(error="Database error fetching genre stats"), 500


@app.get("/api/stats/wordcloud/<int:novel_id>")
@jwt_required()
def wordcloud_img(novel_id):
    wordcloud_dir = current_app.config.get("WORDCLOUD_SAVE_PATH")
    if not wordcloud_dir:
        return jsonify(error="Server configuration error"), 500

    # Construct expected filename and perform basic security check
    safe_filename = f"wordcloud_{novel_id}.png"
    path = os.path.abspath(os.path.join(wordcloud_dir, safe_filename))

    # Prevent directory traversal
    if not path.startswith(os.path.abspath(wordcloud_dir)):
        return jsonify(error="Invalid file path"), 400

    if os.path.exists(path) and os.path.isfile(path):
        try:
            return send_file(path, mimetype="image/png")
        except Exception as send_err:
            current_app.logger.error(
                f"Error sending file {path}: {send_err}", exc_info=True
            )
            return jsonify(error="Error sending file"), 500
    else:
        # Provide more context in the 404 message
        novel_exists = Novel.query.get(novel_id) is not None
        error_msg = (
            "Wordcloud image not found. Analysis may not have completed or failed."
            if novel_exists
            else f"Novel with ID {novel_id} not found."
        )
        return jsonify(error=error_msg), 404


@app.route("/api/novels/<int:novel_id>/download", methods=["GET"])
@jwt_required()
def download_novel_file(novel_id):
    logger = current_app.logger
    try:
        novel = Novel.query.get(novel_id)
        if not novel:
            abort(404, description="Novel not found")
        if not novel.title:
            abort(404, description="Cannot determine filename (novel missing title)")

        # Get base path and format from config
        save_path_base = current_app.config.get("NOVEL_SAVE_PATH")
        novel_format = current_app.config.get(
            "NOVEL_NOVEL_FORMAT", "epub"
        )  # Default to epub
        if not save_path_base:
            abort(500, description="Server configuration error: Save path not set.")

        # Construct filename and full path
        safe_book_name = re.sub(r'[\\/*?:"<>|]', "_", novel.title)
        filename = f"{safe_book_name}.{novel_format}"
        full_path = os.path.abspath(os.path.join(save_path_base, filename))
        abs_save_path_base = os.path.abspath(save_path_base)

        # Security check: ensure the path is within the intended directory
        if not full_path.startswith(abs_save_path_base):
            abort(400, description="Invalid file path requested.")

        if os.path.exists(full_path) and os.path.isfile(full_path):
            mime_type = (
                "application/epub+zip" if novel_format == "epub" else "text/plain"
            )
            logger.info(f"Sending file: {filename} (MIME: {mime_type})")
            return send_file(
                full_path,
                mimetype=mime_type,
                as_attachment=True,
                download_name=filename,  # Suggest filename to browser
            )
        else:
            # Check task status for better error message
            task = (
                DownloadTask.query.filter_by(novel_id=novel_id)
                .order_by(DownloadTask.created_at.desc())
                .first()
            )
            status_msg = (
                f"Task status: {task.status.name}."
                if task
                else "No download task found."
            )
            description = f"Generated novel file ({filename}) not found. {status_msg} Processing might be incomplete or failed."
            abort(404, description=description)

    except HTTPException as e:
        # Log and return the specific HTTP error
        logger.warning(
            f"HTTP Exception during download for novel {novel_id}: {e.code} - {e.description}"
        )
        return jsonify(error=e.description), e.code
    except Exception as e:
        logger.error(
            f"Error during novel file download for ID {novel_id}: {e}", exc_info=True
        )
        abort(500, description="An error occurred while trying to serve the file.")


# === Error Handling ===
@app.errorhandler(404)
def not_found_error(error):
    app.logger.warning(
        f"Not Found (404): {request.path} - {getattr(error, 'description', 'No description')}"
    )
    return jsonify(error=getattr(error, "description", "Resource not found")), 404


@app.errorhandler(500)
def internal_error(error):
    app.logger.error(
        f"Internal Server Error (500): {request.path} - {error}", exc_info=True
    )
    # Attempt to rollback DB session in case of error during commit
    try:
        _db.session.rollback()
    except Exception as rb_err:
        app.logger.error(f"Error rolling back session during 500 handler: {rb_err}")
    return jsonify(error="Internal server error occurred"), 500


@app.errorhandler(Exception)
def unhandled_exception(e):
    # If it's an HTTPException (like 404, 400, etc.), let its handler work
    if isinstance(e, HTTPException):
        return e

    # Handle unexpected errors
    app.logger.error(f"Unhandled exception caught: {request.path} - {e}", exc_info=True)
    # Attempt rollback for safety
    try:
        _db.session.rollback()
    except Exception as rb_err:
        app.logger.error(
            f"Error rolling back session during unhandled exception handler: {rb_err}"
        )
    return jsonify(error="An unexpected error occurred"), 500


# --- Main Execution ---
if __name__ == "__main__":
    app.logger.info("Starting Flask application directly (intended for development)")
    host = os.getenv("FLASK_RUN_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_RUN_PORT", 5000))
    debug_mode = os.getenv("FLASK_ENV") == "development"
    app.logger.info(f"Running on http://{host}:{port}/ with debug={debug_mode}")
    # Use SocketIO to run the app for WebSocket support
    socketio.run(app, host=host, port=port, debug=debug_mode, use_reloader=debug_mode)
