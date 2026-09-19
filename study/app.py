"""Single-worker Railway service with account and book scoped RAG."""

import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, abort, g, jsonify, request, send_file, send_from_directory, session, stream_with_context
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from .compaction import (
    compaction_circuit_open, compact_conversation, context_usage,
    record_compaction_result, should_compact,
)
from .database import BUILTIN_OWNER, Database, public_book, public_message
from .documents import FORMATS, parse_document, split_sections
from .rag import EmbeddingClient, index_tokens, retrieve, semantic_sentence_ranges, terms
from .reader import register_reader_routes
from .tutor import MODES, Tutor, TutorError
from .websearch import WebSearchClient, WebSearchError

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)
LOG = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def uid():
    return uuid.uuid4().hex


def search_step_text(step):
    """One-line description of a search_book / web_search / draw_diagram tool
    call, shared by the SSE status event, the per-call log row and the
    persisted step list."""
    label = step["query"] or "无效请求"
    if step.get("diagram"):
        prefix = "生成图解"
        outcome = "调用被拒绝" if step.get("error") else f"生成 {step['count']} 张"
    elif step.get("web"):
        prefix = "联网检索"
        outcome = "调用被拒绝" if step.get("error") else f"命中 {step['count']} 条来源"
    else:
        prefix = "自动检索" if step.get("auto") else "检索"
        outcome = "调用被拒绝" if step.get("error") else f"命中 {step['count']} 段"
    return f"{prefix}「{label}」· {outcome}"


