"""Canonical full-text reading and private, versioned UTF-16 annotations."""

import hashlib
import re
import sys
import threading
from array import array
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from contextlib import contextmanager

from flask import abort, g, jsonify, request

from .documents import MAX_CHARS, MAX_CHUNKS, parse_document, split_sections

BLOCK_CHARS = 2400
CACHE_ENTRIES = 3
CACHE_BYTES = 64 * 1024 * 1024
ANNOTATION_FIELDS = "id,version,start,end,quote,note,color,created_at,updated_at"
COLORS = {"yellow", "blue", "green"}


class Snapshot:
    def __init__(self, sections):
        self.text = "\n\n".join(section["text"].strip() for section in sections)
        if not self.text or len(self.text) > MAX_CHARS + 2 * MAX_CHUNKS or len(sections) > MAX_CHUNKS:
            raise ValueError("Invalid canonical text length")
        self.version = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        # Sparse tables avoid one Python integer per character in ordinary books.
        self.astral = array("I", (i for i, char in enumerate(self.text) if ord(char) > 0xffff))
        self.interiors = array("I", (position + i + 1 for i, position in enumerate(self.astral)))
        self.length = self.to_utf16(len(self.text))
        self.blocks, self.toc, self.chunks = [], [], []
        position = 0
        for index, section in enumerate(sections):
            text = section["text"].strip()
            section_end = position + len(text) + (2 if index + 1 < len(sections) else 0)
            self.toc.append({"title": section["section"], "start": self.to_utf16(position),
                             "index": len(self.blocks)})
            # Inter-section separators belong to the preceding section's last block.
            while position < section_end:
                end = min(position + BLOCK_CHARS, section_end)
                if end < section_end:
                    boundary = self.text.rfind("\n\n", position + BLOCK_CHARS // 2, end)
                    if boundary >= 0:
                        end = boundary + 2
                self.blocks.append({"index": len(self.blocks), "start": self.to_utf16(position),
                                    "end": self.to_utf16(end), "section": section["section"],
                                    "source_start": position, "source_end": end})
                position = end
        for chunk in split_sections(sections, include_offsets=True):
            # Retain only spans, not another overlapping copy of the book text.
            self.chunks.append({key: chunk[key] for key in
                                ("ordinal", "section", "page", "source_start", "source_end")})
        self.starts = [block["start"] for block in self.blocks]
        # This intentionally overcounts shared labels, keeping retained memory bounded.
        self.weight = sum(sys.getsizeof(value) for value in
                          (self.text, self.version, self.astral, self.interiors, self.starts,
                           self.blocks, self.toc, self.chunks))
        self.weight += sum(sys.getsizeof(value) for value in self.starts)
        self.weight += sum(sys.getsizeof(row) + sum(sys.getsizeof(value) for value in row.values())
                           for rows in (self.blocks, self.toc, self.chunks) for row in rows)

    def to_utf16(self, position):
        return position + bisect_left(self.astral, position)

    def to_codepoint(self, offset):
        if type(offset) is not int or not 0 <= offset <= self.length:
            abort(400, description="正文位置无效。")
        index = bisect_left(self.interiors, offset)
        if index < len(self.interiors) and self.interiors[index] == offset:
            abort(400, description="正文位置不能截断 UTF-16 字符。")
        return offset - index

    def block_index(self, offset):
        return max(0, bisect_right(self.starts, offset) - 1)

    def window(self, start, end):
        return [{"index": block["index"], "start": block["start"], "end": block["end"],
                 "text": self.text[block["source_start"]:block["source_end"]],
                 "section": block["section"]} for block in self.blocks[start:end]]

    def anchor(self, chunk):
        ordinal = chunk["ordinal"]
        if not 1 <= ordinal <= len(self.chunks):
            abort(409, description="引用与当前正文不一致，请重建索引。")
        span = self.chunks[ordinal - 1]
        start, end = span["source_start"], span["source_end"]
        if (span["ordinal"] != ordinal or span["section"] != chunk["section"]
                or span["page"] != chunk["page"] or self.text[start:end] != chunk["text"]):
            abort(409, description="引用与当前正文不一致，请重建索引。")
        return {"start": self.to_utf16(start), "end": self.to_utf16(end)}


def source_stamp(path):
    try:
        stat = path.stat()
        if not path.is_file():
            raise FileNotFoundError
    except OSError:
        abort(404, description="教材原文件不存在或不可读取。")
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def check_source(path, stamp):
    if source_stamp(path) != stamp:
        abort(409, description="教材原文件已更新，请重新打开阅读器。")


class SnapshotCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = OrderedDict()
        self.weight = 0

    def get(self, book, root):
        try:
            path = (root / book["source_path"]).resolve()
            if not path.is_relative_to(root.resolve()):
                abort(404, description="教材原文件不存在或不可读取。")
        except (OSError, ValueError, RuntimeError):
            abort(404, description="教材原文件不存在或不可读取。")
        stamp = source_stamp(path)
        key = (book["id"], str(path), book["filename"], stamp)
        with self.lock:
            snapshot = self.entries.get(key)
            if snapshot is not None:
                self.entries.move_to_end(key)
                return snapshot, path, stamp
            for old_key in list(self.entries):
                if old_key[0] == book["id"]:
                    self.weight -= self.entries.pop(old_key).weight
        try:
            snapshot = Snapshot(parse_document(path, book["filename"]))
        except OSError:
            abort(404, description="教材原文件不存在或不可读取。")
        except ValueError:
            abort(409, description="教材原文件无法解析，请检查原文件并重建索引。")
        check_source(path, stamp)
        if snapshot.weight <= CACHE_BYTES:
            with self.lock:
                while self.entries and (len(self.entries) >= CACHE_ENTRIES
                                        or self.weight + snapshot.weight > CACHE_BYTES):
                    self.weight -= self.entries.popitem(last=False)[1].weight
                self.entries[key] = snapshot
                self.weight += snapshot.weight
        return snapshot, path, stamp


def query_integer(name, default=None, minimum=0):
    raw = request.args.get(name)
    if raw is None:
        return default
    if not re.fullmatch(r"[0-9]{1,19}", raw):
        abort(400, description="阅读参数无效。")
    value = int(raw)
    if not minimum <= value <= 9223372036854775807:
        abort(400, description="阅读参数超出范围。")
    return value


def version_value(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        abort(400, description="正文版本无效。")
    return value


def annotation_text(value, allow_empty=True):
    if not isinstance(value, str) or "\x00" in value:
        abort(400, description="批注文本无效。")
    try:
        length = len(value.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        abort(400, description="批注文本包含无效字符。")
    if length > 4000 or (not allow_empty and not value):
        abort(400, description="批注文本应为不超过 4000 个 UTF-16 字符的有效文本。")
    return value


def annotation_color(value):
    if not isinstance(value, str) or value not in COLORS:
        abort(400, description="高亮颜色无效。")
    return value


def register_reader_routes(app, database, root, book_row, book_lock_for, body, throttle, now, uid):
    cache = SnapshotCache()

    @contextmanager
    def locked_book(book_id):
        # Authenticate before accessing even a warm shared-book snapshot or its lock.
        with database.connect() as db:
            book_row(db, book_id)
        lock = book_lock_for(book_id)
        if not lock.acquire(blocking=False):
            abort(409, description="教材正在处理，请稍后重试。")
        try:
            with database.connect() as db:
                book = book_row(db, book_id)
            yield book
        finally:
            lock.release()

    def annotation_row(db, book_id, annotation_id):
        row = db.execute(f"SELECT {ANNOTATION_FIELDS} FROM annotations "
                         "WHERE id=? AND book_id=? AND owner_id=?",
                         (annotation_id, book_id, g.user["id"])).fetchone()
        if row is None:
            abort(404, description="批注不存在或不可访问。")
        return dict(row)

    @app.get("/api/books/<book_id>/reader")
    def reader(book_id):
        allowed = {"start", "count", "anchor", "at", "version"}
        if set(request.args) - allowed or any(len(request.args.getlist(key)) != 1 for key in request.args):
            abort(400, description="阅读参数无效。")
        if ("anchor" in request.args and "at" in request.args
                or "start" in request.args and ("anchor" in request.args or "at" in request.args)):
            abort(400, description="阅读定位参数不能同时使用。")
        start = query_integer("start", 0)
        count = query_integer("count", 12, 1)
        chunk_id = query_integer("anchor", minimum=1)
        at = query_integer("at")
        if count > 20:
            abort(400, description="每次最多读取 20 个正文块。")
        version = version_value(request.args["version"]) if "version" in request.args else None
        with locked_book(book_id) as book:
            if book["status"] != "ready":
                abort(409, description="教材尚未完成索引。")
            chunk = None
            if chunk_id is not None:
                with database.connect() as db:
                    chunk = db.execute("SELECT ordinal,text,section,page FROM chunks "
                                       "WHERE id=? AND book_id=? AND owner_id=?",
                                       (chunk_id, book_id, book["owner_id"])).fetchone()
                if chunk is None:
                    abort(404, description="引用不属于当前教材或已失效。")
            snapshot, path, stamp = cache.get(book, root)
            if version is not None and version != snapshot.version:
                abort(409, description="正文版本已更新，请重新打开阅读器。")
            anchor = snapshot.anchor(chunk) if chunk is not None else None
            target = anchor["start"] if anchor else at
            if target is not None:
                snapshot.to_codepoint(target)
                if target >= snapshot.length:
                    abort(400, description="正文位置超出范围。")
                start = max(0, snapshot.block_index(target) - 2)
                last = snapshot.block_index(anchor["end"] - 1 if anchor else target)
                count = max(count, last - start + 1)
            if start > len(snapshot.blocks):
                abort(400, description="正文块位置超出范围。")
            result = {"version": snapshot.version, "blocks": snapshot.window(start, start + count),
                      "total": len(snapshot.blocks), "length": snapshot.length, "toc": snapshot.toc,
                      "anchor": anchor, "offset_unit": "utf-16"}
            check_source(path, stamp)
            return jsonify(result)

    @app.get("/api/books/<book_id>/annotations")
    def annotations(book_id):
        with locked_book(book_id):
            with database.connect() as db:
                rows = db.execute(f"SELECT {ANNOTATION_FIELDS} FROM annotations "
                                  "WHERE book_id=? AND owner_id=? ORDER BY start,id",
                                  (book_id, g.user["id"])).fetchall()
            return jsonify(annotations=[dict(row) for row in rows])

    @app.post("/api/books/<book_id>/annotations")
    def new_annotation(book_id):
        data = body()
        if set(data) - {"version", "start", "end", "quote", "note", "color"}:
            abort(400, description="批注字段无效。")
        version = version_value(data.get("version"))
        start, end = data.get("start"), data.get("end")
        if type(start) is not int or type(end) is not int or not 0 <= start < end:
            abort(400, description="批注范围无效。")
        quote = annotation_text(data.get("quote"), allow_empty=False)
        note = annotation_text(data.get("note", ""))
        color = annotation_color(data.get("color", "yellow"))
        throttle(("annotations", g.user["id"]), 120, 600)
        with locked_book(book_id) as book:
            # Annotation management is independent of index status, including creation.
            snapshot, path, stamp = cache.get(book, root)
            if version != snapshot.version:
                abort(409, description="正文版本已更新，请重新选择高亮文字。")
            cp_start, cp_end = snapshot.to_codepoint(start), snapshot.to_codepoint(end)
            if end - start > 4000 or snapshot.text[cp_start:cp_end] != quote:
                abort(400, description="选中文字与正文范围不一致。")
            annotation_id, stamp_now = uid(), now()
            with database.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                book_row(db, book_id)
                if db.execute("SELECT count(*) FROM annotations WHERE book_id=? AND owner_id=?",
                              (book_id, g.user["id"])).fetchone()[0] >= 500:
                    abort(400, description="本书已达到 500 条私人批注上限。")
                check_source(path, stamp)
                db.execute("INSERT INTO annotations "
                           "(id,book_id,owner_id,version,start,end,quote,note,color,created_at,updated_at) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (annotation_id, book_id, g.user["id"], version, start, end, quote,
                            note, color, stamp_now, stamp_now))
                result = annotation_row(db, book_id, annotation_id)
                check_source(path, stamp)
            return jsonify(annotation=result), 201

    @app.patch("/api/books/<book_id>/annotations/<annotation_id>")
    def update_annotation(book_id, annotation_id):
        data = body()
        if not data or set(data) - {"note", "color"}:
            abort(400, description="只能修改批注笔记或颜色。")
        if "note" in data:
            data["note"] = annotation_text(data["note"])
        if "color" in data:
            data["color"] = annotation_color(data["color"])
        throttle(("annotations", g.user["id"]), 120, 600)
        with locked_book(book_id):
            with database.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                book_row(db, book_id)
                row = annotation_row(db, book_id, annotation_id)
                db.execute("UPDATE annotations SET note=?,color=?,updated_at=? "
                           "WHERE id=? AND book_id=? AND owner_id=?",
                           (data.get("note", row["note"]), data.get("color", row["color"]), now(),
                            annotation_id, book_id, g.user["id"]))
                result = annotation_row(db, book_id, annotation_id)
            return jsonify(annotation=result)

    @app.delete("/api/books/<book_id>/annotations/<annotation_id>")
    def delete_annotation(book_id, annotation_id):
        throttle(("annotations", g.user["id"]), 120, 600)
        with locked_book(book_id):
            with database.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                book_row(db, book_id)
                annotation_row(db, book_id, annotation_id)
                db.execute("DELETE FROM annotations WHERE id=? AND book_id=? AND owner_id=?",
                           (annotation_id, book_id, g.user["id"]))
            return jsonify(ok=True)
