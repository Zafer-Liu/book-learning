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
from .database import BUILTIN_OWNER, Database, public_book, public_message, public_reading_progress
from .documents import FORMATS, PARSER_VERSION, parse_document, split_sections
from .rag import EmbeddingClient, index_tokens, retrieve, semantic_sentence_ranges, terms
from .reader import register_reader_routes
from .tutor import MODES, Tutor, TutorError
from .websearch import WebSearchClient, WebSearchError

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)
LOG = logging.getLogger(__name__)

# Exact UI commands describe a scope task, not textbook search terms. The first
# entry is the canonical instruction used for generation and conversation history.
SCOPE_COMMANDS = {
    "explain": ("讲解当前范围", "开始讲解", "章节讲解"),
    "outline": ("梳理当前范围的要点", "生成要点", "要点梳理"),
    "quiz": ("针对当前范围出自测题", "针对当前范围出三道自测题", "生成自测", "自测练习"),
}


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
        # The deployer-only admin reports authenticate with their own key
        # instead of a session; the routes hide themselves (404) when the key
        # is unset or wrong.
        if request.path.startswith("/api/admin/"):
            expected = os.getenv("STUDY_ADMIN_KEY", "")
            supplied = request.headers.get("X-Admin-Key", "")
            if not expected or not hmac.compare_digest(expected.encode(), supplied.encode()):
                abort(404, description="不存在。")
            return None
        # Public surfaces: the guest demo (builtin books, IP-throttled) and
        # one-off share links carry no account session.
        if request.path.startswith("/api/demo/") or request.path.startswith("/api/share/"):
            return None
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
        if name not in {"app.js", "reader.js", "styles.css", "mermaid.min.js", "share.html"}:
            abort(404)
        response = send_from_directory(ROOT / "web", name)
        # Windows hosts may map .js/.css to text/plain via the registry, which
        # makes browsers refuse to execute the scripts; pin the MIME types.
        asset_mime = {".js": "text/javascript; charset=utf-8",
                      ".css": "text/css; charset=utf-8"}
        suffix = "." + name.rsplit(".", 1)[-1].lower()
        if suffix in asset_mime:
            response.headers["Content-Type"] = asset_mime[suffix]
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

    @app.get("/api/admin/test-codes")
    def admin_test_codes():
        # Deployer-only report on invite-code usage; guarded in protect() by
        # STUDY_ADMIN_KEY (X-Admin-Key header), never by a user session.
        with database.connect() as db:
            rows = db.execute("SELECT code,bound_username,bound_at FROM test_codes "
                              "ORDER BY bound_username='', bound_username, code").fetchall()
        codes = [{"code": row["code"], "bound": bool(row["bound_username"]),
                  "username": row["bound_username"] or None, "bound_at": row["bound_at"] or None}
                 for row in rows]
        bound = sum(1 for item in codes if item["bound"])
        return jsonify(total=len(codes), bound=bound, open=len(codes) - bound, codes=codes)

    # ------------------------------------------------------------------
    # Learning card export (Anki-compatible CSV) and share links
    # ------------------------------------------------------------------

    @app.get("/api/books/<book_id>/export/cards.csv")
    def export_cards(book_id):
        import csv
        import io
        with database.connect() as db:
            book = book_row(db, book_id)
            rows = db.execute("SELECT payload FROM messages WHERE owner_id=? AND book_id=? AND role='assistant' "
                              "ORDER BY created_at, id", (g.user["id"], book_id)).fetchall()
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["正面（问题）", "背面（答案）", "解析", "来源"])
        cards = 0
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (ValueError, TypeError):
                continue
            quiz = payload.get("quiz") if isinstance(payload, dict) else None
            if not isinstance(quiz, list):
                continue
            sections = {}
            for ref in payload.get("citations") or []:
                if isinstance(ref, dict) and ref.get("label"):
                    sections[ref["label"]] = ref.get("section") or ""
            for item in quiz:
                if not isinstance(item, dict) or not item.get("question"):
                    continue
                cites = "、".join(filter(None, (sections.get(label) for label in (item.get("citations") or []))))
                writer.writerow([item.get("question", ""), item.get("answer", ""), item.get("explanation", ""),
                                 book["title"] + (f"（{cites}）" if cites else "")])
                cards += 1
        if not cards:
            abort(404, description="本书还没有可导出的自测题；先用自测练习模式生成题目。")
        # UTF-8 BOM keeps Excel happy with Chinese text.
        return Response("\ufeff" + buffer.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename=cards-{book_id[:8]}.csv"})

    @app.post("/api/books/<book_id>/conversations/<conversation_id>/messages/<message_id>/share")
    def create_share(book_id, conversation_id, message_id):
        with database.connect() as db:
            book_row(db, book_id)
            conversation_row(db, book_id, conversation_id)
            row = db.execute("SELECT id,role,created_at FROM messages WHERE id=? AND owner_id=? AND book_id=? "
                             "AND conversation_id=?", (message_id, g.user["id"], book_id, conversation_id)).fetchone()
            if row is None:
                abort(404, description="消息不存在或已删除。")
            if row["role"] != "assistant":
                abort(400, description="只能分享回答消息。")
            existing = db.execute("SELECT token FROM shares WHERE message_id=? AND revoked_at IS NULL",
                                  (message_id,)).fetchone()
            if existing:
                return jsonify(url=f"/share/{existing['token']}", revoked=False)
            token = secrets.token_urlsafe(18)
            db.execute("INSERT INTO shares(token,owner_id,book_id,conversation_id,message_id,created_at) "
                       "VALUES(?,?,?,?,?,?)",
                       (token, g.user["id"], book_id, conversation_id, message_id, now()))
        add_log("share_created", detail=f"分享了一条回答 · 消息 {message_id[:8]}", owner_id=g.user["id"])
        return jsonify(url=f"/share/{token}", revoked=False), 201

    @app.delete("/api/books/<book_id>/conversations/<conversation_id>/messages/<message_id>/share")
    def revoke_share(book_id, conversation_id, message_id):
        with database.connect() as db:
            book_row(db, book_id)
            conversation_row(db, book_id, conversation_id)
            result = db.execute("UPDATE shares SET revoked_at=COALESCE(revoked_at,?) WHERE message_id=? AND owner_id=?",
                                (now(), message_id, g.user["id"])).rowcount
        if not result:
            abort(404, description="该消息没有生效中的分享链接。")
        return jsonify(ok=True)

    @app.get("/api/share/<token>")
    def share_payload(token):
        # Public, read-only, sanitized view of one shared answer.
        with database.connect() as db:
            share = db.execute("SELECT * FROM shares WHERE token=?", (token,)).fetchone()
            if share is None or share["revoked_at"]:
                abort(404, description="分享不存在或已撤销。")
            answer = db.execute("SELECT * FROM messages WHERE id=? AND book_id=? AND conversation_id=?",
                                (share["message_id"], share["book_id"], share["conversation_id"])).fetchone()
            if answer is None:
                abort(404, description="原回答已删除，分享失效。")
            book = db.execute("SELECT title FROM books WHERE id=?", (share["book_id"],)).fetchone()
            question = db.execute("SELECT content FROM messages WHERE owner_id=? AND book_id=? AND conversation_id=? "
                                  "AND role='user' AND created_at<=? ORDER BY created_at DESC, id DESC LIMIT 1",
                                  (share["owner_id"], share["book_id"], share["conversation_id"],
                                   answer["created_at"])).fetchone()
        payload = json.loads(answer["payload"])
        return jsonify(book_title=book["title"] if book else "",
                       question=question["content"] if question else "",
                       message={"paragraphs": payload.get("paragraphs") or [],
                                "quiz": payload.get("quiz") or [],
                                "diagrams": payload.get("diagrams") or [],
                                "citations": [ref for ref in (payload.get("citations") or [])
                                              if isinstance(ref, dict) and not ref.get("kind") == "web"]})

    @app.get("/share/<token>")
    def share_page(token):
        response = send_from_directory(ROOT / "web", "share.html")
        response.headers["Cache-Control"] = "no-cache"
        return response

    # ------------------------------------------------------------------
    # Guest demo: builtin books only, IP-throttled, nothing persisted
    # ------------------------------------------------------------------

    @app.get("/api/demo/books")
    def demo_books():
        with database.connect() as db:
            rows = db.execute("SELECT id,title,section_count,chunk_count FROM books "
                              "WHERE owner_id=? AND category='textbook' AND status='ready' ORDER BY title",
                              (BUILTIN_OWNER,)).fetchall()
        return jsonify(books=[dict(row) for row in rows])

    @app.post("/api/demo/message")
    def demo_message():
        data = body()
        book_id, question = data.get("book_id"), data.get("message")
        if not isinstance(book_id, str) or not isinstance(question, str) or not 1 <= len(question.strip()) <= 1000:
            abort(400, description="请选择内置教材并输入 1–1000 字的问题。")
        question = question.strip()
        with database.connect() as db:
            book = db.execute("SELECT id,title FROM books WHERE id=? AND owner_id=? AND category='textbook' "
                              "AND status='ready'", (book_id, BUILTIN_OWNER)).fetchone()
            if book is None:
                abort(404, description="体验教材不存在或未就绪。")
        # Throttle only valid-looking asks: 404 probes stay free.
        throttle(("demo", request.remote_addr), 6, 3600)
        if not model_slots.acquire(blocking=False):
            abort(429, description="体验通道繁忙，请稍后再试。")
        with database.connect() as db:
            rows = db.execute("SELECT * FROM chunks WHERE owner_id=? AND book_id=? ORDER BY ordinal",
                              (BUILTIN_OWNER, book_id)).fetchall()
        chunks = []
        for row in rows:
            chunk = dict(row)
            chunk["embedding"] = json.loads(chunk["embedding"]) if chunk["embedding"] else None
            chunks.append(chunk)

        def fts_lookup(word_list):
            if not word_list:
                return []
            match = " OR ".join('"' + word.replace('"', '""') + '"' for word in word_list)
            try:
                with database.connect() as db:
                    return [r[0] for r in db.execute(
                        "SELECT c.id FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid "
                        "WHERE chunks_fts MATCH ? AND c.owner_id=? AND c.book_id=? "
                        "ORDER BY bm25(chunks_fts) LIMIT 24", (match, BUILTIN_OWNER, book_id))]
            except sqlite3.OperationalError:
                return []

        def run_search(agent_query, limit):
            word_list = terms(agent_query)
            return retrieve(agent_query, chunks, fts_lookup(word_list), embedder, limit=limit)["hits"]

        def stream():
            try:
                yield sse({"type": "status", "stage": "generate", "text": "模型正在检索这本教材…"})
                try:
                    for kind, value in tutor.agent_stream(question, "qa", book["title"], run_search, [], {}):
                        if kind == "delta":
                            yield sse({"type": "delta", "text": value})
                        elif kind == "search":
                            yield sse({"type": "status", "stage": "search", "reset": True,
                                       "text": search_step_text(value)})
                        else:
                            yield sse({"type": "answer", "message": value})
                except TutorError as exc:
                    yield sse({"type": "error", "error": str(exc)})
                except Exception:
                    # Guest channel: any internal failure becomes a readable
                    # SSE error instead of a broken stream.
                    LOG.exception("demo chat failed")
                    yield sse({"type": "error", "error": "体验通道暂时不可用，请稍后再试。"})
            finally:
                model_slots.release()

        add_log("demo_chat", detail=f"游客体验 · 《{book['title'][:30]}》· 问: {question[:40]}")
        return Response(stream_with_context(stream()), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


    @app.get("/api/admin/overview")
    def admin_overview():
        # Deployer-only operations report: per-account activity plus the recent
        # global log stream (app_logs only retains LOG_RETENTION days); guarded
        # in protect() by STUDY_ADMIN_KEY, never by a user session.
        days = min(max(int(request.args.get("days", "3") or 3), 1), int(LOG_RETENTION.days))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="microseconds")
        with database.connect() as db:
            users = db.execute("SELECT id, username, created_at FROM users ORDER BY created_at").fetchall()
            names = {row["id"]: row["username"] for row in users}
            names[BUILTIN_OWNER] = "内置书库"
            accounts = []
            for row in users:
                owner = row["id"]
                shelf = db.execute(
                    "SELECT category, SUM(status='ready') ready, COUNT(*) total "
                    "FROM books WHERE owner_id=? GROUP BY category", (owner,)).fetchall()
                questions = db.execute(
                    "SELECT COUNT(*) c, MAX(created_at) last FROM messages "
                    "WHERE owner_id=? AND role='user'", (owner,)).fetchone()
                window = db.execute(
                    "SELECT COUNT(*) c FROM messages WHERE owner_id=? AND role='user' AND created_at>=?",
                    (owner, cutoff)).fetchone()
                conversations = db.execute(
                    "SELECT COUNT(*) c FROM conversations WHERE owner_id=?", (owner,)).fetchone()
                notes = db.execute(
                    "SELECT COUNT(*) c FROM annotations WHERE owner_id=?", (owner,)).fetchone()
                issues = db.execute(
                    "SELECT COUNT(*) c FROM app_logs WHERE owner_id=? AND level IN ('error','warning') "
                    "AND created_at>=?", (owner, cutoff)).fetchone()
                accounts.append({
                    "username": row["username"], "created_at": row["created_at"],
                    "books": {item["category"]: {"ready": item["ready"], "total": item["total"]} for item in shelf},
                    "conversations": conversations["c"], "questions": questions["c"],
                    "questions_window": window["c"], "annotations": notes["c"],
                    "last_question_at": questions["last"], "issues_window": issues["c"]})
            logs = db.execute(
                "SELECT created_at, owner_id, level, event, detail FROM app_logs "
                "WHERE created_at>=? ORDER BY id DESC LIMIT 200", (cutoff,)).fetchall()
        return jsonify(days=days, accounts=accounts,
                       logs=[{"created_at": row["created_at"], "username": names.get(row["owner_id"], "系统"),
                              "level": row["level"], "event": row["event"], "detail": row["detail"]}
                             for row in logs])

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
                db.execute("UPDATE books SET status='ready',error='',chunk_count=?,section_count=?,index_backend=?,parser_version=? WHERE id=? AND owner_id=?",
                           (len(chunks), len({chunk["section"] for chunk in chunks}), backend,
                            PARSER_VERSION, book_id, owner_id))
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

    def review_due_by_book(db):
        rows = db.execute(
            "SELECT book_id,count(*) AS due FROM quiz_attempts "
            "WHERE owner_id=? AND rating!='' AND due_at<=? GROUP BY book_id",
            (g.user["id"], now())).fetchall()
        return {row["book_id"]: row["due"] for row in rows}

    @app.get("/api/books")
    def books():
        category = request.args.get("category")
        if category and category not in ("textbook", "literature"):
            abort(400, description="分类参数无效。")
        with database.connect() as db:
            if category:
                rows = db.execute("SELECT * FROM books WHERE owner_id IN (?, ?) AND category=? ORDER BY created_at DESC",
                                  (g.user["id"], BUILTIN_OWNER, category)).fetchall()
            else:
                rows = db.execute("SELECT * FROM books WHERE owner_id IN (?, ?) AND category != 'group' ORDER BY created_at DESC",
                                  (g.user["id"], BUILTIN_OWNER)).fetchall()
            due = review_due_by_book(db)
            progress_rows = db.execute(
                "SELECT book_id,version,start,text_length,finished,updated_at "
                "FROM reading_progress WHERE owner_id=?", (g.user["id"],)).fetchall()
            progress = {item["book_id"]: public_reading_progress(item) for item in progress_rows}
        return jsonify(books=[{**public_book(row), "review_due": due.get(row["id"], 0),
                               "reading_progress": progress.get(row["id"])} for row in rows])

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
        category = (request.form.get("category") or "textbook").strip()
        if category not in ("textbook", "literature"):
            abort(400, description="分类参数无效。")
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
                db.execute("INSERT INTO books(id,owner_id,title,filename,source_path,created_at,category) VALUES(?,?,?,?,?,?,?)",
                           (book_id, owner, title, filename, str(source.relative_to(root)), now(), category))
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
            if row["category"] == "group":
                members = db.execute(
                    "SELECT b.id, b.title, b.status FROM group_members gm "
                    "JOIN books b ON b.id=gm.book_id WHERE gm.group_id=?",
                    (book_id,)).fetchall()
                item = public_book(row)
                item["members"] = [dict(m) for m in members]
                due = review_due_by_book(db).get(book_id, 0)
                return jsonify(book=item, sections=[], stats={"review_due": due})
            sections = db.execute("SELECT section AS name,count(*) AS chunk_count FROM chunks "
                                  "WHERE owner_id IN (?, ?) AND book_id=? GROUP BY section ORDER BY min(ordinal)",
                                  (g.user["id"], BUILTIN_OWNER, book_id)).fetchall()
            progress_row = db.execute(
                "SELECT version,start,text_length,finished,updated_at FROM reading_progress "
                "WHERE owner_id=? AND book_id=?", (g.user["id"], book_id)).fetchone()
            # Lightweight study stats for the header line.
            stats = {
                "conversations": db.execute("SELECT count(*) FROM conversations WHERE owner_id=? AND book_id=?",
                                            (g.user["id"], book_id)).fetchone()[0],
                "questions": db.execute("SELECT count(*) FROM messages WHERE owner_id=? AND book_id=? AND role='user'",
                                        (g.user["id"], book_id)).fetchone()[0],
                "annotations": db.execute("SELECT count(*) FROM annotations WHERE owner_id=? AND book_id=?",
                                          (g.user["id"], book_id)).fetchone()[0],
                "review_due": db.execute("SELECT count(*) FROM quiz_attempts WHERE owner_id=? AND book_id=? "
                                         "AND rating!='' AND due_at<=?",
                                         (g.user["id"], book_id, now())).fetchone()[0],
                "last_activity": db.execute("SELECT max(created_at) FROM messages WHERE owner_id=? AND book_id=?",
                                            (g.user["id"], book_id)).fetchone()[0] or "",
            }
        item = public_book(row)
        item["reading_progress"] = public_reading_progress(progress_row)
        return jsonify(book=item, sections=[dict(s) for s in sections], stats=stats)

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
            if row["source_path"]:
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
            if row["category"] == "group":
                abort(400, description="文献组无需索引。")
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
                db.execute("DELETE FROM reading_progress WHERE book_id=? AND owner_id=?", (book_id, g.user["id"]))
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
            if row["category"] == "group":
                abort(400, description="文献组没有原文件。")
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

    # ------------------------------------------------------------------
    # Literature groups
    # ------------------------------------------------------------------

    @app.get("/api/groups")
    def list_groups():
        with database.connect() as db:
            rows = db.execute(
                "SELECT * FROM books WHERE owner_id=? AND category='group' ORDER BY created_at DESC",
                (g.user["id"],)).fetchall()
            groups = []
            due = review_due_by_book(db)
            for row in rows:
                members = db.execute(
                    "SELECT b.id, b.title, b.status FROM group_members gm "
                    "JOIN books b ON b.id=gm.book_id WHERE gm.group_id=?",
                    (row["id"],)).fetchall()
                item = public_book(row)
                item["members"] = [dict(m) for m in members]
                item["review_due"] = due.get(row["id"], 0)
                groups.append(item)
        return jsonify(groups=groups)

    @app.post("/api/groups")
    def create_group():
        data = body()
        name = (data.get("name") or "").strip()[:120]
        book_ids = data.get("book_ids")
        if not name:
            abort(400, description="请输入组名。")
        if not isinstance(book_ids, list) or not 2 <= len(book_ids) <= 20:
            abort(400, description="组内至少 2 篇、至多 20 篇文献。")
        throttle(("create-group", g.user["id"]), 30, 3600)
        owner = g.user["id"]
        with database.connect() as db:
            count = db.execute("SELECT count(*) FROM books WHERE owner_id=? AND category='group'",
                               (owner,)).fetchone()[0]
            if count >= 50:
                abort(400, description="文献组数量已达上限（50）。")
            # Validate all member books
            placeholders = ",".join("?" * len(book_ids))
            members = db.execute(
                f"SELECT id, title, category, status, owner_id FROM books WHERE id IN ({placeholders})",
                book_ids).fetchall()
            found = {m["id"] for m in members}
            for bid in book_ids:
                if bid not in found:
                    abort(400, description=f"文献 {bid[:8]}… 不存在。")
            for m in members:
                if m["owner_id"] not in (owner, BUILTIN_OWNER):
                    abort(403, description=f"无权访问「{m['title'][:30]}」。")
                if m["category"] != "literature":
                    abort(400, description=f"「{m['title'][:30]}」不是文献，无法加入组。")
                if m["status"] != "ready":
                    abort(409, description=f"「{m['title'][:30]}」尚未完成索引。")
            group_id = uid()
            db.execute(
                "INSERT INTO books(id,owner_id,title,filename,source_path,status,created_at,category) "
                "VALUES(?,?,?,?,?,'ready',?,?)",
                (group_id, owner, name, "", "", now(), "group"))
            for bid in book_ids:
                db.execute("INSERT INTO group_members(group_id,book_id) VALUES(?,?)", (group_id, bid))
            row = db.execute("SELECT * FROM books WHERE id=?", (group_id,)).fetchone()
        add_log("group_created", detail=f"创建文献组「{name}」· {len(book_ids)} 篇", owner_id=owner)
        return jsonify(group=public_book(row)), 201

    @app.get("/api/groups/<group_id>")
    def group_detail(group_id):
        with database.connect() as db:
            row = db.execute("SELECT * FROM books WHERE id=? AND owner_id=? AND category='group'",
                             (group_id, g.user["id"])).fetchone()
            if row is None:
                abort(404, description="文献组不存在。")
            members = db.execute(
                "SELECT b.id, b.title, b.status, b.category FROM group_members gm "
                "JOIN books b ON b.id=gm.book_id WHERE gm.group_id=?",
                (group_id,)).fetchall()
        item = public_book(row)
        item["members"] = [dict(m) for m in members]
        return jsonify(group=item)

    @app.patch("/api/groups/<group_id>")
    def update_group(group_id):
        data = body()
        with database.connect() as db:
            row = db.execute("SELECT * FROM books WHERE id=? AND owner_id=? AND category='group'",
                             (group_id, g.user["id"])).fetchone()
            if row is None:
                abort(404, description="文献组不存在。")
            if "name" in data:
                name = (data["name"] or "").strip()[:120]
                if not name:
                    abort(400, description="组名不能为空。")
                db.execute("UPDATE books SET title=? WHERE id=?", (name, group_id))
            if "book_ids" in data:
                book_ids = data["book_ids"]
                if not isinstance(book_ids, list) or not 2 <= len(book_ids) <= 20:
                    abort(400, description="组内至少 2 篇、至多 20 篇文献。")
                owner = g.user["id"]
                placeholders = ",".join("?" * len(book_ids))
                members = db.execute(
                    f"SELECT id, title, category, status, owner_id FROM books WHERE id IN ({placeholders})",
                    book_ids).fetchall()
                found = {m["id"] for m in members}
                for bid in book_ids:
                    if bid not in found:
                        abort(400, description=f"文献 {bid[:8]}… 不存在。")
                for m in members:
                    if m["owner_id"] not in (owner, BUILTIN_OWNER):
                        abort(403, description=f"无权访问「{m['title'][:30]}」。")
                    if m["category"] != "literature":
                        abort(400, description=f"「{m['title'][:30]}」不是文献。")
                    if m["status"] != "ready":
                        abort(409, description=f"「{m['title'][:30]}」尚未完成索引。")
                db.execute("DELETE FROM group_members WHERE group_id=?", (group_id,))
                for bid in book_ids:
                    db.execute("INSERT INTO group_members(group_id,book_id) VALUES(?,?)", (group_id, bid))
                # Conversations may reference stale chunks after membership change.
                db.execute("DELETE FROM conversations WHERE book_id=? AND owner_id=?", (group_id, owner))
            row = db.execute("SELECT * FROM books WHERE id=?", (group_id,)).fetchone()
            members = db.execute(
                "SELECT b.id, b.title, b.status FROM group_members gm "
                "JOIN books b ON b.id=gm.book_id WHERE gm.group_id=?",
                (group_id,)).fetchall()
        item = public_book(row)
        item["members"] = [dict(m) for m in members]
        return jsonify(group=item)

    @app.delete("/api/groups/<group_id>")
    def delete_group(group_id):
        with database.connect() as db:
            row = db.execute("SELECT * FROM books WHERE id=? AND owner_id=? AND category='group'",
                             (group_id, g.user["id"])).fetchone()
            if row is None:
                abort(404, description="文献组不存在。")
            # CASCADE removes group_members and conversations
            db.execute("DELETE FROM books WHERE id=? AND owner_id=?", (group_id, g.user["id"]))
        add_log("group_deleted", detail=f"删除文献组「{row['title'][:40]}」", owner_id=g.user["id"])
        return jsonify(ok=True)

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
            attempts = db.execute(
                "SELECT qa.message_id,qa.question_index,qa.draft,qa.rating,qa.due_at "
                "FROM quiz_attempts qa JOIN messages m ON m.id=qa.message_id "
                "WHERE qa.owner_id=? AND qa.book_id=? AND m.conversation_id=?",
                (g.user["id"], book_id, conversation_id)).fetchall()
        by_message = defaultdict(list)
        for attempt in attempts:
            by_message[attempt["message_id"]].append(dict(attempt))
        # Context meter: the un-compacted tail that counts toward the next
        # compression, plus the summary size already folded away.
        summary = conversation["summary"] or ""
        context = context_usage([dict(row) for row in rows], conversation["summary_mark"] or "")
        context["summary_chars"] = len(summary)
        return jsonify(conversation={key: conversation[key] for key in ("id", "title", "created_at")},
                       messages=[{**public_message(row), "feedback": row["feedback_rating"],
                                  "quiz_attempts": by_message.get(row["id"], [])} for row in rows],
                       context=context)

    def review_due_count(db, book_id=None):
        if book_id is None:
            return db.execute("SELECT count(*) FROM quiz_attempts WHERE owner_id=? "
                              "AND rating!='' AND due_at<=?", (g.user["id"], now())).fetchone()[0]
        return db.execute("SELECT count(*) FROM quiz_attempts WHERE owner_id=? AND book_id=? "
                          "AND rating!='' AND due_at<=?", (g.user["id"], book_id, now())).fetchone()[0]

    @app.patch("/api/books/<book_id>/conversations/<conversation_id>/messages/<message_id>/quiz/<int:question_index>")
    def save_quiz_attempt(book_id, conversation_id, message_id, question_index):
        data = body()
        if set(data) - {"draft", "rating", "reviewed"}:
            abort(400, description="自测记录字段无效。")
        draft, rating, reviewed = data.get("draft"), data.get("rating"), data.get("reviewed", False)
        if (not isinstance(draft, str) or len(draft) > 3000 or "\x00" in draft
                or rating not in ("", "understood", "review") or type(reviewed) is not bool):
            abort(400, description="自测作答或自评无效。")
        throttle(("quiz_attempt", g.user["id"]), 240, 600)
        stamp = now()
        with database.connect() as db:
            book_row(db, book_id)
            conversation_row(db, book_id, conversation_id)
            row = db.execute("SELECT mode,payload FROM messages WHERE id=? AND owner_id=? AND book_id=? "
                             "AND conversation_id=? AND role='assistant'",
                             (message_id, g.user["id"], book_id, conversation_id)).fetchone()
            if row is None or row["mode"] != "quiz":
                abort(404, description="自测题不存在。")
            quiz = json.loads(row["payload"]).get("quiz") or []
            if not 0 <= question_index < len(quiz):
                abort(404, description="自测题不存在。")
            previous = db.execute("SELECT rating,due_at FROM quiz_attempts WHERE owner_id=? AND message_id=? "
                                  "AND question_index=?", (g.user["id"], message_id, question_index)).fetchone()
            if previous and previous["rating"] == rating and not reviewed:
                due = previous["due_at"]
            elif rating == "understood" or (rating == "review" and reviewed):
                due = (datetime.now(timezone.utc) + timedelta(days=3 if rating == "understood" else 1))\
                    .isoformat(timespec="microseconds")
            else:
                due = stamp if rating == "review" else ""
            db.execute("INSERT INTO quiz_attempts(owner_id,book_id,message_id,question_index,draft,rating,due_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(owner_id,message_id,question_index) "
                       "DO UPDATE SET draft=excluded.draft,rating=excluded.rating,due_at=excluded.due_at,"
                       "updated_at=excluded.updated_at",
                       (g.user["id"], book_id, message_id, question_index, draft, rating, due, stamp))
            count = review_due_count(db, book_id)
            total = review_due_count(db)
        return jsonify(attempt={"question_index": question_index, "draft": draft, "rating": rating,
                                "due_at": due}, review_due=count, review_due_total=total)

    def review_items(db, book_id=None):
        cutoff = now()
        where = ("qa.owner_id=? AND m.owner_id=? AND b.owner_id IN (?, ?) "
                 "AND qa.rating!='' AND qa.due_at<=?")
        params = [g.user["id"], g.user["id"], g.user["id"], BUILTIN_OWNER, cutoff]
        if book_id is not None:
            where += " AND qa.book_id=?"
            params.append(book_id)
        source = (" FROM quiz_attempts qa JOIN books b ON b.id=qa.book_id "
                  "JOIN messages m ON m.id=qa.message_id WHERE " + where)
        count = db.execute("SELECT count(*)" + source, params).fetchone()[0]
        rows = db.execute(
            "SELECT qa.book_id,b.title AS book_title,qa.question_index,qa.rating,qa.due_at,"
            "m.id AS message_id,m.conversation_id,m.payload" + source
            + " ORDER BY qa.due_at,qa.updated_at LIMIT 20", params).fetchall()
        items = []
        for row in rows:
            quiz = json.loads(row["payload"]).get("quiz") or []
            if row["question_index"] >= len(quiz):
                continue
            question = quiz[row["question_index"]]
            items.append({"book_id": row["book_id"], "book_title": row["book_title"],
                          "message_id": row["message_id"], "conversation_id": row["conversation_id"],
                          "question_index": row["question_index"], "question": question["question"],
                          "answer": question["answer"], "explanation": question["explanation"],
                          "rating": row["rating"], "due_at": row["due_at"]})
        return items, count

    @app.get("/api/review")
    def all_review_queue():
        with database.connect() as db:
            items, count = review_items(db)
        return jsonify(items=items, due_count=count)

    @app.get("/api/books/<book_id>/review")
    def review_queue(book_id):
        with database.connect() as db:
            book_row(db, book_id)
            items, count = review_items(db, book_id)
            total = review_due_count(db)
        return jsonify(items=items, due_count=count, due_total=total)

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
        if not isinstance(mode, str) or mode not in MODES or (section is not None and not isinstance(section, str)):
            abort(400, description="学习模式或章节参数无效。")
        section = section or None
        if not isinstance(question, str) or len(question) > 3000:
            abort(400, description="message 必须是字符串，且不能超过 3000 字。")
        question = question.strip()
        if mode == "qa" and not question:
            abort(400, description="请输入 1–3000 字的问题。")
        # Empty input and exact action labels mean "use this SQL scope". Match
        # before tokenization/history so "开始" cannot become a retrieval query.
        # Never use prefix matching: "开始讲解想象" still has a specific topic.
        scope_commands = SCOPE_COMMANDS.get(mode, ())
        command_text = re.sub(r"^(?:请帮我|帮我|请)\s*", "", question).rstrip("。.!！?？").strip()
        scope_overview = bool(scope_commands) and (not question or command_text in scope_commands)
        if scope_overview:
            question = scope_commands[0]
        # Opt-in web supplement: honoured only when the deployment configured
        # the search MCP and the agentic QA path (qa mode) will actually run.
        web_enabled = bool(data.get("web")) and searcher.configured and mode == "qa"
        # Quiz tuning and the reader selection ride along as trusted defaults.
        raw_count = data.get("quiz_count", 3)
        quiz_count = raw_count if isinstance(raw_count, int) and raw_count in (3, 5, 10) else 3
        quiz_level = data.get("quiz_level") if data.get("quiz_level") in ("standard", "deep") else "standard"
        selection = data.get("selection")
        if selection is not None:
            if not isinstance(selection, dict) \
                    or not isinstance(selection.get("quote"), str) or not 1 <= len(selection["quote"].strip()) <= 4000 \
                    or not isinstance(selection.get("section", ""), str) or len(selection.get("section", "")) > 200:
                abort(400, description="选文上下文无效。")
            selection = {"quote": selection["quote"].strip(), "section": (selection.get("section") or "").strip()[:200]}
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
                is_group = False
                member_ids = []
                book_titles = {}  # book_id -> title for group citation attribution
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
                    is_group = book["category"] == "group"
                    if is_group:
                        members = db.execute(
                            "SELECT b.id, b.title FROM group_members gm "
                            "JOIN books b ON b.id=gm.book_id WHERE gm.group_id=?",
                            (book_id,)).fetchall()
                        member_ids = [m["id"] for m in members]
                        book_titles = {m["id"]: m["title"] for m in members}
                        if not member_ids:
                            yield sse({"type": "error", "error": "文献组内没有成员。"})
                            return
                    old = db.execute("SELECT * FROM messages WHERE owner_id=? AND book_id=? AND conversation_id=? ORDER BY created_at DESC LIMIT 120",
                                     (owner, book_id, conversation_id)).fetchall()
                    if len(old) >= 120:
                        add_log("chat_error", level="error", detail=f"对话已满 120 条上限 · 问: {question[:60]}", owner_id=owner)
                        yield sse({"type": "error", "error": "当前对话已满，请新建学习对话。"})
                        return
                    # Scope filtering happens in SQL before any channel sees candidates.
                    if is_group:
                        ph = ",".join("?" * len(member_ids))
                        sql = f"SELECT * FROM chunks WHERE owner_id IN (?, ?) AND book_id IN ({ph})"
                        params = [owner, BUILTIN_OWNER] + member_ids
                    else:
                        sql, params = "SELECT * FROM chunks WHERE owner_id IN (?, ?) AND book_id=?", [owner, BUILTIN_OWNER, book_id]
                        if section is not None:
                            sql += " AND section=?"
                            params.append(section)
                    rows = db.execute(sql + " ORDER BY ordinal", params).fetchall()
                    if not is_group and section is not None and not rows:
                        add_log("chat_error", level="error", detail=f"章节「{section}」不存在或已失效 · 问: {question[:60]}", owner_id=owner)
                        yield sse({"type": "error", "error": "章节不存在或已失效，请重新选择。"})
                        return
                    chunks = []
                    for row in rows:
                        chunk = dict(row)
                        chunk["embedding"] = json.loads(chunk["embedding"]) if chunk["embedding"] else None
                        if is_group:
                            chunk["book_title"] = book_titles.get(chunk["book_id"], "")
                        chunks.append(chunk)
                    # A fresh scope task must not inherit the previous topic,
                    # including any pronoun resolution in the model context.
                    previous = [] if scope_overview else [
                        row["content"] for row in reversed(old) if row["role"] == "user"][-3:]
                    generation_summary = "" if scope_overview else summary
                    query = "" if scope_overview else question
                    if not scope_overview and re.search(r"继续|上面|刚才|它|这个|这一|再讲|举例", question) and previous:
                        query = previous[-1][:300] + " " + question

                    def fts_lookup(word_list):
                        """FTS candidates for any query text; scope filters stay in SQL."""
                        if not word_list:
                            return []
                        match = " OR ".join('"' + word.replace('"', '""') + '"' for word in word_list)
                        if is_group:
                            ph = ",".join("?" * len(member_ids))
                            fts_sql = ("SELECT c.id FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid "
                                       f"WHERE chunks_fts MATCH ? AND c.owner_id IN (?, ?) AND c.book_id IN ({ph})")
                            fts_params = [match, owner, BUILTIN_OWNER] + member_ids
                        else:
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

                    search_terms = [] if scope_overview else terms(query)
                # Preserve legacy keyword-free non-QA overviews, but never use
                # a failed topical retrieval as a reason to sample unrelated text.
                overview = scope_overview or (mode != "qa" and not search_terms)
                yield sse({"type": "status", "stage": "retrieve", "text":
                           "正在按当前范围选取原文片段…" if overview else
                           "正在检索文献组相关内容…" if is_group else "正在检索本书相关内容…"})
                streamed, answer = False, None
                user_key = g.user.get("api_key") or ""
                hit_count, retrieve_ms = 0, 0
                retrieval = {}
                try:
                    generate_started = time.monotonic()
                    # Concrete explain/outline questions run the agentic loop
                    # (multi-round retrieval, diagrams); bare scope commands
                    # keep the deterministic sampling pipeline, quiz stays on
                    # the classic one-shot path, and classic retrieval remains
                    # the fallback when the loop fails before streaming.
                    if not scope_overview and mode != "quiz":
                        agent_state = {"steps": [], "word_set": set(), "backend": "lexical+fts5",
                                       "degraded": True, "ms": 0}

                        def run_agent_search(agent_query, limit):
                            started = time.monotonic()
                            word_list = terms(agent_query)
                            agent_state["word_set"].update(word_list)
                            result = retrieve(agent_query, chunks, fts_lookup(word_list), embedder, limit=limit,
                                              gate_hook=lambda event, level, detail: add_log(
                                                  event, level=level,
                                                  detail=detail + f" · 问: {question[:40]}", owner_id=owner))
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
                                   "text": "模型正在自主检索文献组并核对引用…" if is_group else "模型正在自主检索本书并核对引用…"})
                        try:
                            for kind, value in tutor.agent_stream(question, mode, book["title"], run_agent_search,
                                                                  previous, retrieval, user_key, summary,
                                                                  run_web_search if web_enabled else None,
                                                                  is_group=is_group, quiz_count=quiz_count,
                                                                  quiz_level=quiz_level, selection=selection):
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
                        if overview:
                            # Sampling is its own material-selection path: no FTS,
                            # query embedding or relevance gate for instruction words.
                            result = {"hits": [], "backend": "scope-sampling", "degraded": False}
                            if chunks:
                                indices = sorted({round(i * (len(chunks) - 1) / min(5, len(chunks) - 1))
                                                  for i in range(min(6, len(chunks)))}) if len(chunks) > 1 else [0]
                                result["hits"] = [chunks[index] for index in indices]
                        else:
                            result = retrieve(query, chunks, fts_lookup(search_terms), embedder,
                                              gate_hook=lambda event, level, detail: add_log(
                                                  event, level=level,
                                                  detail=detail + f" · 问: {question[:40]}", owner_id=owner))
                        retrieve_ms = int((time.monotonic() - retrieve_started) * 1000)
                        retrieval = {key: result[key] for key in ("backend", "degraded")}
                        retrieval["scope"] = "selected-excerpts" if overview else "retrieved-excerpts"
                        retrieval["scope_overview"] = overview
                        retrieval["section"] = section
                        retrieval["total_chunks"] = len(chunks)
                        hit_count = len(result["hits"])
                        retrieval["hits"] = hit_count
                        retrieval["retrieve_ms"] = retrieve_ms
                        retrieval["terms"] = search_terms[:24]
                        if overview:
                            stage_text = (f"已按当前范围抽取 {hit_count}/{len(chunks)} 段原文，不代表完整覆盖，"
                                          "正在核对引用并生成回答…")
                        else:
                            stage_text = (f"已定位 {hit_count} 段相关原文，正在核对引用并生成回答…" if hit_count
                                          else "未检索到直接相关的原文，正在整理回答…")
                        yield sse({"type": "status", "stage": "generate", "text": stage_text, "hits": hit_count})
                        if mode != "quiz":
                            try:
                                for kind, value in tutor.generate_stream(question, mode, book["title"], result["hits"],
                                                                         previous, retrieval, user_key, generation_summary,
                                                                         is_group=is_group, quiz_count=quiz_count,
                                                                         quiz_level=quiz_level, selection=selection):
                                    if kind == "delta":
                                        streamed = True
                                        yield sse({"type": "delta", "text": value})
                                    else:
                                        answer = value
                            except TutorError:
                                if streamed:
                                    raise
                                # Streaming failed before any text arrived; retry one-shot.
                                answer = tutor.generate(question, mode, book["title"], result["hits"], previous,
                                                        retrieval, user_key, generation_summary, is_group=is_group,
                                                        quiz_count=quiz_count, quiz_level=quiz_level, selection=selection)
                        else:
                            answer = tutor.generate(question, mode, book["title"], result["hits"], previous,
                                                    retrieval, user_key, generation_summary, is_group=is_group,
                                                    quiz_count=quiz_count, quiz_level=quiz_level, selection=selection)
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
                if overview:
                    answer["notice"] = (f"本次按当前范围的位置抽取 {hit_count}/{len(chunks)} 段原文辅助学习（最多 6 段），"
                                        "不代表完整覆盖当前范围、全章、全书或文献组。")
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
                channel = ("按范围选段" if overview else
                           "语义向量已启用" if not retrieval["degraded"] else "语义向量不可用（关键词检索）")
                material_note = (f"抽取 {hit_count}/{len(chunks)} 段（不代表完整覆盖）" if overview
                                 else f"命中 {hit_count} 段")
                searches_note = f" · 自主检索 {retrieval['searches']} 次" if retrieval.get("searches") else ""
                web_note = f" · 联网 {retrieval['web_searches']} 次" if retrieval.get("web_searches") else ""
                add_log("chat", level="warning" if retrieval["degraded"] else "info",
                        detail=(f"《{book['title']}》· {channel} · {material_note}{searches_note}{web_note} · "
                                f"{'选段' if overview else '检索'} {retrieve_ms}ms · "
                                f"生成 {retrieval.get('generate_ms', 0) / 1000:.1f}s · 问: {question[:60]}"),
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

    def seed_builtin_book(path: Path, category: str = "textbook"):
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
                    if (row["filename"] == path.name and source.is_file()
                            and source.read_bytes() == path.read_bytes()
                            and row["parser_version"] == PARSER_VERSION):
                        return
                if row is None:
                    folder = root / "sources" / BUILTIN_OWNER
                    folder.mkdir(parents=True, exist_ok=True)
                    source = folder / (book_id + path.suffix.lower())
                    shutil.copyfile(path, source)
                    db.execute("INSERT INTO books(id,owner_id,title,filename,source_path,created_at,category) VALUES(?,?,?,?,?,?,?)",
                               (book_id, BUILTIN_OWNER, title, path.name, str(source.relative_to(root)), now(), category))
                else:
                    source = root / row["source_path"]
                    if not source.is_file() or source.read_bytes() != path.read_bytes():
                        shutil.copyfile(path, source)
                    # Old citations must never refer to new chunks after a content update.
                    db.execute("DELETE FROM conversations WHERE book_id=?", (book_id,))
                    db.execute("DELETE FROM reading_progress WHERE book_id=?", (book_id,))
                db.execute("UPDATE books SET status='queued',error='',filename=?,category=? WHERE id=? AND owner_id=?",
                           (path.name, category, book_id, BUILTIN_OWNER))
            # The dedicated seed thread may wait without delaying web requests.
            index_slots.acquire()
            reserved = True
            LOG.info("Seeding builtin book: %s [%s]", title, category)
            executor.submit(index_book, BUILTIN_OWNER, book_id, source, path.name, book_lock)
            queued = True
        finally:
            if not queued:
                book_lock.release()
                if reserved:
                    index_slots.release()

    def seed_builtin_books():
        # Baked-in textbooks and literature ship with the image; every account
        # can read them. Files in builtin_books/ root are textbooks; files in
        # builtin_books/literature/ are literature. Tests disable the seeder
        # (STUDY_SEED_BUILTIN=0) to keep index slots free and runs fast.
        if os.getenv("STUDY_SEED_BUILTIN", "1") != "1":
            return
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
                seed_builtin_book(path, "textbook")
            except Exception:
                LOG.exception("Builtin book seeding failed: %s", path.name)
        literature_folder = folder / "literature"
        if literature_folder.is_dir():
            for path in sorted(literature_folder.iterdir()):
                if not path.is_file() or path.suffix.lower() not in FORMATS:
                    continue
                try:
                    seed_builtin_book(path, "literature")
                except Exception:
                    LOG.exception("Builtin literature seeding failed: %s", path.name)

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
