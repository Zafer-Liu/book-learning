"""Offline reader regressions; external services and startup workers are mocked."""

import hashlib
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

with patch("dotenv.load_dotenv", return_value=False):
    from study.app import create_app, now, uid

from study.database import BUILTIN_OWNER, Database
from study.documents import parse_document, split_sections
from study.reader import (CACHE_BYTES, CACHE_ENTRIES, Snapshot, SnapshotCache,
                          check_source, register_reader_routes)

ASTRAL = "\U0001f600"
# Captured before any test patches threading.Thread app-wide: the seeder
# lock test needs a REAL thread object, not the module-level mock.
REAL_THREAD = threading.Thread


def utf16(text):
    return len(text.encode("utf-16-le")) // 2


def utf16_slice(text, start, end):
    return text.encode("utf-16-le")[start * 2:end * 2].decode("utf-16-le")


class CanonicalTests(unittest.TestCase):
    def test_long_section_is_partitioned_without_overlap(self):
        sections = [{"section": "Long", "text": "  " + "alpha " * 2000 + "  ", "page": None}]
        snapshot = Snapshot(sections)
        blocks = snapshot.window(0, len(snapshot.blocks))
        self.assertGreater(len(blocks), 2)
        self.assertEqual("".join(block["text"] for block in blocks), sections[0]["text"].strip())
        self.assertEqual(blocks[0]["start"], 0)
        self.assertEqual(blocks[-1]["end"], snapshot.length)
        self.assertTrue(all(a["end"] == b["start"] for a, b in zip(blocks, blocks[1:])))
        self.assertTrue(all(len(block["text"]) <= 2400 for block in blocks))
        chunks = split_sections(sections)
        self.assertGreater(sum(len(chunk["text"]) for chunk in chunks), len(snapshot.text))

    def test_paragraph_boundaries_sections_toc_and_utf16(self):
        sections = [{"section": "First", "text": "a" * 1500 + "\n\n" + "b" * 1700},
                    {"section": "Second", "text": ASTRAL * 20 + " c " * 1500},
                    {"section": "Third", "text": "last"}]
        snapshot = Snapshot(sections)
        expected = "\n\n".join(section["text"].strip() for section in sections)
        blocks = snapshot.window(0, len(snapshot.blocks))
        self.assertEqual("".join(block["text"] for block in blocks), expected)
        self.assertEqual(blocks[0]["text"], "a" * 1500 + "\n\n")
        self.assertEqual(snapshot.length, utf16(expected))
        position = 0
        for toc, section in zip(snapshot.toc, sections):
            self.assertEqual(toc["title"], section["section"])
            self.assertEqual(toc["start"], utf16(expected[:position]))
            self.assertEqual(blocks[toc["index"]]["start"], toc["start"])
            self.assertEqual(blocks[toc["index"]]["section"], toc["title"])
            position += len(section["text"].strip()) + 2
        for block in blocks:
            self.assertEqual(utf16_slice(expected, block["start"], block["end"]), block["text"])

    def test_optional_chunk_offsets_preserve_default_output_and_trim(self):
        sections = [{"section": "Same", "text": " \n" + ("a" * 1100 + "\n\n  ") * 4 + "  ", "page": 3},
                    {"section": "Same", "text": " \t" + "b" * 1500 + " \n", "page": 4}]
        canonical = "\n\n".join(section["text"].strip() for section in sections)
        default = split_sections(sections)
        located = split_sections(sections, include_offsets=True)
        self.assertEqual(default, [{key: value for key, value in chunk.items()
                                    if key not in {"source_start", "source_end"}} for chunk in located])
        for chunk in located:
            self.assertEqual(canonical[chunk["source_start"]:chunk["source_end"]], chunk["text"])
        self.assertEqual(located[-1]["source_end"], len(canonical))


class ReaderApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.embedder = Mock(configured=False)
        self.embedder.embed_texts.side_effect = AssertionError("No embedding calls allowed")
        self.tutor = Mock()
        self.tutor.generate.side_effect = AssertionError("No model calls allowed")
        self.tutor.generate_stream.side_effect = AssertionError("No model calls allowed")
        self.tutor.agent_stream.side_effect = AssertionError("No model calls allowed")
        self.executor = Mock()
        self.executor.submit.side_effect = lambda fn, *args: fn(*args)
        self.threads = Mock()

        def capture_routes(*args):
            self.book_lock_for = args[4]
            return register_reader_routes(*args)

        for patcher in (
            patch.dict(os.environ, {}, clear=True),
            patch("study.app.ROOT", self.root),
            patch("study.app.EmbeddingClient", return_value=self.embedder),
            patch("study.app.Tutor", return_value=self.tutor),
            patch("study.app.ThreadPoolExecutor", return_value=self.executor),
            patch("study.app.threading.Thread", self.threads),
            patch("study.app.register_reader_routes", side_effect=capture_routes),
            patch("requests.sessions.Session.request", side_effect=AssertionError("No network allowed")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app = create_app({"TESTING": True, "DATA_ROOT": self.root,
                               "SECRET_KEY": "reader-test-secret-" * 4,
                               "SESSION_COOKIE_SECURE": False, "TEST_CODES": ()})
        self.db = self.app.extensions["database"]
        self.seed_builtin_books = self.threads.call_args_list[0].kwargs["target"]
        self.a, self.a_id, self.a_csrf = self.user("reader_a")
        self.b, self.b_id, self.b_csrf = self.user("reader_b")
        with self.db.connect() as db:
            db.execute("INSERT INTO users(id,username,username_key,password_hash,created_at) VALUES(?,?,?,?,?)",
                       (BUILTIN_OWNER, "builtin", "builtin", "unused", now()))

    def tearDown(self):
        self.embedder.embed_texts.assert_not_called()
        self.tutor.generate.assert_not_called()
        self.tutor.generate_stream.assert_not_called()
        self.tutor.agent_stream.assert_not_called()

    def user(self, name):
        user_id, token = uid(), uid()
        with self.db.connect() as db:
            db.execute("INSERT INTO users(id,username,username_key,password_hash,created_at) VALUES(?,?,?,?,?)",
                       (user_id, name, name, "unused", now()))
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["user_id"], session["csrf"] = user_id, token
        return client, user_id, token

    def book(self, content=None, owner=None, filename="source.md"):
        owner = owner or self.a_id
        content = content if content is not None else "# Chapter\n\n" + "The complete original textbook. " * 120
        book_id = uid()
        path = self.root / (book_id + Path(filename).suffix)
        path.write_text(content, encoding="utf-8", newline="\n")
        sections = parse_document(path, filename)
        chunks = split_sections(sections)
        with self.db.connect() as db:
            db.execute("INSERT INTO books(id,owner_id,title,filename,source_path,status,created_at,chunk_count,section_count) "
                       "VALUES(?,?,?,?,?,'ready',?,?,?)",
                       (book_id, owner, "Reader book", filename, path.name, now(), len(chunks), len(sections)))
            ids = []
            for chunk in chunks:
                ids.append(db.execute("INSERT INTO chunks(owner_id,book_id,ordinal,section,page,text) VALUES(?,?,?,?,?,?)",
                                      (owner, book_id, chunk["ordinal"], chunk["section"],
                                       chunk["page"], chunk["text"])).lastrowid)
        return book_id, path, sections, ids

    def reader(self, book_id, client=None, **params):
        return (client or self.a).get(f"/api/books/{book_id}/reader", query_string=params)

    def annotations(self, book_id, client=None):
        return (client or self.a).get(f"/api/books/{book_id}/annotations")

    def create_annotation(self, book_id, payload=None, client=None, csrf=None):
        if payload is None:
            reading = self.reader(book_id, client=client).get_json()
            text = reading["blocks"][0]["text"][:10]
            payload = {"version": reading["version"], "start": 0, "end": utf16(text), "quote": text}
        return (client or self.a).post(f"/api/books/{book_id}/annotations", json=payload,
                                       headers={"X-CSRF-Token": csrf or self.a_csrf})

    def rewrite(self, path, content):
        previous = path.stat()
        path.write_text(content, encoding="utf-8", newline="\n")
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000))

    def test_reader_pagination_reconstructs_canonical_and_toc(self):
        raw = "---\nhidden: yes\n---\n# Alpha\n" + ("A paragraph. " * 100 + "\n\n") * 25
        raw += "\n## Beta\n" + ASTRAL + "Last chapter. " * 300 + '\n<script>alert("text")</script>'
        book_id, _, sections, _ = self.book(raw)
        expected = "\n\n".join(section["text"].strip() for section in sections)
        initial = self.reader(book_id).get_json()
        self.assertEqual(len(initial["blocks"]), 12)
        self.assertEqual(initial["offset_unit"], "utf-16")
        self.assertEqual(initial["version"], hashlib.sha256(expected.encode("utf-8")).hexdigest())
        self.assertEqual(initial["length"], utf16(expected))
        self.assertIsNone(initial["anchor"])
        blocks = initial["blocks"]
        while len(blocks) < initial["total"]:
            response = self.reader(book_id, start=len(blocks), count=3, version=initial["version"])
            self.assertEqual(response.status_code, 200)
            page = response.get_json()
            self.assertEqual(page["toc"], initial["toc"])
            self.assertEqual(page["version"], initial["version"])
            blocks.extend(page["blocks"])
        self.assertEqual("".join(block["text"] for block in blocks), expected)
        self.assertEqual([block["index"] for block in blocks], list(range(initial["total"])))
        for block in blocks:
            self.assertEqual(utf16_slice(expected, block["start"], block["end"]), block["text"])
        self.assertEqual(self.reader(book_id, start=initial["total"], version=initial["version"])
                         .get_json()["blocks"], [])
        self.assertNotIn("hidden", expected)
        self.assertIn('<script>alert("text")</script>', expected)
        self.assertNotIn("source_path", initial)
        self.assertNotIn("owner_id", initial)

    def test_repeated_text_anchors_use_ordinal_and_utf16(self):
        repeated = ("repeat" + ASTRAL) * 1800
        book_id, _, sections, ids = self.book(repeated, filename="repeat.txt")
        chunks = split_sections(sections, include_offsets=True)
        target = 7
        self.assertEqual(chunks[0]["text"], chunks[7]["text"])
        response = self.reader(book_id, anchor=ids[target], count=1)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        start, end = chunks[target]["source_start"], chunks[target]["source_end"]
        self.assertEqual(data["anchor"], {"start": utf16(repeated[:start]), "end": utf16(repeated[:end])})
        self.assertGreater(data["anchor"]["start"], 0)
        self.assertEqual(utf16_slice(repeated, **data["anchor"]), chunks[target]["text"])
        all_blocks = Snapshot(sections).blocks
        target_index = next(block["index"] for block in all_blocks
                            if block["start"] <= data["anchor"]["start"] < block["end"])
        self.assertEqual(data["blocks"][0]["index"], max(0, target_index - 2))
        self.assertLessEqual(data["blocks"][0]["start"], data["anchor"]["start"])
        self.assertGreaterEqual(data["blocks"][-1]["end"], data["anchor"]["end"])
        self.assertLessEqual(len(data["blocks"]), 20)
        at = self.reader(book_id, at=data["anchor"]["start"], version=data["version"], count=1).get_json()
        self.assertIsNone(at["anchor"])
        self.assertEqual(at["blocks"][0]["index"], data["blocks"][0]["index"])

    def test_repeated_sections_anchor_the_second_occurrence(self):
        raw = ("# Same\n" + "identical content " * 30 + "\n") * 3
        book_id, _, sections, ids = self.book(raw)
        self.assertEqual(len(sections), 3)
        data = self.reader(book_id, anchor=ids[1]).get_json()
        self.assertEqual(data["anchor"]["start"], utf16(sections[0]["text"]) + 2)
        self.assertEqual(data["toc"][1]["start"], data["anchor"]["start"])
        self.assertEqual(data["blocks"][data["toc"][1]["index"]]["section"], "Same")

    def test_invalid_reader_parameters_and_surrogate_boundaries(self):
        book_id, _, _, ids = self.book("a" + ASTRAL + "b" * 6000, filename="unicode.txt")
        data = self.reader(book_id).get_json()
        invalid = [{"count": value} for value in ("bad", "true", "", 0, -1, 21, "1.5", "1e1", " 2")]
        invalid += [{"start": value} for value in (-1, "nan", "0.0", "false", "9" * 200)]
        invalid += [{"anchor": value} for value in (0, -1, "false", "1.0", "9" * 20)]
        invalid += [{"at": value, "version": data["version"]} for value in (-1, "bad", 2, data["length"])]
        invalid += [{"start": 0, "anchor": ids[0]}, {"start": 0, "at": 0},
                    {"anchor": ids[0], "at": 0}, {"version": "bad"}, {"version": ""}, {"other": 1},
                    {"start": data["total"] + 1, "version": data["version"]}]
        for params in invalid:
            with self.subTest(params=params):
                self.assertEqual(self.reader(book_id, **params).status_code, 400)
        self.assertEqual(self.a.get(f"/api/books/{book_id}/reader?count=1&count=2").status_code, 400)
        self.assertEqual(self.reader(book_id, at=3, version=data["version"]).status_code, 200)
        self.assertEqual(self.reader(book_id, at=0).status_code, 200)
        self.assertEqual(self.reader(book_id, start=1).status_code, 200)
        self.assertEqual(self.reader(book_id, version="0" * 64).status_code, 409)

    def test_authorization_cache_privacy_shared_annotations_and_csrf(self):
        private, _, _, _ = self.book()
        shared, _, _, _ = self.book(owner=BUILTIN_OWNER)
        self.assertEqual(self.reader(private).status_code, 200)
        anonymous = self.app.test_client()
        for book_id in (private, shared):
            self.assertEqual(self.reader(book_id, client=anonymous).status_code, 401)
            self.assertEqual(self.annotations(book_id, client=anonymous).status_code, 401)
        self.assertEqual(self.reader(private, client=self.b).status_code, 404)
        self.assertEqual(self.annotations(private, client=self.b).status_code, 404)
        first = self.create_annotation(shared).get_json()["annotation"]
        second_response = self.create_annotation(shared, client=self.b, csrf=self.b_csrf)
        self.assertEqual(second_response.status_code, 201)
        second = second_response.get_json()["annotation"]
        self.assertEqual(self.annotations(shared).get_json()["annotations"], [first])
        self.assertEqual(self.annotations(shared, client=self.b).get_json()["annotations"], [second])
        self.assertNotIn("owner_id", first)
        self.assertNotIn("book_id", first)
        self.assertEqual(set(first), {"id", "version", "start", "end", "quote", "note", "color", "created_at", "updated_at"})
        item = f"/api/books/{shared}/annotations/{first['id']}"
        for method, path, payload in (("post", f"/api/books/{shared}/annotations", {}),
                                      ("patch", item, {"note": "edit"}), ("delete", item, None)):
            self.assertEqual(self.a.open(path, method=method, json=payload).status_code, 403)
            self.assertEqual(self.a.open(path, method=method, json=payload,
                                         headers={"X-CSRF-Token": "wrong"}).status_code, 403)
        for method in ("patch", "delete"):
            self.assertEqual(self.b.open(item, method=method, json={"note": "edit"},
                                         headers={"X-CSRF-Token": self.b_csrf}).status_code, 404)
        token = anonymous.get("/api/auth/me").get_json()["csrf_token"]
        self.assertEqual(anonymous.post(f"/api/books/{shared}/annotations", json={},
                                        headers={"X-CSRF-Token": token}).status_code, 401)
        self.assertEqual(self.b.post(f"/api/books/{private}/annotations", json={key: first[key] for key in
                                                                                ("version", "start", "end", "quote")},
                                     headers={"X-CSRF-Token": self.b_csrf}).status_code, 404)

    def test_annotation_crud_order_and_static_anchor(self):
        book_id, _, _, _ = self.book()
        data = self.reader(book_id).get_json()
        text = data["blocks"][0]["text"]
        later = self.create_annotation(book_id, {"version": data["version"], "start": 20, "end": 30,
                                                  "quote": text[20:30]}).get_json()["annotation"]
        first_response = self.create_annotation(book_id)
        self.assertEqual(first_response.status_code, 201)
        first = first_response.get_json()["annotation"]
        self.assertEqual((first["note"], first["color"]), ("", "yellow"))
        self.assertEqual(first["created_at"], first["updated_at"])
        duplicate = self.create_annotation(book_id).get_json()["annotation"]
        rows = self.annotations(book_id).get_json()["annotations"]
        self.assertEqual([row["id"] for row in rows], sorted([first["id"], duplicate["id"]]) + [later["id"]])
        item = f"/api/books/{book_id}/annotations/{first['id']}"
        edited = self.a.patch(item, json={"note": "  <b>private note</b>  ", "color": "blue"},
                               headers={"X-CSRF-Token": self.a_csrf}).get_json()["annotation"]
        for key in ("id", "version", "start", "end", "quote", "created_at"):
            self.assertEqual(edited[key], first[key])
        self.assertEqual(edited["note"], "  <b>private note</b>  ")
        self.assertEqual(edited["color"], "blue")
        again = self.a.patch(item, json={"color": "green"}, headers={"X-CSRF-Token": self.a_csrf})
        self.assertEqual(again.get_json()["annotation"]["note"], edited["note"])
        cleared = self.a.patch(item, json={"note": ""}, headers={"X-CSRF-Token": self.a_csrf})
        self.assertEqual(cleared.get_json()["annotation"]["color"], "green")
        self.assertEqual(self.a.delete(item, headers={"X-CSRF-Token": self.a_csrf}).get_json(), {"ok": True})
        self.assertEqual(self.a.delete(item, headers={"X-CSRF-Token": self.a_csrf}).status_code, 404)
        self.assertEqual(self.a.patch(item, json={"note": "gone"}, headers={"X-CSRF-Token": self.a_csrf}).status_code, 404)
        other, _, _, _ = self.book()
        foreign_item = f"/api/books/{other}/annotations/{later['id']}"
        self.assertEqual(self.a.patch(foreign_item, json={"note": "wrong book"},
                                      headers={"X-CSRF-Token": self.a_csrf}).status_code, 404)
        self.assertEqual(self.a.delete(foreign_item, headers={"X-CSRF-Token": self.a_csrf}).status_code, 404)

    def test_annotation_validation_lengths_types_and_unicode(self):
        book_id, _, _, _ = self.book("a" + ASTRAL * 2200 + " text " * 100, filename="unicode.txt")
        data = self.reader(book_id).get_json()
        valid = {"version": data["version"], "start": 1, "end": 3, "quote": ASTRAL}
        bad = [{"start": value} for value in (True, False, 1.0, "1", None, -1)]
        bad += [{"end": value} for value in (True, 3.0, "3", None, -1, 1, data["length"] + 1)]
        bad += [{"start": 2}, {"end": 2}, {"quote": ""}, {"quote": " "}, {"quote": "other"},
                {"quote": None}, {"quote": []}, {"quote": "\ud800"}, {"quote": "\x00"},
                {"version": None}, {"version": 1}, {"version": "bad"},
                {"note": None}, {"note": []}, {"note": True}, {"note": "\udfff"}, {"note": "\x00"},
                {"note": "x" * 4001}, {"note": ASTRAL * 2001},
                {"color": "red"}, {"color": []}, {"color": True}, {"owner_id": self.b_id},
                {"end": 4003, "quote": ASTRAL * 2001}]
        for changes in bad:
            with self.subTest(changes=changes):
                self.assertEqual(self.create_annotation(book_id, dict(valid, **changes)).status_code, 400)
        self.assertEqual(self.create_annotation(book_id, dict(valid, version="0" * 64)).status_code, 409)
        created = self.create_annotation(book_id, dict(valid, note=ASTRAL * 2000))
        self.assertEqual(created.status_code, 201)
        item = f"/api/books/{book_id}/annotations/{created.get_json()['annotation']['id']}"
        for payload in ({}, {"start": 0}, {"end": 4}, {"quote": "other"}, {"version": data["version"]},
                        {"note": None}, {"color": []}, {"color": "bad"}, {"note": "x" * 4001}):
            self.assertEqual(self.a.patch(item, json=payload, headers={"X-CSRF-Token": self.a_csrf}).status_code, 400)
        for payload in ([], "string", 7):
            self.assertEqual(self.a.post(f"/api/books/{book_id}/annotations", json=payload,
                                         headers={"X-CSRF-Token": self.a_csrf}).status_code, 400)
        oversized = self.a.post(f"/api/books/{book_id}/annotations", data="x" * 32769,
                                 content_type="application/json", headers={"X-CSRF-Token": self.a_csrf})
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(self.create_annotation(book_id, dict(valid, end=4001, quote=ASTRAL * 2000)).status_code, 201)

    def test_source_changes_reject_old_versions_and_preserve_old_annotations(self):
        book_id, path, _, ids = self.book()
        old = self.reader(book_id).get_json()
        annotation = self.create_annotation(book_id).get_json()["annotation"]
        self.rewrite(path, "# Changed\n\n" + "Replacement source content. " * 400)
        for params in ({"version": old["version"]}, {"start": 1, "version": old["version"]},
                       {"at": 10, "version": old["version"]}, {"anchor": ids[0]}):
            self.assertEqual(self.reader(book_id, **params).status_code, 409)
        payload = {key: annotation[key] for key in ("version", "start", "end", "quote")}
        self.assertEqual(self.create_annotation(book_id, payload).status_code, 409)
        current = self.reader(book_id).get_json()
        self.assertNotEqual(current["version"], old["version"])
        self.assertEqual(self.annotations(book_id).get_json()["annotations"], [annotation])
        item = f"/api/books/{book_id}/annotations/{annotation['id']}"
        with self.db.connect() as db:
            db.execute("UPDATE books SET status='error' WHERE id=?", (book_id,))
        self.assertEqual(self.reader(book_id).status_code, 409)
        self.assertEqual(self.annotations(book_id).status_code, 200)
        self.assertEqual(self.a.patch(item, json={"note": "old quote"},
                                      headers={"X-CSRF-Token": self.a_csrf}).status_code, 200)
        self.assertEqual(self.a.delete(item, headers={"X-CSRF-Token": self.a_csrf}).status_code, 200)
        self.assertEqual(self.create_annotation(book_id, {"version": current["version"], "start": 0,
                                                           "end": 7, "quote": "Changed"}).status_code, 201)

    def test_reindex_keeps_version_annotations_and_new_anchor_location(self):
        book_id, _, _, ids = self.book()
        before = self.reader(book_id, anchor=ids[1]).get_json()
        annotation = self.create_annotation(book_id).get_json()["annotation"]
        response = self.a.post(f"/api/books/{book_id}/reindex", json={}, headers={"X-CSRF-Token": self.a_csrf})
        self.assertEqual(response.status_code, 202)
        with self.db.connect() as db:
            new_id = db.execute("SELECT id FROM chunks WHERE book_id=? AND ordinal=2", (book_id,)).fetchone()[0]
        self.assertNotEqual(new_id, ids[1])
        after = self.reader(book_id, anchor=new_id, version=before["version"]).get_json()
        self.assertEqual(after["version"], before["version"])
        self.assertEqual(after["anchor"], before["anchor"])
        self.assertEqual(self.annotations(book_id).get_json()["annotations"], [annotation])
        self.assertEqual(self.reader(book_id, anchor=ids[1]).status_code, 404)
        other, _, _, other_ids = self.book()
        self.assertEqual(self.reader(book_id, anchor=other_ids[0]).status_code, 404)
        self.assertEqual(self.reader(other, anchor=new_id).status_code, 404)

    def test_chunk_mismatch_rejected_even_when_cache_is_warm(self):
        book_id, _, _, ids = self.book()
        self.reader(book_id)
        for column, value in (("text", "wrong text"), ("section", "wrong section"), ("page", 99), ("ordinal", 999)):
            with self.db.connect() as db:
                old = db.execute(f"SELECT {column} FROM chunks WHERE id=?", (ids[0],)).fetchone()[0]
                db.execute(f"UPDATE chunks SET {column}=? WHERE id=?", (value, ids[0]))
            self.assertEqual(self.reader(book_id, anchor=ids[0]).status_code, 409)
            with self.db.connect() as db:
                db.execute(f"UPDATE chunks SET {column}=? WHERE id=?", (old, ids[0]))

    def test_missing_source_and_not_ready_still_allow_annotation_management(self):
        book_id, path, _, _ = self.book()
        annotation = self.create_annotation(book_id).get_json()["annotation"]
        path.unlink()
        response = self.reader(book_id)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(str(self.root), response.get_data(as_text=True))
        self.assertNotIn(path.name, response.get_data(as_text=True))
        payload = {key: annotation[key] for key in ("version", "start", "end", "quote")}
        self.assertEqual(self.create_annotation(book_id, payload).status_code, 404)
        for status in ("queued", "indexing", "error"):
            with self.db.connect() as db:
                db.execute("UPDATE books SET status=? WHERE id=?", (status, book_id))
            self.assertEqual(self.reader(book_id).status_code, 409)
            self.assertEqual(self.annotations(book_id).get_json()["annotations"], [annotation])
        item = f"/api/books/{book_id}/annotations/{annotation['id']}"
        self.assertEqual(self.a.patch(item, json={"color": "green"}, headers={"X-CSRF-Token": self.a_csrf}).status_code, 200)
        self.assertEqual(self.a.delete(item, headers={"X-CSRF-Token": self.a_csrf}).status_code, 200)
        self.assertEqual(self.reader("missing").status_code, 404)
        self.assertEqual(self.annotations("missing").status_code, 404)

    def test_parser_failure_never_exposes_internal_paths(self):
        book_id, path, _, _ = self.book()
        path.write_bytes(b"\xff\xfe\x00")
        response = self.reader(book_id)
        self.assertEqual(response.status_code, 409)
        self.assertNotIn(str(path), response.get_data(as_text=True))

    def test_book_lock_rejects_reader_annotation_reindex_and_delete(self):
        book_id, _, _, _ = self.book()
        annotation = self.create_annotation(book_id).get_json()["annotation"]
        payload = {key: annotation[key] for key in ("version", "start", "end", "quote")}
        item = f"/api/books/{book_id}/annotations/{annotation['id']}"
        lock = self.book_lock_for(book_id)
        with lock:
            self.assertEqual(self.reader(book_id).status_code, 409)
            self.assertEqual(self.annotations(book_id).status_code, 409)
            self.assertEqual(self.create_annotation(book_id, payload).status_code, 409)
            self.assertEqual(self.a.patch(item, json={"note": "busy"}, headers={"X-CSRF-Token": self.a_csrf}).status_code, 409)
            self.assertEqual(self.a.delete(item, headers={"X-CSRF-Token": self.a_csrf}).status_code, 409)
            self.assertEqual(self.a.delete(f"/api/books/{book_id}", headers={"X-CSRF-Token": self.a_csrf}).status_code, 409)
            self.assertEqual(self.a.post(f"/api/books/{book_id}/reindex", json={},
                                         headers={"X-CSRF-Token": self.a_csrf}).status_code, 409)
            self.assertEqual(self.reader(book_id, client=self.b).status_code, 404)
        self.assertEqual(self.reader(book_id).status_code, 200)
        shared, _, _, _ = self.book(owner=BUILTIN_OWNER)
        with self.book_lock_for(shared):
            self.assertEqual(self.reader(shared).status_code, 409)
            self.assertEqual(self.annotations(shared).status_code, 409)

    def test_reader_holds_existing_book_lock_while_parsing(self):
        book_id, _, _, _ = self.book()

        def parse_under_lock(path, filename):
            self.assertTrue(self.book_lock_for(book_id).locked())
            self.assertEqual(self.a.delete(f"/api/books/{book_id}", headers={"X-CSRF-Token": self.a_csrf}).status_code, 409)
            return parse_document(path, filename)

        with patch("study.reader.parse_document", side_effect=parse_under_lock):
            self.assertEqual(self.reader(book_id).status_code, 200)
        self.assertFalse(self.book_lock_for(book_id).locked())

    def test_source_mutation_during_parse_and_write_is_rejected_atomically(self):
        book_id, path, _, _ = self.book()

        def mutate_during_parse(source, filename):
            sections = parse_document(source, filename)
            self.rewrite(source, "# Other\n" + "Updated text. " * 300)
            return sections

        with patch("study.reader.parse_document", side_effect=mutate_during_parse):
            self.assertEqual(self.reader(book_id).status_code, 409)
        data = self.reader(book_id).get_json()
        checks = []

        def mutate_before_commit(source, stamp):
            self.assertTrue(self.book_lock_for(book_id).locked())
            checks.append(stamp)
            if len(checks) == 2:
                self.rewrite(source, "# Again\n" + "Different content. " * 300)
            check_source(source, stamp)

        with patch("study.reader.check_source", side_effect=mutate_before_commit):
            result = self.create_annotation(book_id, {"version": data["version"], "start": 0, "end": 5, "quote": "Other"})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.annotations(book_id).get_json()["annotations"], [])
        self.assertFalse(self.book_lock_for(book_id).locked())

    def test_annotation_quota_is_per_user_per_book_and_transactional(self):
        book_id, _, _, _ = self.book(owner=BUILTIN_OWNER)
        annotation = self.create_annotation(book_id).get_json()["annotation"]
        with self.db.connect() as db:
            db.executemany("INSERT INTO annotations(id,book_id,owner_id,version,start,end,quote,note,color,created_at,updated_at) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           [(uid(), book_id, self.a_id, annotation["version"], 0, 1, "C", "", "yellow", now(), now())
                            for _ in range(498)])
        self.assertEqual(self.create_annotation(book_id).status_code, 201)
        self.assertEqual(self.create_annotation(book_id).status_code, 400)
        self.assertEqual(len(self.annotations(book_id).get_json()["annotations"]), 500)
        self.assertEqual(self.create_annotation(book_id, client=self.b, csrf=self.b_csrf).status_code, 201)
        other, _, _, _ = self.book()
        self.assertEqual(self.create_annotation(other).status_code, 201)
        with self.db.connect() as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_annotation_writes_are_rate_limited(self):
        book_id, _, _, _ = self.book()
        annotation = self.create_annotation(book_id).get_json()["annotation"]
        item = f"/api/books/{book_id}/annotations/{annotation['id']}"
        for _ in range(119):
            response = self.a.patch(item, json={"color": "blue"}, headers={"X-CSRF-Token": self.a_csrf})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.a.patch(item, json={"note": "limited"},
                                      headers={"X-CSRF-Token": self.a_csrf}).status_code, 429)
        self.assertEqual(self.annotations(book_id).status_code, 200)

    def test_book_and_annotation_owner_deletion_cascade(self):
        shared, _, _, _ = self.book(owner=BUILTIN_OWNER)
        first = self.create_annotation(shared).get_json()["annotation"]
        second = self.create_annotation(shared, client=self.b, csrf=self.b_csrf).get_json()["annotation"]
        with self.db.connect() as db:
            db.execute("DELETE FROM users WHERE id=?", (self.b_id,))
            self.assertIsNone(db.execute("SELECT id FROM annotations WHERE id=?", (second["id"],)).fetchone())
            self.assertIsNotNone(db.execute("SELECT id FROM annotations WHERE id=?", (first["id"],)).fetchone())
        private, path, _, _ = self.book()
        own = self.create_annotation(private).get_json()["annotation"]
        self.assertEqual(self.a.delete(f"/api/books/{private}", headers={"X-CSRF-Token": self.a_csrf}).status_code, 200)
        self.assertFalse(path.exists())
        self.assertEqual(self.reader(private).status_code, 404)
        with self.db.connect() as db:
            self.assertIsNone(db.execute("SELECT id FROM annotations WHERE id=?", (own["id"],)).fetchone())
            db.execute("DELETE FROM books WHERE id=?", (shared,))
            self.assertEqual(db.execute("SELECT count(*) FROM annotations").fetchone()[0], 0)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_cache_is_bounded_and_checks_filename_stat_and_auth(self):
        cache = SnapshotCache()
        rows = []
        for index in range(CACHE_ENTRIES + 1):
            book_id, _, _, _ = self.book("# Heading\n" + f"Unique {index}. " * 100)
            with self.db.connect() as db:
                rows.append(db.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone())
        for row in rows:
            cache.get(row, self.root)
        self.assertEqual(len(cache.entries), CACHE_ENTRIES)
        self.assertLessEqual(cache.weight, CACHE_BYTES)
        self.assertNotIn(rows[0]["id"], {key[0] for key in cache.entries})
        snapshot, path, _ = cache.get(rows[-1], self.root)
        same, _, _ = cache.get(rows[-1], self.root)
        self.assertIs(snapshot, same)
        filename_changed = dict(rows[-1], filename="plain.txt")
        reparsed, _, _ = cache.get(filename_changed, self.root)
        self.assertNotEqual(snapshot.version, reparsed.version)
        self.rewrite(path, "# Heading\n" + "New source. " * 100)
        updated, _, _ = cache.get(rows[-1], self.root)
        self.assertNotEqual(snapshot.version, updated.version)
        self.assertEqual(sum(key[0] == rows[-1]["id"] for key in cache.entries), 1)
        with patch("study.reader.CACHE_BYTES", 1):
            tiny = SnapshotCache()
            tiny.get(rows[-1], self.root)
            self.assertEqual(len(tiny.entries), 0)
        with patch("study.reader.parse_document", wraps=parse_document) as parser:
            self.assertEqual(self.reader(rows[0]["id"]).status_code, 200)
            self.assertEqual(self.reader(rows[0]["id"]).status_code, 200)
            self.assertEqual(parser.call_count, 1)
            self.assertEqual(self.reader(rows[0]["id"], client=self.b).status_code, 404)
            self.assertEqual(parser.call_count, 1)

    def test_builtin_seed_uses_explicit_user_columns_and_locks_source_updates(self):
        with self.db.connect() as db:
            db.execute("DELETE FROM users WHERE id=?", (BUILTIN_OWNER,))
        folder = self.root / "builtin_books"
        folder.mkdir()
        baked = folder / "Shipped.md"
        baked.write_text("# Shipped\n" + "Original shared textbook. " * 100, encoding="utf-8", newline="\n")
        self.seed_builtin_books()
        with self.db.connect() as db:
            user = db.execute("SELECT api_key FROM users WHERE id=?", (BUILTIN_OWNER,)).fetchone()
            row = db.execute("SELECT * FROM books WHERE owner_id=?", (BUILTIN_OWNER,)).fetchone()
        self.assertIsNotNone(user)
        self.assertEqual(user["api_key"], "")
        self.assertEqual(row["status"], "ready")
        source = self.root / row["source_path"]
        original = source.read_text(encoding="utf-8")
        annotation = self.create_annotation(row["id"]).get_json()["annotation"]
        baked.write_text("# Updated\n" + "Replacement shared textbook. " * 100, encoding="utf-8", newline="\n")
        # The startup seeder must wait out a busy reader lock, not skip the
        # update until the next boot; run it on a real thread while held.
        failures = []

        def seed_in_thread():
            try:
                self.seed_builtin_books()
            except Exception as error:
                failures.append(error)

        with self.book_lock_for(row["id"]):
            contender = REAL_THREAD(target=seed_in_thread, daemon=True)
            contender.start()
            self.assertIsNone(contender.join(0.2))
            self.assertEqual(source.read_text(encoding="utf-8"), original)
        contender.join(10)
        self.assertFalse(contender.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(source.read_text(encoding="utf-8"), baked.read_text(encoding="utf-8"))
        self.assertEqual(self.annotations(row["id"]).get_json()["annotations"], [annotation])
        self.assertNotEqual(self.reader(row["id"]).get_json()["version"], annotation["version"])
        baked.rename(folder / "Shipped.txt")
        self.seed_builtin_books()
        with self.db.connect() as db:
            filename = db.execute("SELECT filename FROM books WHERE id=?", (row["id"],)).fetchone()[0]
        self.assertEqual(filename, "Shipped.txt")
        self.assertTrue(self.reader(row["id"]).get_json()["blocks"][0]["text"].startswith("# Updated"))
        self.assertEqual(self.annotations(row["id"]).get_json()["annotations"], [annotation])


class ReaderMigrationTests(unittest.TestCase):
    def test_legacy_users_gain_annotations_and_repeated_migration_preserves_them(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            legacy = sqlite3.connect(root / "study.sqlite3")
            legacy.execute("CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL, "
                           "username_key TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, created_at TEXT NOT NULL)")
            legacy.execute("INSERT INTO users VALUES('system','system','system','unused','2026-01-01')")
            legacy.execute("INSERT INTO users VALUES('reader','reader','reader','unused','2026-01-01')")
            legacy.commit()
            legacy.close()
            database = Database(root)
            with database.connect() as db:
                db.execute("INSERT INTO books(id,owner_id,title,filename,source_path,status,created_at) "
                           "VALUES('book','system','Shared','shared.txt','shared.txt','ready','2026-01-01')")
                db.execute("INSERT INTO annotations(id,book_id,owner_id,version,start,end,quote,note,color,created_at,updated_at) "
                           "VALUES('annotation','book','reader',?,0,1,'a','','yellow','2026-01-01','2026-01-01')", ("a" * 64,))
            for _ in range(2):
                database = Database(root)
                with database.connect() as db:
                    row = db.execute("SELECT * FROM annotations WHERE id='annotation'").fetchone()
                    self.assertEqual(row["owner_id"], "reader")
                    self.assertEqual(row["book_id"], "book")
                    self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertEqual(db.execute("SELECT api_key FROM users WHERE id='reader'").fetchone()[0], "")
                    foreign_keys = {(row[2], row[3], row[6]) for row in db.execute("PRAGMA foreign_key_list(annotations)")}
                    self.assertEqual(foreign_keys, {("users", "owner_id", "CASCADE"), ("books", "book_id", "CASCADE")})
            with database.connect() as db:
                db.execute("DELETE FROM users WHERE id='reader'")
                self.assertEqual(db.execute("SELECT count(*) FROM annotations").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