def create_app(test_config=None):
    app = Flask(__name__, static_folder=None)
    hosted = bool(os.getenv("RAILWAY_ENVIRONMENT_ID") or os.getenv("RAILWAY_PROJECT_ID"))
    root = Path(os.getenv("STUDY_DATA_DIR") or ROOT / ".study-data").resolve()
    app.config.update(
        DATA_ROOT=root, SECRET_KEY=os.getenv("STUDY_SECRET_KEY", ""),
        MAX_CONTENT_LENGTH=21 * 1024 * 1024,
        MAX_FORM_MEMORY_SIZE=256 * 1024, MAX_FORM_PARTS=5,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        SESSION_REFRESH_EACH_REQUEST=False,
        SESSION_COOKIE_SECURE=os.getenv("STUDY_COOKIE_SECURE", "1" if hosted else "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
        REGISTRATION_OPEN=os.getenv("STUDY_REGISTRATION_OPEN", "1") == "1",
        TEST_CODES=tuple(code for code in (part.strip().upper() for part
                                           in re.split(r"[,\s]+", os.getenv("STUDY_TEST_CODES", "")))
                         if 6 <= len(code) <= 64),
        MAX_USERS=int(os.getenv("STUDY_MAX_USERS", "100")),
        MAX_BOOKS=int(os.getenv("STUDY_MAX_BOOKS", "20")),
    )
    if test_config:
        app.config.update(test_config)
    root = Path(app.config["DATA_ROOT"])
    root.mkdir(parents=True, exist_ok=True)
    if not app.config["SECRET_KEY"]:
        if hosted:
            raise RuntimeError("Set STUDY_SECRET_KEY to a persistent random secret before deployment")
        secret_path = root / ".session-secret"
        try:
            with secret_path.open("x", encoding="utf-8") as file:
                file.write(secrets.token_hex(32))
        except FileExistsError:
            pass
        app.config["SECRET_KEY"] = secret_path.read_text(encoding="utf-8").strip()
    if len(app.config["SECRET_KEY"]) < 32:
        raise RuntimeError("STUDY_SECRET_KEY must contain at least 32 characters")
    if hosted:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    database = Database(root)
    embedder, tutor, searcher = EmbeddingClient(), Tutor(), WebSearchClient()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="book-index")
    index_slots = threading.BoundedSemaphore(4)
    model_slots = threading.BoundedSemaphore(2)
    guard = threading.Lock()
    book_locks, user_locks = {}, {}
    # Per-conversation compaction breaker state (in-memory; process-local).
    compact_circuits = {}
    rate_buckets = defaultdict(deque)
    app.extensions.update(database=database, embedder=embedder, tutor=tutor, web_search=searcher,
                          index_executor=executor)

    def lock_for(mapping, key):
        with guard:
            return mapping.setdefault(key, threading.Lock())

    def throttle(key, limit, window=600):
        stamp = time.monotonic()
        with guard:
            # Bound process-local abuse tracking even under rotating clients.
            if len(rate_buckets) > 5000:
                for old_key in list(rate_buckets):
                    if not rate_buckets[old_key] or rate_buckets[old_key][-1] < stamp - 3600:
                        del rate_buckets[old_key]
            if len(rate_buckets) > 5000 and key not in rate_buckets:
                abort(429, description="服务暂时繁忙，请稍后再试。")
            bucket = rate_buckets[key]
            while bucket and bucket[0] < stamp - window:
                bucket.popleft()
            if len(bucket) >= limit:
                abort(429, description="操作过于频繁，请稍后再试。")
            bucket.append(stamp)

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    LOG_RETENTION = timedelta(days=3)

    def log_cutoff():
        return (datetime.now(timezone.utc) - LOG_RETENTION).isoformat(timespec="microseconds")

    def add_log(event, level="info", detail="", owner_id=""):
        # Activity log for the in-app settings panel; rows auto-expire after 3 days.
        try:
            with database.connect() as db:
                db.execute("DELETE FROM app_logs WHERE created_at<?", (log_cutoff(),))
                db.execute("INSERT INTO app_logs(created_at,owner_id,level,event,detail) VALUES(?,?,?,?,?)",
                           (now(), owner_id, level, event, str(detail)[:2000]))
        except Exception:
            LOG.exception("Failed to write app log")

    BETA_ACCOUNTS = ("beta01", "beta02", "beta03", "beta04", "beta05")

    def seed_test_codes():
        """Seed test codes and retire the shared beta accounts.

        Codes come from STUDY_TEST_CODES (comma/space separated). INSERT OR
        IGNORE preserves bindings from earlier boots, so the env var only
        ever adds codes. The fixed beta accounts retire together with the
        switch to one-code-one-account registration; their books, builtin
        book conversations, messages and logs are removed."""
        if not app.config["TEST_CODES"]:
            return
        retired = []
        with database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for code in app.config["TEST_CODES"]:
                db.execute("INSERT OR IGNORE INTO test_codes(code,created_at) VALUES(?,?)", (code, now()))
            for name in BETA_ACCOUNTS:
                row = db.execute("SELECT id FROM users WHERE username_key=?", (name,)).fetchone()
                if row is None:
                    continue
                # Builtin-book conversations, then owned books (cascading to
                # their conversations and messages), logs, the user itself.
                db.execute("DELETE FROM conversations WHERE owner_id=?", (row["id"],))
                db.execute("DELETE FROM books WHERE owner_id=?", (row["id"],))
                db.execute("DELETE FROM app_logs WHERE owner_id=?", (row["id"],))
                db.execute("DELETE FROM users WHERE id=?", (row["id"],))
                retired.append(name)
        if retired:
            add_log("beta_retired", detail="内测共享账户已退役，改用一码一户测试码：" + "、".join(retired))

    seed_test_codes()

    def body():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            abort(400, description="请求必须包含 JSON 对象。")
        return value

    def book_row(db, book_id):
        row = db.execute("SELECT * FROM books WHERE id=? AND owner_id IN (?, ?)",
                         (book_id, g.user["id"], BUILTIN_OWNER)).fetchone()
        if row is None:
            abort(404, description="教材不存在或不可访问。")
        return row

    def require_own_book(row):
        # Builtin textbooks belong to the deployment, not to any account.
        if row["owner_id"] == BUILTIN_OWNER:
            abort(403, description="内置教材由部署者统一维护，不能删除、重建索引或下载原文件。")

    def conversation_row(db, book_id, conversation_id):
        row = db.execute("SELECT * FROM conversations WHERE id=? AND book_id=? AND owner_id=?",
                         (conversation_id, book_id, g.user["id"])).fetchone()
        if row is None:
            abort(404, description="会话不存在或不属于当前教材。")
        return row

    @app.before_request
    def protect():
        if not request.path.startswith("/api/"):
            return None
        g.user = None
        user_id = session.get("user_id")
        if user_id:
            with database.connect() as db:
                row = db.execute("SELECT id,username,api_key FROM users WHERE id=?", (user_id,)).fetchone()
                g.user = dict(row) if row else None
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            expected, actual = session.get("csrf", ""), request.headers.get("X-CSRF-Token", "")
            if not expected or not hmac.compare_digest(expected.encode(), actual.encode()):
                abort(403, description="页面安全凭证已过期，请刷新后重试。")
        public = {"/api/auth/me", "/api/auth/login", "/api/auth/register"}
        if request.path not in public and not g.user:
            abort(401, description="请先登录。")
        if request.path != "/api/books" and request.content_length and request.content_length > 32768:
            abort(413, description="请求内容过长。")
        return None

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(error=str(exc.description)), exc.code

    @app.errorhandler(Exception)
    def unexpected_error(exc):
        LOG.exception("Request failed")
        owner = g.user["id"] if getattr(g, "user", None) else ""
        add_log("request_error", level="error",
                detail=f"{request.method} {request.path} · {type(exc).__name__}: {exc}", owner_id=owner)
        return jsonify(error="服务处理失败，请稍后重试。"), 500

    @app.get("/health")
    def health():
        with database.connect() as db:
            db.execute("SELECT 1").fetchone()
        return jsonify(status="ok")

    @app.get("/")
    def index():
        response = send_from_directory(ROOT / "web", "index.html")
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/assets/<path:name>")
    def assets(name):
        if name not in {"app.js", "reader.js", "styles.css", "mermaid.min.js"}:
            abort(404)
        response = send_from_directory(ROOT / "web", name)
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/api/auth/me")
    def me():
        # test_code_registration reflects unbound codes in the database, so
        # the register entry hides itself again once every code is used up.
        with database.connect() as db:
            open_codes = db.execute("SELECT count(*) FROM test_codes WHERE bound_username=''").fetchone()[0]
        user = None
        if g.user:
            user = {"id": g.user["id"], "username": g.user["username"]}
        return jsonify(user=user, csrf_token=csrf_token(), registration_open=app.config["REGISTRATION_OPEN"],
                       test_code_registration=bool(open_codes),
                       api_key_set=bool(g.user and g.user.get("api_key")))

    def auth_response(user):
        session.clear()
        session["user_id"] = user["id"]
        session.permanent = True
        return jsonify(user={"id": user["id"], "username": user["username"]}, csrf_token=csrf_token())

    def credentials(data):
        username, password = data.get("username"), data.get("password")
        if not isinstance(username, str) or not re.fullmatch(r"[\w\u4e00-\u9fff]{3,32}", username.strip()):
            abort(400, description="用户名需为 3–32 位中文、字母、数字或下划线。")
        if not isinstance(password, str) or not 10 <= len(password) <= 256:
            abort(400, description="密码长度须为 10–256 个字符。")
        return username.strip(), password

    @app.post("/api/auth/register")
    def register():
        throttle(("register", request.remote_addr), 5, 3600)
        data = body()
        supplied_code = data.get("test_code")
        test_code = supplied_code.strip().upper() if isinstance(supplied_code, str) else ""
        api_key = str(data.get("api_key", "") or "").strip()
        if not test_code:
            # No test code: open registration requires the user's own API key,
            # so their model calls run on their key instead of the site's.
            if not app.config["REGISTRATION_OPEN"]:
                abort(403, description="暂未开放无测试码注册，请联系部署者。")
            if not 8 <= len(api_key) <= 400 or re.search(r"\s", api_key):
                abort(403, description="无测试码注册需填写有效的 API Key（8–400 个字符，不含空格），问答将使用你自己的密钥。")
        elif api_key and (not 8 <= len(api_key) <= 400 or re.search(r"\s", api_key)):
            abort(400, description="API Key 需为 8–400 个字符且不含空格。")
        username, password = credentials(data)
        user = {"id": uid(), "username": username}
        password_hash = generate_password_hash(password, method="pbkdf2:sha256:600000")
        try:
            with database.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if test_code:
                    # One code binds one account permanently; the check and
                    # the bind share this write transaction, so concurrent
                    # requests cannot double-spend a code.
                    row = db.execute("SELECT bound_username FROM test_codes WHERE code=?", (test_code,)).fetchone()
                    if row is None:
                        abort(403, description="测试码不正确。")
                    if row["bound_username"]:
                        abort(403, description="测试码已被使用。")
                if db.execute("SELECT count(*) FROM users").fetchone()[0] >= app.config["MAX_USERS"]:
                    abort(403, description="注册名额已满，请联系部署者。")
                db.execute("INSERT INTO users VALUES (?,?,?,?,?,?)",
                           (user["id"], username, username.casefold(), password_hash, now(), api_key))
                if test_code:
                    db.execute("UPDATE test_codes SET bound_username=?,bound_user_id=?,bound_at=? WHERE code=?",
                               (username, user["id"], now(), test_code))
        except sqlite3.IntegrityError:
            abort(409, description="该用户名不可用，请换一个。")
        if test_code:
            add_log("test_code_bound", detail=f"测试码绑定新账户「{username}」")
        elif api_key:
            add_log("api_key_registered", detail=f"账户「{username}」使用个人 API Key 注册")
        return auth_response(user)

    @app.post("/api/account/api-key")
    def set_api_key():
        # Update or clear the account's BYO key; empty string clears it.
        data = body()
        raw = data.get("api_key")
        api_key = raw.strip() if isinstance(raw, str) else None
        if api_key is None or len(api_key) > 400 or re.search(r"\s", api_key) or \
                (api_key and len(api_key) < 8):
            abort(400, description="API Key 需为 8–400 个字符且不含空格；留空表示清除。")
        with database.connect() as db:
            db.execute("UPDATE users SET api_key=? WHERE id=?", (api_key, g.user["id"]))
        add_log("api_key_updated", detail=f"账户「{g.user['username']}」{'清除' if not api_key else '更新'}了个人 API Key")
        return jsonify(ok=True, api_key_set=bool(api_key))

    @app.post("/api/auth/login")
    def login():
        throttle(("login", request.remote_addr), 15)
        username, password = credentials(body())
        throttle(("login-name", username.casefold()), 20)
        with database.connect() as db:
            row = db.execute("SELECT * FROM users WHERE username_key=?", (username.casefold(),)).fetchone()
        if row is None or not check_password_hash(row["password_hash"], password):
            add_log("login_failed", level="warning", detail=f"用户名「{username}」登录失败（用户名不存在或密码不正确）")
            abort(401, description="用户名或密码不正确。")
        return auth_response(row)

    @app.post("/api/auth/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/config")
    def config():
        return jsonify(llm_configured=tutor.configured, embedding_configured=embedder.configured,
                       embedding_backend="cloud" if embedder.configured else "lexical+fts5",
                       web_search_configured=searcher.configured,
                       max_upload_mb=20, formats=sorted(FORMATS))

    @app.get("/api/logs")
    def logs():
        # login_failed rows carry no owner and contain no secrets, so they stay visible;
        # builtin rows are system-level indexing events, useful to every account.
        with database.connect() as db:
            db.execute("DELETE FROM app_logs WHERE created_at<?", (log_cutoff(),))
            rows = db.execute(
                "SELECT id,created_at,level,event,detail FROM app_logs "
                "WHERE owner_id IN (?, ?) OR event='login_failed' ORDER BY id DESC LIMIT 200",
                (g.user["id"], BUILTIN_OWNER)).fetchall()
        return jsonify(logs=[dict(row) for row in rows])

    def index_book(owner_id, book_id, source, filename, book_lock):
        book_title = filename
        try:
            with database.connect() as db:
                row = db.execute("SELECT title FROM books WHERE id=? AND owner_id=?", (book_id, owner_id)).fetchone()
                if row:
                    book_title = row["title"]
                db.execute("UPDATE books SET status='indexing',error='' WHERE id=? AND owner_id=?", (book_id, owner_id))
            chunks = split_sections(parse_document(Path(source), filename))
            vectors, space, backend = [], None, "lexical+fts5"
            if embedder.configured:
                try:
                    vectors, space = embedder.embed_texts([chunk["text"] for chunk in chunks])
                    backend = "vector+lexical+fts5"
                except ValueError:
                    LOG.warning("Embedding unavailable during indexing; using lexical fallback")
                    add_log("index_embed_fallback", level="warning",
                            detail=f"《{book_title}》索引时向量化服务不可用，已降级为关键词检索", owner_id=owner_id)
            with database.connect() as db:
                db.execute("DELETE FROM chunks WHERE owner_id=? AND book_id=?", (owner_id, book_id))
                for index, chunk in enumerate(chunks):
                    vector = json.dumps(vectors[index], separators=(",", ":")) if vectors else None
                    cursor = db.execute(
                        "INSERT INTO chunks(owner_id,book_id,ordinal,section,page,text,embedding,embedding_space) VALUES(?,?,?,?,?,?,?,?)",
                        (owner_id, book_id, chunk["ordinal"], chunk["section"], chunk["page"], chunk["text"], vector, space))
                    db.execute("INSERT INTO chunks_fts(rowid,tokens) VALUES(?,?)", (cursor.lastrowid, index_tokens(chunk["text"])))
                db.execute("UPDATE books SET status='ready',error='',chunk_count=?,section_count=?,index_backend=? WHERE id=? AND owner_id=?",
                           (len(chunks), len({chunk["section"] for chunk in chunks}), backend, book_id, owner_id))
            add_log("index_ready", detail=(
                f"《{book_title}》索引完成 · {len(chunks)} 段 · "
                f"{'语义向量已启用' if vectors else '语义向量未启用（关键词检索）'} · {backend}"), owner_id=owner_id)
        except Exception as exc:
            LOG.exception("Book indexing failed")
            error = str(exc) if isinstance(exc, ValueError) else "索引失败，请稍后重新索引或检查文件内容。"
            with database.connect() as db:
                db.execute("UPDATE books SET status='error',error=? WHERE id=? AND owner_id=?", (error[:500], book_id, owner_id))
            add_log("index_error", level="error", detail=f"《{book_title}》索引失败 · {error[:300]}", owner_id=owner_id)
        finally:
            book_lock.release()
            index_slots.release()

    @app.get("/api/books")
    def books():
        with database.connect() as db:
            rows = db.execute("SELECT * FROM books WHERE owner_id IN (?, ?) ORDER BY created_at DESC",
                              (g.user["id"], BUILTIN_OWNER)).fetchall()
        return jsonify(books=[public_book(row) for row in rows])

    @app.post("/api/books")
    def upload():
        throttle(("upload", g.user["id"]), 20, 3600)
        file = request.files.get("file")
        if not file or not file.filename:
            abort(400, description="请选择教材文件。")
        filename = file.filename.replace("\\", "/").split("/")[-1]
        if re.search(r"[\x00-\x1f\x7f]", filename) or len(filename) > 180 or Path(filename).suffix.lower() not in FORMATS:
            abort(400, description="仅支持 Markdown、TXT 和 DOCX；文件名不能含控制字符且不超过 180 字符。")
        title = (request.form.get("title") or Path(filename).stem).strip()[:120]
        if not title:
            abort(400, description="请输入教材名称。")
        owner = g.user["id"]
        account_lock = lock_for(user_locks, owner)
        if not account_lock.acquire(blocking=False):
            abort(409, description="当前账户有教材正在操作，请稍后重试。")
        queued, reserved, book_lock, source = False, False, None, None
        try:
            with database.connect() as db:
                count = db.execute("SELECT count(*) FROM books WHERE owner_id=?", (owner,)).fetchone()[0]
                if count >= app.config["MAX_BOOKS"]:
                    abort(400, description="教材数量已达账户上限，请先删除不用的教材。")
            folder = root / "sources" / owner
            folder.mkdir(parents=True, exist_ok=True)
            if sum(p.stat().st_size for p in folder.iterdir() if p.is_file()) >= 50 * 1024 * 1024:
                abort(400, description="账户教材原文件已达到 50 MB 上限，请先清理。")
            if not index_slots.acquire(blocking=False):
                abort(429, description="索引队列已满，请稍后上传。")
            reserved = True
            book_id = uid()
            source = folder / (book_id + Path(filename).suffix.lower())
            size = 0
            with source.open("xb") as output:
                while block := file.stream.read(64 * 1024):
                    size += len(block)
                    if size > 20 * 1024 * 1024:
                        abort(413, description="单本教材不得超过 20 MB，请按卷拆分。")
                    output.write(block)
            if size == 0:
                abort(400, description="文件为空。")
            if sum(p.stat().st_size for p in folder.iterdir() if p.is_file()) > 50 * 1024 * 1024:
                abort(400, description="上传后超出账户 50 MB 原文件配额，请拆分或清理。")
            book_lock = lock_for(book_locks, book_id)
            book_lock.acquire()
            with database.connect() as db:
                db.execute("INSERT INTO books(id,owner_id,title,filename,source_path,created_at) VALUES(?,?,?,?,?,?)",
                           (book_id, owner, title, filename, str(source.relative_to(root)), now()))
                row = db.execute("SELECT * FROM books WHERE id=? AND owner_id=?", (book_id, owner)).fetchone()
            executor.submit(index_book, owner, book_id, source, filename, book_lock)
            queued = True
            return jsonify(book=public_book(row)), 202
        finally:
            if not queued:
                if source:
                    source.unlink(missing_ok=True)
                if book_lock:
                    book_lock.release()
                if reserved:
                    index_slots.release()
            account_lock.release()

    @app.get("/api/books/<book_id>")
    def book_detail(book_id):
        with database.connect() as db:
            row = book_row(db, book_id)
            sections = db.execute("SELECT section AS name,count(*) AS chunk_count FROM chunks "
                                  "WHERE owner_id IN (?, ?) AND book_id=? GROUP BY section ORDER BY min(ordinal)",
                                  (g.user["id"], BUILTIN_OWNER, book_id)).fetchall()
        return jsonify(book=public_book(row), sections=[dict(s) for s in sections])

    @app.delete("/api/books/<book_id>")
    def delete_book(book_id):
        with database.connect() as db:
            row = book_row(db, book_id)
            require_own_book(row)
        book_lock = lock_for(book_locks, book_id)
        if not book_lock.acquire(blocking=False):
            abort(409, description="教材正在索引或回答问题，请稍后删除。")
        try:
            with database.connect() as db:
                book_row(db, book_id)
                db.execute("DELETE FROM books WHERE id=? AND owner_id=?", (book_id, g.user["id"]))
            (root / row["source_path"]).unlink(missing_ok=True)
        finally:
            book_lock.release()
        return jsonify(ok=True)

    @app.post("/api/books/<book_id>/reindex")
    def reindex(book_id):
        throttle(("reindex", g.user["id"]), 10, 3600)
        with database.connect() as db:
            row = book_row(db, book_id)
            require_own_book(row)
        book_lock = lock_for(book_locks, book_id)
        if not book_lock.acquire(blocking=False):
            abort(409, description="教材正在处理，请稍后重试。")
        if not index_slots.acquire(blocking=False):
            book_lock.release()
            abort(429, description="索引队列已满，请稍后重试。")
        try:
            with database.connect() as db:
                row = book_row(db, book_id)
                db.execute("UPDATE books SET status='queued',error='' WHERE id=? AND owner_id=?", (book_id, g.user["id"]))
                # Old citation identifiers must never refer to new chunks after reindexing.
                db.execute("DELETE FROM conversations WHERE book_id=? AND owner_id=?", (book_id, g.user["id"]))
                updated = db.execute("SELECT * FROM books WHERE id=? AND owner_id=?", (book_id, g.user["id"])).fetchone()
            executor.submit(index_book, g.user["id"], book_id, root / row["source_path"], row["filename"], book_lock)
        except Exception:
            book_lock.release()
            index_slots.release()
            raise
        return jsonify(book=public_book(updated)), 202

    @app.get("/api/books/<book_id>/source")
    def source_file(book_id):
        with database.connect() as db:
            row = book_row(db, book_id)
            require_own_book(row)
        path = root / row["source_path"]
        if not path.is_file():
            abort(404, description="教材原文件不存在，请重新上传。")
        return send_file(path, as_attachment=True, download_name=row["filename"], mimetype="text/plain")

    @app.get("/api/books/<book_id>/chunks/<int:chunk_id>")
    def chunk_detail(book_id, chunk_id):
        with database.connect() as db:
            book = book_row(db, book_id)
            if book["status"] != "ready":
                abort(409, description="教材尚未完成索引。")
            scope = (g.user["id"], BUILTIN_OWNER, book_id)
            row = db.execute("SELECT id,text,section,page,ordinal FROM chunks "
                             "WHERE id=? AND owner_id IN (?, ?) AND book_id=?",
                             (chunk_id, g.user["id"], BUILTIN_OWNER, book_id)).fetchone()
            if row is None:
                abort(404, description="引用不属于当前教材或已失效。")
            # Adjacent chunks let readers page through surrounding context
            # when a cited fragment cuts a case or argument in half.
            def neighbor(comparison, order):
                query = (f"SELECT id,ordinal FROM chunks WHERE owner_id IN (?, ?) AND book_id=? "
                         f"AND ordinal{comparison}? ORDER BY ordinal {order} LIMIT 1")
                return db.execute(query, scope + (row["ordinal"],)).fetchone()
            prev, nxt = neighbor("<", "DESC"), neighbor(">", "ASC")
            # A window of chunks around the citation lets the reference
            # panel read continuously by scrolling instead of paging.
            window = db.execute(
                "SELECT id,text,section,page,ordinal FROM chunks "
                "WHERE owner_id IN (?, ?) AND book_id=? "
                "AND ordinal BETWEEN ? AND ? ORDER BY ordinal",
                scope + (row["ordinal"] - 3, row["ordinal"] + 6)).fetchall()
        return jsonify(chunk=dict(row),
                       prev=dict(prev) if prev else None,
                       next=dict(nxt) if nxt else None,
                       window=[dict(item) for item in window])

    @app.get("/api/books/<book_id>/chunks")
    def chunk_range(book_id):
        # Streaming context feed for the scrollable reference panel.
        anchor = request.args.get("anchor", type=int)
        direction = request.args.get("direction")
        count = request.args.get("count", default=10, type=int)
        if anchor is None or direction not in {"before", "after"} or not 1 <= count <= 20:
            abort(400, description="参数无效。")
        with database.connect() as db:
            book = book_row(db, book_id)
            if book["status"] != "ready":
                abort(409, description="教材尚未完成索引。")
            scope = (g.user["id"], BUILTIN_OWNER, book_id)
            row = db.execute("SELECT ordinal FROM chunks "
                             "WHERE id=? AND owner_id IN (?, ?) AND book_id=?",
                             (anchor, g.user["id"], BUILTIN_OWNER, book_id)).fetchone()
            if row is None:
                abort(404, description="引用不属于当前教材或已失效。")
            comparison, order = ("<", "DESC") if direction == "before" else (">", "ASC")
            rows = db.execute(
                f"SELECT id,text,section,page,ordinal FROM chunks "
                f"WHERE owner_id IN (?, ?) AND book_id=? AND ordinal{comparison}? "
                f"ORDER BY ordinal {order} LIMIT ?",
                scope + (row["ordinal"], count)).fetchall()
        if direction == "before":
            rows.reverse()
        return jsonify(chunks=[dict(item) for item in rows])

    @app.post("/api/books/<book_id>/chunks/<int:chunk_id>/match")
    def chunk_match(book_id, chunk_id):
        data = body()
        question = data.get("question")
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 3000:
            abort(400, description="问题内容无效。")
        question = question.strip()
        throttle(("chunk-match", g.user["id"]), 120, 3600)
        with database.connect() as db:
            book = book_row(db, book_id)
            if book["status"] != "ready":
                abort(409, description="教材尚未完成索引。")
            row = db.execute("SELECT text FROM chunks "
                             "WHERE id=? AND owner_id IN (?, ?) AND book_id=?",
                             (chunk_id, g.user["id"], BUILTIN_OWNER, book_id)).fetchone()
        if row is None:
            abort(404, description="引用不属于当前教材或已失效。")
        # Sentence-level semantic evidence locating for the reference panel;
        # empty ranges simply keep the keyword highlighting as-is.
        return jsonify(ranges=semantic_sentence_ranges(embedder, question, row["text"]))

    @app.get("/api/books/<book_id>/conversations")
    def conversations(book_id):
        with database.connect() as db:
            book_row(db, book_id)
            rows = db.execute("SELECT id,title,created_at FROM conversations WHERE owner_id=? AND book_id=? ORDER BY created_at DESC",
                              (g.user["id"], book_id)).fetchall()
        return jsonify(conversations=[dict(row) for row in rows])

    @app.post("/api/books/<book_id>/conversations")
    def new_conversation(book_id):
        throttle(("new-conversation", g.user["id"]), 40, 3600)
        conversation = {"id": uid(), "title": "新的学习对话", "created_at": now()}
        with database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = book_row(db, book_id)
            if row["status"] != "ready":
                abort(409, description="请等待教材索引完成。")
            if db.execute("SELECT count(*) FROM conversations WHERE owner_id=? AND book_id=?", (g.user["id"], book_id)).fetchone()[0] >= 100:
                abort(400, description="本书已达到 100 个会话上限，请使用已有会话。")
            db.execute("INSERT INTO conversations(id,owner_id,book_id,title,created_at) VALUES(?,?,?,?,?)",
                       (conversation["id"], g.user["id"], book_id, conversation["title"], conversation["created_at"]))
        return jsonify(conversation=conversation), 201

    @app.delete("/api/books/<book_id>/conversations/<conversation_id>")
    def delete_conversation(book_id, conversation_id):
        with database.connect() as db:
            book = book_row(db, book_id)
            conversation_row(db, book_id, conversation_id)
        # Own books hold an exclusive lock while answering; a wait-free reject
        # avoids orphaning an in-flight answer mid-stream. Builtin books skip
        # the lock by design and revalidate before persisting instead.
        book_lock = None if book["owner_id"] == BUILTIN_OWNER else lock_for(book_locks, book_id)
        if book_lock is not None and not book_lock.acquire(blocking=False):
            abort(409, description="本书有请求正在处理，请稍后重试。")
        try:
            with database.connect() as db:
                row = conversation_row(db, book_id, conversation_id)
                # Messages cascade via FK; the chat stream revalidates the
                # conversation before persisting, so nothing dangles.
                db.execute("DELETE FROM conversations WHERE id=? AND book_id=? AND owner_id=?",
                           (conversation_id, book_id, g.user["id"]))
        finally:
            if book_lock is not None:
                book_lock.release()
        add_log("conversation_deleted", detail=f"删除对话「{row['title'][:40]}」", owner_id=g.user["id"])
        return jsonify(ok=True)

    @app.get("/api/books/<book_id>/conversations/<conversation_id>")
    def history(book_id, conversation_id):
        with database.connect() as db:
            book_row(db, book_id)
            conversation = conversation_row(db, book_id, conversation_id)
            rows = db.execute(
                "SELECT messages.*, feedback.rating AS feedback_rating FROM messages "
                "LEFT JOIN feedback ON feedback.message_id=messages.id "
                "WHERE messages.owner_id=? AND messages.book_id=? AND messages.conversation_id=? "
                "ORDER BY messages.created_at, messages.id",
                (g.user["id"], book_id, conversation_id)).fetchall()
        # Context meter: the un-compacted tail that counts toward the next
        # compression, plus the summary size already folded away.
        summary = conversation["summary"] or ""
        context = context_usage([dict(row) for row in rows], conversation["summary_mark"] or "")
        context["summary_chars"] = len(summary)
        return jsonify(conversation={key: conversation[key] for key in ("id", "title", "created_at")},
                       messages=[{**public_message(row), "feedback": row["feedback_rating"]} for row in rows],
                       context=context)

    @app.post("/api/books/<book_id>/conversations/<conversation_id>/messages/<message_id>/feedback")
    def rate_message(book_id, conversation_id, message_id):
        # Tester verdicts close the evaluation loop: one row per answer,
        # rating 1/-1, and 0 clears a misclick by deleting the row.
        # Dislikes may carry an optional reason (preset or free text).
        data = body()
        rating = data.get("rating")
        if rating not in (1, -1, 0):
            abort(400, description="评价内容无效。")
        raw_reason = data.get("reason")
        reason = raw_reason.strip()[:200] if isinstance(raw_reason, str) else ""
        if rating != -1:
            reason = ""
        throttle(("feedback", g.user["id"]), 200, 3600)
        with database.connect() as db:
            book_row(db, book_id)
            conversation_row(db, book_id, conversation_id)
            row = db.execute("SELECT role,created_at FROM messages WHERE id=? AND owner_id=? AND book_id=? AND conversation_id=?",
                             (message_id, g.user["id"], book_id, conversation_id)).fetchone()
            if row is None:
                abort(404, description="消息不存在或已删除。")
            if row["role"] != "assistant":
                abort(400, description="只能评价回答消息。")
            # The question that produced this answer feeds the log entry and archive.
            question = db.execute("SELECT content FROM messages WHERE owner_id=? AND book_id=? AND conversation_id=? "
                                  "AND role='user' AND created_at<=? ORDER BY created_at DESC, id DESC LIMIT 1",
                                  (g.user["id"], book_id, conversation_id, row["created_at"])).fetchone()
            question_text = (question["content"] if question else "")[:120]
        stamp = now()
        with database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if rating == 0:
                db.execute("DELETE FROM feedback WHERE message_id=? AND owner_id=?", (message_id, g.user["id"]))
            else:
                db.execute("INSERT INTO feedback(message_id,owner_id,book_id,conversation_id,rating,created_at,reason) "
                           "VALUES(?,?,?,?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET rating=excluded.rating, reason=excluded.reason",
                           (message_id, g.user["id"], book_id, conversation_id, rating, stamp, reason))
            # Every click lands in the permanent archive so 3-day log cleanup
            # never destroys the improvement dataset.
            db.execute("INSERT INTO feedback_archive(created_at,owner_id,book_id,message_id,rating,reason,question) "
                       "VALUES(?,?,?,?,?,?,?)",
                       (stamp, g.user["id"], book_id, message_id, rating, reason, question_text))
        if rating:
            detail = f"{'满意' if rating == 1 else '不满意'} · 问: {question_text[:60]}"
            if reason:
                detail += f" · 因: {reason[:60]}"
            add_log("answer_feedback", detail=detail, owner_id=g.user["id"])
        return jsonify(feedback=rating or None)

    def sse(payload: dict) -> str:
        return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

    @app.post("/api/books/<book_id>/conversations/<conversation_id>/messages")
    def chat(book_id, conversation_id):
        data = body()
        question, mode, section = data.get("message"), data.get("mode", "qa"), data.get("section")
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 3000:
            abort(400, description="请输入 1–3000 字的问题。")
        if not isinstance(mode, str) or mode not in MODES or (section is not None and not isinstance(section, str)):
            abort(400, description="学习模式或章节参数无效。")
        question = question.strip()
        # Opt-in web supplement: honoured only when the deployment configured
        # the search MCP and the agentic QA path (qa mode) will actually run.
        web_enabled = bool(data.get("web")) and searcher.configured and mode == "qa"
        owner = g.user["id"]
        with database.connect() as db:
            book = book_row(db, book_id)
            conversation_row(db, book_id, conversation_id)
            # Reject unknown sections eagerly: a 400 is cheaper than an SSE round trip,
            # and the frontend shows the same message either way.
            if section is not None and book["status"] == "ready" and db.execute(
                    "SELECT 1 FROM chunks WHERE owner_id IN (?, ?) AND book_id=? AND section=? LIMIT 1",
                    (owner, BUILTIN_OWNER, book_id, section)).fetchone() is None:
                abort(400, description="章节不存在或已失效，请重新选择。")
        throttle(("chat", owner), 60, 3600)
        # Builtin books are immutable for accounts (no reindex/delete), so chats
        # on them share nothing except the model slots; skip the exclusive lock.
        shared_book = book["owner_id"] == BUILTIN_OWNER
        book_lock = None if shared_book else lock_for(book_locks, book_id)
        if book_lock is not None and not book_lock.acquire(blocking=False):
            abort(409, description="本书已有请求正在处理，请稍后重试。")
        if not model_slots.acquire(blocking=False):
            if book_lock is not None:
                book_lock.release()
            abort(429, description="问答服务繁忙，请稍后重试。")

        def stream():
            try:
                yield sse({"type": "status", "stage": "retrieve", "text": "正在检索本书相关内容…"})
                with database.connect() as db:
                    book = book_row(db, book_id)
                    conversation = conversation_row(db, book_id, conversation_id)
                    # Rolling compaction summary of earlier turns rides along
                    # as untrusted context; empty until the conversation is long.
                    summary = conversation["summary"] or ""
                    summary_mark = conversation["summary_mark"] or ""
                    if book["status"] != "ready":
                        add_log("chat_error", level="error", detail=f"教材尚未完成索引 · 问: {question[:60]}", owner_id=owner)
                        yield sse({"type": "error", "error": "请等待教材完成索引。"})
                        return
                    old = db.execute("SELECT * FROM messages WHERE owner_id=? AND book_id=? AND conversation_id=? ORDER BY created_at DESC LIMIT 120",
                                     (owner, book_id, conversation_id)).fetchall()
                    if len(old) >= 120:
                        add_log("chat_error", level="error", detail=f"对话已满 120 条上限 · 问: {question[:60]}", owner_id=owner)
                        yield sse({"type": "error", "error": "当前对话已满，请新建学习对话。"})
                        return
                    # Scope filtering happens in SQL before any channel sees candidates.
                    sql, params = "SELECT * FROM chunks WHERE owner_id IN (?, ?) AND book_id=?", [owner, BUILTIN_OWNER, book_id]
                    if section is not None:
                        sql += " AND section=?"
                        params.append(section)
                    rows = db.execute(sql + " ORDER BY ordinal", params).fetchall()
                    if section is not None and not rows:
                        add_log("chat_error", level="error", detail=f"章节「{section}」不存在或已失效 · 问: {question[:60]}", owner_id=owner)
                        yield sse({"type": "error", "error": "章节不存在或已失效，请重新选择。"})
                        return
                    chunks = []
                    for row in rows:
                        chunk = dict(row)
                        chunk["embedding"] = json.loads(chunk["embedding"]) if chunk["embedding"] else None
                        chunks.append(chunk)
                    previous = [row["content"] for row in reversed(old) if row["role"] == "user"][-3:]
                    query = question
                    if re.search(r"继续|上面|刚才|它|这个|这一|再讲|举例", question) and previous:
                        query = previous[-1][:300] + " " + question

                    def fts_lookup(word_list):
                        """FTS candidates for any query text; scope filters stay in SQL."""
                        if not word_list:
                            return []
                        match = " OR ".join('"' + word.replace('"', '""') + '"' for word in word_list)
                        fts_sql = ("SELECT c.id FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid "
                                   "WHERE chunks_fts MATCH ? AND c.owner_id IN (?, ?) AND c.book_id=?")
                        fts_params = [match, owner, BUILTIN_OWNER, book_id]
                        if section is not None:
                            fts_sql += " AND c.section=?"
                            fts_params.append(section)
                        try:
                            with database.connect() as db:
                                return [row[0] for row in db.execute(
                                    fts_sql + " ORDER BY bm25(chunks_fts) LIMIT 24", fts_params)]
                        except sqlite3.OperationalError:
                            LOG.warning("FTS query unavailable; using lexical channel")
                            return []

                    search_terms = terms(query)
                streamed, answer = False, None
                user_key = g.user.get("api_key") or ""
                hit_count, retrieve_ms, overview = 0, 0, False
                retrieval = {}
                try:
                    generate_started = time.monotonic()
                    if mode == "qa":
                        # Agentic QA: the model drives retrieval itself through
                        # the search_book tool; classic retrieval is fallback.
                        agent_state = {"steps": [], "word_set": set(), "backend": "lexical+fts5",
                                       "degraded": True, "ms": 0}

                        def run_agent_search(agent_query, limit):
                            started = time.monotonic()
                            word_list = terms(agent_query)
                            agent_state["word_set"].update(word_list)
                            result = retrieve(agent_query, chunks, fts_lookup(word_list), embedder, limit=limit)
                            agent_state["backend"] = result["backend"]
                            agent_state["degraded"] = result["degraded"]
                            agent_state["ms"] += int((time.monotonic() - started) * 1000)
                            return result["hits"]

                        def run_web_search(web_query):
                            """One opt-in lookup on the deployment's search MCP;
                            failures become tool errors the model can route
                            around instead of killing the stream."""
                            try:
                                throttle(("web-search", owner), 40, 3600)
                            except HTTPException as exc:
                                raise WebSearchError(str(exc.description)) from exc
                            return searcher.search(web_query, 6)

                        yield sse({"type": "status", "stage": "generate",
                                   "text": "模型正在自主检索本书并核对引用…"})
                        try:
                            for kind, value in tutor.agent_stream(question, mode, book["title"], run_agent_search,
                                                                  previous, retrieval, user_key, summary,
                                                                  run_web_search if web_enabled else None):
                                if kind == "search":
                                    # Every tool call becomes a visible UI step,
                                    # a dedicated log row and persisted metadata.
                                    step = {"query": (value.get("query") or "")[:60],
                                            "count": int(value.get("count") or 0)}
                                    if value.get("diagram"):
                                        step["diagram"] = True
                                    if value.get("web"):
                                        step["web"] = True
                                    if value.get("auto"):
                                        step["auto"] = True
                                    if value.get("error"):
                                        step["error"] = True
                                    agent_state["steps"].append(step)
                                    add_log("diagram" if step.get("diagram") else
                                            "web_search" if step.get("web") else "agent_search",
                                            level="warning" if step.get("error") else "info",
                                            detail=f"{search_step_text(step)} · 问: {question[:40]}",
                                            owner_id=owner)
                                    yield sse({"type": "status", "stage": "search", "reset": True,
                                               "text": search_step_text(step), "search": step})
                                elif kind == "delta":
                                    streamed = True
                                    yield sse({"type": "delta", "text": value})
                                else:
                                    answer = value
                        except TutorError:
                            if streamed:
                                raise
                            # Provider rejected tools or the loop died before
                            # any visible text: retry with the classic pipeline.
                            LOG.warning("agent loop failed before streaming; using classic retrieval")
                            answer = None
                        if answer is not None:
                            hit_count = retrieval.get("evidence", len(answer.get("citations", [])))
                            retrieve_ms = agent_state["ms"]
                            web_steps = sum(1 for step in agent_state["steps"] if step.get("web"))
                            diagram_steps = sum(1 for step in agent_state["steps"] if step.get("diagram"))
                            retrieval.update({
                                "backend": agent_state["backend"], "degraded": agent_state["degraded"],
                                "scope": "agent-searches", "section": section,
                                "steps": agent_state["steps"],
                                "searches": len(agent_state["steps"]) - web_steps - diagram_steps,
                                "hits": hit_count, "retrieve_ms": retrieve_ms,
                                # Shipped with the answer so the evidence panel can highlight query hits.
                                "terms": sorted(agent_state["word_set"])[:24],
                            })
                            if web_steps:
                                retrieval["web_searches"] = web_steps
                            if diagram_steps:
                                retrieval["diagrams"] = diagram_steps
                            retrieval.pop("evidence", None)
                    if answer is None:
                        retrieve_started = time.monotonic()
                        result = retrieve(query, chunks, fts_lookup(search_terms), embedder)
                        retrieve_ms = int((time.monotonic() - retrieve_started) * 1000)
                        overview = mode in {"outline", "quiz", "explain"} and not search_terms
                        if overview and chunks:
                            # Evenly sampled excerpts are explicit, never called a full-book summary.
                            indices = sorted({round(i * (len(chunks) - 1) / min(5, len(chunks) - 1))
                                              for i in range(min(6, len(chunks)))}) if len(chunks) > 1 else [0]
                            result["hits"] = [chunks[index] for index in indices]
                        retrieval = {key: result[key] for key in ("backend", "degraded")}
                        retrieval["scope"] = "selected-excerpts" if overview else "retrieved-excerpts"
                        retrieval["section"] = section
                        hit_count = len(result["hits"])
                        retrieval["hits"] = hit_count
                        retrieval["retrieve_ms"] = retrieve_ms
                        retrieval["terms"] = search_terms[:24]
                        stage_text = (f"已定位 {hit_count} 段相关原文，正在核对引用并生成回答…" if hit_count
                                      else "未检索到直接相关的原文，正在整理回答…")
                        yield sse({"type": "status", "stage": "generate", "text": stage_text, "hits": hit_count})
                        if mode != "quiz":
                            try:
                                for kind, value in tutor.generate_stream(question, mode, book["title"], result["hits"],
                                                                         previous, retrieval, user_key, summary):
                                    if kind == "delta":
                                        streamed = True
                                        yield sse({"type": "delta", "text": value})
                                    else:
                                        answer = value
                            except TutorError:
                                if streamed:
                                    raise
                                # Streaming failed before any text arrived; retry one-shot.
                                answer = tutor.generate(question, mode, book["title"], result["hits"], previous, retrieval, user_key, summary)
                        else:
                            answer = tutor.generate(question, mode, book["title"], result["hits"], previous, retrieval, user_key, summary)
                except TutorError as exc:
                    add_log("chat_error", level="error",
                            detail=f"生成失败 · {str(exc)[:200]} · 问: {question[:60]}", owner_id=owner)
                    yield sse({"type": "error", "error": str(exc)})
                    return
                if answer is None:
                    add_log("chat_error", level="error", detail=f"生成失败 · 问: {question[:60]}", owner_id=owner)
                    yield sse({"type": "error", "error": "生成失败，请重试。"})
                    return
                retrieval["generate_ms"] = int((time.monotonic() - generate_started) * 1000)
                if overview and answer["grounded"]:
                    answer["notice"] = "本次按位置抽取最多 6 段原文辅助学习，不代表完整覆盖全书或本章。"
                user_message = {"id": uid(), "role": "user", "content": question, "mode": mode, "created_at": now()}
                answer.update(id=uid(), role="assistant", mode=mode, created_at=now())
                # Persist before delivering: if the client disconnected, the answer still lands.
                with database.connect() as db:
                    book_row(db, book_id)
                    conversation_row(db, book_id, conversation_id)
                    for message in (user_message, answer):
                        db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                                   (message["id"], owner, book_id, conversation_id, message["role"], message["content"], mode,
                                    json.dumps(message, ensure_ascii=False), message["created_at"]))
                    if not old:
                        db.execute("UPDATE conversations SET title=? WHERE id=? AND book_id=? AND owner_id=?",
                                   (question[:40], conversation_id, book_id, owner))
                channel = "语义向量已启用" if not retrieval["degraded"] else "语义向量不可用（关键词检索）"
                searches_note = f" · 自主检索 {retrieval['searches']} 次" if retrieval.get("searches") else ""
                web_note = f" · 联网 {retrieval['web_searches']} 次" if retrieval.get("web_searches") else ""
                add_log("chat", level="warning" if retrieval["degraded"] else "info",
                        detail=(f"《{book['title']}》· {channel} · 命中 {hit_count} 段{searches_note}{web_note} · "
                                f"检索 {retrieve_ms}ms · 生成 {retrieval.get('generate_ms', 0) / 1000:.1f}s · 问: {question[:60]}"),
                        owner_id=owner)
                yield sse({"type": "answer", "message": answer, "user_message": user_message})
                # Rolling compaction for long conversations: once the part not
                # covered by the stored summary passes a character budget, one
                # extra model call condenses the older turns. It runs after the
                # answer is delivered and can never fail this request.
                history, live_summary, live_mark = [], summary, summary_mark
                try:
                    with guard:
                        circuit = compact_circuits.setdefault(conversation_id, {})
                    history = [dict(row) for row in reversed(old)] + [user_message, answer]
                    if should_compact(history, summary_mark) and not compaction_circuit_open(circuit):
                        outcome = compact_conversation(
                            history, summary, lambda prompt_messages: tutor.summarize(prompt_messages, user_key))
                        if outcome:
                            new_summary, mark = outcome
                            with database.connect() as db:
                                db.execute("UPDATE conversations SET summary=?, summary_mark=? "
                                           "WHERE id=? AND book_id=? AND owner_id=?",
                                           (new_summary, mark, conversation_id, book_id, owner))
                            record_compaction_result(circuit, success=True)
                            add_log("compaction",
                                    detail=f"《{book['title']}》· 摘要 {len(new_summary)} 字 · 压缩 {len(history)} 条中较早部分",
                                    owner_id=owner)
                            live_summary, live_mark = new_summary, mark
                            yield sse({"type": "compacted", "summary_chars": len(new_summary)})
                        else:
                            record_compaction_result(circuit, success=False)
                            add_log("compaction", level="warning",
                                    detail=f"摘要为空或过长已跳过 · 问: {question[:60]}", owner_id=owner)
                except Exception:
                    with guard:
                        circuit = compact_circuits.setdefault(conversation_id, {})
                    record_compaction_result(circuit, success=False)
                    LOG.exception("conversation compaction failed; keeping conversation as-is")
                    add_log("compaction", level="warning",
                            detail=f"压缩失败，对话保持原样 · 问: {question[:60]}", owner_id=owner)
                # Context meter update: what the next turn will carry after
                # any compaction above. Emitted even without compaction so the
                # client's counter stays in step with every answer.
                usage = context_usage(history, live_mark)
                usage["summary_chars"] = len(live_summary)
                yield sse({"type": "context", "context": usage})
            finally:
                model_slots.release()
                if book_lock is not None:
                    book_lock.release()

        return Response(stream_with_context(stream()), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def seed_builtin_book(path: Path):
        title = path.stem[:120]
        with database.connect() as db:
            row = db.execute("SELECT * FROM books WHERE owner_id=? AND title=?",
                             (BUILTIN_OWNER, title)).fetchone()
        book_id = row["id"] if row is not None else uid()
        book_lock = lock_for(book_locks, book_id)
        # Dedicated seed thread may wait; a busy reader must not skip the update
        # until the next boot. HTTP handlers keep their nonblocking acquire.
        book_lock.acquire()
        queued, reserved = False, False
        try:
            # Source replacement and index changes share the reader's book lock.
            with database.connect() as db:
                row = db.execute("SELECT * FROM books WHERE id=? AND owner_id=?",
                                 (book_id, BUILTIN_OWNER)).fetchone()
                if row is not None and row["status"] == "ready":
                    source = root / row["source_path"]
                    if row["filename"] == path.name and source.is_file() and source.read_bytes() == path.read_bytes():
                        return
                if row is None:
                    folder = root / "sources" / BUILTIN_OWNER
                    folder.mkdir(parents=True, exist_ok=True)
                    source = folder / (book_id + path.suffix.lower())
                    shutil.copyfile(path, source)
                    db.execute("INSERT INTO books(id,owner_id,title,filename,source_path,created_at) VALUES(?,?,?,?,?,?)",
                               (book_id, BUILTIN_OWNER, title, path.name, str(source.relative_to(root)), now()))
                else:
                    source = root / row["source_path"]
                    if not source.is_file() or source.read_bytes() != path.read_bytes():
                        shutil.copyfile(path, source)
                    # Old citations must never refer to new chunks after a content update.
                    db.execute("DELETE FROM conversations WHERE book_id=?", (book_id,))
                db.execute("UPDATE books SET status='queued',error='',filename=? WHERE id=? AND owner_id=?",
                           (path.name, book_id, BUILTIN_OWNER))
            # The dedicated seed thread may wait without delaying web requests.
            index_slots.acquire()
            reserved = True
            LOG.info("Seeding builtin book: %s", title)
            executor.submit(index_book, BUILTIN_OWNER, book_id, source, path.name, book_lock)
            queued = True
        finally:
            if not queued:
                book_lock.release()
                if reserved:
                    index_slots.release()

    def seed_builtin_books():
        # Baked-in textbooks ship with the image; every account can read them.
        folder = ROOT / "builtin_books"
        if not folder.is_dir():
            return
        try:
            with database.connect() as db:
                db.execute("INSERT OR IGNORE INTO users(id,username,username_key,password_hash,created_at) VALUES(?,?,?,?,?)",
                           (BUILTIN_OWNER, "内置教材库", "builtin-library",
                            generate_password_hash(secrets.token_hex(32)), now()))
        except Exception:
            LOG.exception("Failed to create the builtin owner account")
            return
        for path in sorted(folder.iterdir()):
            if not path.is_file() or path.suffix.lower() not in FORMATS:
                continue
            try:
                seed_builtin_book(path)
            except Exception:
                LOG.exception("Builtin book seeding failed: %s", path.name)

    def refresh_feedback_stats():
        """Fold the permanent feedback_archive into fixed 3-day periods.

        Runs at startup and periodically; each closed period writes one
        feedback_stats row plus a system log entry every account can read.
        """
        period = timedelta(days=3)
        try:
            with database.connect() as db:
                first = db.execute("SELECT min(created_at) FROM feedback_archive").fetchone()[0]
                if not first:
                    return 0
                base = datetime.fromisoformat(first)
                last = db.execute("SELECT max(period_end) FROM feedback_stats").fetchone()[0]
                if last:
                    base = max(base, datetime.fromisoformat(last))
                written = 0
                while datetime.now(timezone.utc) - base >= period:
                    end = base + period
                    rows = db.execute("SELECT rating,reason,question FROM feedback_archive "
                                      "WHERE created_at>=? AND created_at<?", (base.isoformat(), end.isoformat())).fetchall()
                    reasons = defaultdict(int)
                    disliked = defaultdict(int)
                    for row in rows:
                        if row["rating"] == -1:
                            reasons[row["reason"] or "（未填原因）"] += 1
                            if row["question"]:
                                disliked[row["question"]] += 1
                    db.execute("INSERT OR REPLACE INTO feedback_stats "
                               "(period_start,period_end,up,down,clears,reasons,questions) VALUES(?,?,?,?,?,?,?)",
                               (base.isoformat(), end.isoformat(),
                                sum(1 for r in rows if r["rating"] == 1),
                                sum(1 for r in rows if r["rating"] == -1),
                                sum(1 for r in rows if r["rating"] == 0),
                                json.dumps([{"reason": k, "count": v} for k, v in
                                            sorted(reasons.items(), key=lambda kv: -kv[1])[:10]], ensure_ascii=False),
                                json.dumps([{"question": k, "count": v} for k, v in
                                            sorted(disliked.items(), key=lambda kv: -kv[1])[:10]], ensure_ascii=False)))
                    written += 1
                    base = end
            if written:
                up = down = clears = 0
                with database.connect() as db:
                    for row in db.execute("SELECT period_start,period_end,up,down,clears FROM feedback_stats "
                                          "ORDER BY period_end DESC LIMIT ?", (written,)):
                        up, down, clears = up + row["up"], down + row["down"], clears + row["clears"]
                        add_log("feedback_stats", owner_id=BUILTIN_OWNER,
                                detail=f"反馈统计（{row['period_start'][:10]} ~ {row['period_end'][:10]}）："
                                       f"满意 {row['up']} · 不满意 {row['down']} · 撤销 {row['clears']}")
            return written
        except Exception:
            LOG.exception("feedback stats refresh failed")
            return 0

    def feedback_stats_loop(stop):
        # Cheap check every 6 hours; periods close lazily so restarts never
        # lose data (a period is folded as soon as it is fully elapsed).
        while not stop.wait(21600):
            refresh_feedback_stats()

    register_reader_routes(app, database, root, book_row,
                           lambda book_id: lock_for(book_locks, book_id), body, throttle, now, uid)

    seeder = threading.Thread(target=seed_builtin_books, name="builtin-seed", daemon=True)
    seeder.start()
    # Exposed so tests can wait for background seeding before deleting the
    # data directory; on Windows an in-flight seed holds the sqlite file.
    app.extensions["seed_thread"] = seeder

    refresh_feedback_stats()
    stats_stop = threading.Event()
    stats = threading.Thread(target=feedback_stats_loop, args=(stats_stop,), name="feedback-stats", daemon=True)
    stats.start()
    app.extensions["stats_stop"] = stats_stop
    app.extensions["stats_thread"] = stats
    app.extensions["refresh_feedback_stats"] = refresh_feedback_stats

    return app
