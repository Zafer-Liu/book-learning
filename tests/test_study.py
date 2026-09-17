"""Offline regression cases. Run explicitly with unittest; no real model calls."""

import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from study.app import create_app, now, uid
from study.documents import parse_document, split_sections
from study.rag import (EmbeddingClient, api_url, index_tokens, normalize, retrieve,
                       semantic_sentence_ranges, split_sentences)
from study.tutor import Tutor, TutorError, _visible_stream_text, parse_model_json, validate_answer


class DocumentTests(unittest.TestCase):
    def test_chapters_and_overlap_do_not_cross(self):
        sections = [{"section": "A", "text": "甲" * 1800, "page": None},
                    {"section": "B", "text": "乙" * 1500, "page": None}]
        chunks = split_sections(sections)
        self.assertEqual([row["ordinal"] for row in chunks], list(range(1, len(chunks) + 1)))
        self.assertTrue(all(len(row["text"]) <= 1200 for row in chunks))
        self.assertTrue(all("乙" not in row["text"] for row in chunks if row["section"] == "A"))
        self.assertTrue(all(row["page"] is None for row in chunks))
        self.assertEqual(chunks[0]["text"][-160:], chunks[1]["text"][:160])

    def test_markdown_frontmatter_headings_and_fences(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "book.md"
            path.write_text("---\nsecret: frontmatter\n---\n# 第一章\n" + "行政诉讼法律关系的正文。" * 10 +
                            "\n## 管辖\n管辖章节的正文说明。\n```python\n# Not a heading\n```\n"
                            "第二章\n======\n第二章的正文说明。\n![scan](page.png)", encoding="utf-8")
            sections = parse_document(path, path.name)
            self.assertEqual([s["section"] for s in sections], ["第一章", "第一章 / 管辖", "第二章"])
            self.assertNotIn("frontmatter", "".join(s["text"] for s in sections))
            self.assertNotIn("page.png", "".join(s["text"] for s in sections))
            self.assertIn("# Not a heading", sections[1]["text"])

    def test_image_only_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "scan.md"
            path.write_text("![scan](page.png)\n" * 100, encoding="utf-8")
            with self.assertRaises(ValueError):
                parse_document(path, path.name)

    def test_invalid_encoding_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "book.txt"
            path.write_bytes(b"\xff\xfe\x00bad")
            with self.assertRaises(ValueError):
                parse_document(path, path.name)


class RetrievalTests(unittest.TestCase):
    def client(self):
        client = EmbeddingClient()
        client.configured = False
        return client

    def chunk(self, chunk_id=1, text="行政诉讼管辖的概念和管辖制度。", space=None, vector=None):
        return {"id": chunk_id, "text": text, "section": "第一章", "ordinal": chunk_id,
                "page": None, "embedding_space": space, "embedding": vector}

    def test_keyword_fallback_accepts_single_channel(self):
        result = retrieve("管辖", [self.chunk()], [], self.client())
        self.assertEqual([row["id"] for row in result["hits"]], [1])
        self.assertTrue(result["degraded"])
        self.assertEqual(result["backend"], "lexical+fts5")

    def test_fts_cannot_inject_out_of_scope_chunk(self):
        result = retrieve("管辖", [self.chunk()], [999, 1], self.client())
        self.assertEqual([row["id"] for row in result["hits"]], [1])

    def test_unrelated_query_returns_no_evidence(self):
        result = retrieve("photosynthesis", [self.chunk()], [], self.client())
        self.assertEqual(result["hits"], [])

    def test_equal_dimension_different_space_not_compared(self):
        client = self.client()
        client.configured = True
        with patch.object(client, "embed_texts", return_value=([[1.0] * 16], "new-model")):
            result = retrieve("photosynthesis", [self.chunk(space="old-model", vector=[1.0] * 16)], [], client)
        self.assertTrue(result["degraded"])
        self.assertEqual(result["hits"], [])

    def test_embedding_outage_is_explicit_fallback(self):
        client = self.client()
        client.configured = True
        with patch.object(client, "embed_texts", side_effect=ValueError("offline")):
            result = retrieve("管辖", [self.chunk(space="model", vector=[1.0] * 16)], [1], client)
        self.assertTrue(result["degraded"])
        self.assertEqual(len(result["hits"]), 1)

    def test_vector_validation_and_url_contract(self):
        self.assertEqual(len(normalize([1.0] * 16)), 16)
        for vector in ([0.0] * 16, [float("nan")] * 16, [1.0] * 3):
            with self.assertRaises(ValueError):
                normalize(vector)
        self.assertEqual(api_url("https://example.test/v1/", "embeddings"), "https://example.test/v1/embeddings")
        self.assertEqual(api_url("https://example.test/api/paas/v4", "chat/completions"),
                         "https://example.test/api/paas/v4/chat/completions")
        self.assertEqual(api_url("https://example.test", "chat/completions"),
                         "https://example.test/v1/chat/completions")
        with self.assertRaises(ValueError):
            api_url("http://remote.example/v1", "embeddings")

    def test_missing_and_unknown_citations_are_rejected(self):
        for citations in ([], ["C9"], None):
            with self.assertRaises(ValueError):
                validate_answer({"paragraphs": [{"text": "answer", "citations": citations}]}, {"C1"}, "qa")
        paragraphs, quiz, used = validate_answer({"paragraphs": [{"text": "answer", "citations": ["C1"]}]}, {"C1"}, "qa")
        self.assertEqual(used, {"C1"})
        self.assertEqual(len(paragraphs), 1)
        self.assertEqual(quiz, [])

    def test_inline_citation_markers_are_extracted_and_kept(self):
        result = {"paragraphs": [{"text": "第一句有依据[C1]。第二句另有依据[C2][C3]。", "citations": []}]}
        paragraphs, _, used = validate_answer(result, {"C1", "C2", "C3"}, "qa")
        self.assertEqual(paragraphs[0]["citations"], ["C1", "C2", "C3"])
        self.assertIn("[C1]", paragraphs[0]["text"])
        self.assertIn("[C3]", paragraphs[0]["text"])
        self.assertEqual(used, {"C1", "C2", "C3"})

    def test_inline_marker_precedence_and_unknown_rejection(self):
        paragraphs, _, used = validate_answer(
            {"paragraphs": [{"text": "句子[C2]。", "citations": ["C1"]}]}, {"C1", "C2"}, "qa")
        self.assertEqual(paragraphs[0]["citations"], ["C2"])
        self.assertEqual(used, {"C2"})
        with self.assertRaises(ValueError):
            validate_answer({"paragraphs": [{"text": "句子[C9]。", "citations": ["C1"]}]}, {"C1"}, "qa")

    def test_quiz_inline_markers_are_stripped_from_text(self):
        _, quiz, used = validate_answer(
            {"paragraphs": [], "quiz": [{"question": "题目[C1]？", "answer": "答案", "explanation": "解析[C1]", "citations": []}]},
            {"C1"}, "quiz")
        self.assertEqual(quiz[0]["question"], "题目？")
        self.assertEqual(quiz[0]["explanation"], "解析")
        self.assertEqual(quiz[0]["citations"], ["C1"])
        self.assertEqual(used, {"C1"})

    def test_stream_preview_strips_inline_markers(self):
        # The live SSE preview shows plain sentences; markers only render as
        # buttons in the final message. Partial (unterminated) text streams too.
        complete = '{"paragraphs":[{"text":"第一句[C1]。第二句[C2]。","citations":["C1","C2"]}],"quiz":[]}'
        partial = '{"paragraphs":[{"text":"第一句[C1]。第二句[C2]。还在'
        self.assertEqual(_visible_stream_text(complete), "第一句。第二句。")
        self.assertEqual(_visible_stream_text(partial), "第一句。第二句。还在")

    def test_split_sentences_partitions_and_merges_fragments(self):
        text = ("管辖制度解决法院之间的分工问题。\n\n"
                "一、有犯罪事实。\n\n"
                "犯罪事实必须有一定证据加以证明，且数量足以认定犯罪事实的存在。")
        spans = split_sentences(text)
        # Spans partition the whole text without gaps or overlaps.
        self.assertEqual("".join(text[start:end] for start, end in spans), text)
        self.assertTrue(all(end - start >= 10 for start, end in spans))
        # A too-short fragment ("一、有犯罪事实。" is 8 chars) plus the blank
        # line behind its period forms one 10-char highlightable span, because
        # the punctuation and the newlines are a single break run.
        self.assertIn("一、有犯罪事实。\n\n", [text[start:end] for start, end in spans])

    def test_semantic_sentence_ranges_select_and_fallback(self):
        text = ("管辖制度解决法院之间的分工与权限划分。\n\n"
                "立案必须同时具备犯罪事实和追诉条件。\n\n"
                "回避制度保障审判活动的公正进行。")
        spans = split_sentences(text)
        self.assertEqual(len(spans), 3)
        # 2-dim unit vectors: the query matches sentence 2, not 1 or 3.
        vectors = [[1.0, 0.0], [0.2, 0.98], [1.0, 0.0], [0.25, 0.97]]

        class FakeEmbedder:
            configured = True
            calls = []

            def embed_texts(self, texts):
                self.calls.append(list(texts))
                return vectors, "fingerprint"

        embedder = FakeEmbedder()
        self.assertEqual(semantic_sentence_ranges(embedder, "问题", text), [spans[1]])
        self.assertEqual(embedder.calls[0][0], "问题")
        # Unrelated question (best score below threshold): nothing highlights.
        weak = [[1.0, 0.0], [0.2, 0.98], [0.3, 0.95], [0.25, 0.97]]
        embedder2 = type("E2", (), {"configured": True,
                                     "embed_texts": lambda self, texts: (weak, "fp")})()
        self.assertEqual(semantic_sentence_ranges(embedder2, "无关", text), [])
        # Unconfigured embedder and service outage both degrade to [].
        off = type("E3", (), {"configured": False, "embed_texts": None})()
        self.assertEqual(semantic_sentence_ranges(off, "问题", text), [])

        class Broken:
            configured = True

            def embed_texts(self, texts):
                raise ValueError("向量服务暂不可用")

        self.assertEqual(semantic_sentence_ranges(Broken(), "问题", text), [])

    def test_parse_model_json_plain_fenced_and_reasoning_outputs(self):
        answer = {"paragraphs": [{"text": "回答", "citations": ["C1"]}], "quiz": []}
        self.assertEqual(parse_model_json(json.dumps(answer, ensure_ascii=False)), answer)
        self.assertEqual(parse_model_json("```json\n" + json.dumps(answer) + "\n```"), answer)
        reasoning = "先分析证据……最终答案如下：\n" + json.dumps(answer, ensure_ascii=False) + "\n以上完毕"
        self.assertEqual(parse_model_json(reasoning), answer)
        with self.assertRaises(ValueError):
            parse_model_json("无法给出结构化回答")

    def test_parse_model_json_repairs_unescaped_inner_quotes(self):
        raw = ('先分析证据，级别管辖确定的是"哪个部门"主管事务。\n\n'
               '{"paragraphs":[{"text":"级别管辖确定的是"哪个部门"主管某类事务；'
               '上级行政机关把事务"委托"给下级。","citations":["C3"]}],"quiz":[]}')
        result = parse_model_json(raw)
        self.assertEqual(result["paragraphs"][0]["citations"], ["C3"])
        self.assertIn("哪个部门", result["paragraphs"][0]["text"])
        self.assertIn("委托", result["paragraphs"][0]["text"])
        with self.assertRaises(ValueError):
            parse_model_json('{"paragraphs": [truncated')

    def test_no_evidence_never_calls_llm(self):
        tutor = Tutor()
        with patch("study.tutor.requests.post") as post:
            self.assertFalse(tutor.generate("question", "qa", "book", [], [], {})["grounded"])
            post.assert_not_called()

    def test_embedding_credentials_do_not_cross_endpoints(self):
        with patch.dict(os.environ, {"STUDY_EMBED_BASE_URL": "https://new.example/v1",
                                     "STUDY_EMBED_API_KEY": "", "BAA_CLOUD_EMBED_TOKEN": "old-token"}):
            self.assertEqual(EmbeddingClient().key, "")
        with patch.dict(os.environ, {"STUDY_EMBED_BASE_URL": "", "BAA_CLOUD_EMBED_URL": "https://old.example",
                                     "BAA_CLOUD_EMBED_TOKEN": "old-token", "BAA_CLOUD_EMBED_MODEL": "old-model"}):
            client = EmbeddingClient()
            self.assertEqual(client.key, "old-token")
            self.assertEqual(client.model, "old-model")


class LlmFallbackTests(unittest.TestCase):
    ANSWER = '{"paragraphs":[{"text":"依据教材回答[C1]。","citations":["C1"]}],"quiz":[]}'
    HIT = {"id": 1, "text": "管辖制度。", "section": "第一章", "ordinal": 1, "page": None}

    def setUp(self):
        patcher = patch.dict(os.environ, {
            "STUDY_LLM_BASE_URL": "https://primary.test/v1", "STUDY_LLM_API_KEY": "k1",
            "STUDY_LLM_MODEL": "primary",
            "STUDY_LLM_FALLBACK_BASE_URL": "https://fallback.test/v1",
            "STUDY_LLM_FALLBACK_API_KEY": "k2", "STUDY_LLM_FALLBACK_MODEL": "fallback"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tutor = Tutor()

    @staticmethod
    def one_shot(content):
        class Fake:
            text = ""
            def raise_for_status(self): pass
            def json(self): return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
            def __enter__(self): return self
            def __exit__(self, *args): return False
        return Fake()

    @staticmethod
    def stream(lines):
        class Fake:
            def raise_for_status(self): pass
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def iter_lines(self):
                yield from lines
        return Fake()

    @staticmethod
    def sse_chunk(text, finish=None):
        choice = {"delta": {"content": text}}
        if finish:
            choice["finish_reason"] = finish
        return ("data: " + json.dumps({"choices": [choice]}, ensure_ascii=False)).encode()

    def test_one_shot_switches_to_fallback(self):
        calls = []

        def post(url, **kwargs):
            calls.append(url)
            if "primary" in url:
                raise requests.ConnectionError("quota")
            return self.one_shot(self.ANSWER)

        with patch("study.tutor.requests.post", side_effect=post):
            result = self.tutor.generate("问题", "qa", "教材", [self.HIT], [], {})
        self.assertTrue(result["grounded"])
        self.assertEqual(calls, ["https://primary.test/v1/chat/completions",
                                 "https://fallback.test/v1/chat/completions"])

    def test_stream_switches_before_first_delta(self):
        half = len(self.ANSWER) // 2

        def post(url, **kwargs):
            if "primary" in url:
                raise requests.ConnectionError("down")
            return self.stream([self.sse_chunk(self.ANSWER[:half]),
                                self.sse_chunk(self.ANSWER[half:], finish="stop"),
                                b"data: [DONE]"])

        events = []
        with patch("study.tutor.requests.post", side_effect=post):
            for event in self.tutor.generate_stream("问题", "qa", "教材", [self.HIT], [], {}):
                events.append(event)
        self.assertEqual(events[-1][0], "result")
        self.assertTrue(events[-1][1]["grounded"])
        self.assertTrue(any(event[0] == "delta" for event in events))

    def test_stream_failure_after_delta_does_not_switch(self):
        head = self.ANSWER[:80]  # contains "paragraphs" plus visible text

        class Flaky:
            def raise_for_status(self): pass
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def iter_lines(self):
                yield self.__class__.chunk
                raise requests.ConnectionError("mid-stream")
        Flaky.chunk = self.sse_chunk(head)

        with patch("study.tutor.requests.post", return_value=Flaky()) as post:
            with self.assertRaises(TutorError):
                for _ in self.tutor.generate_stream("问题", "qa", "教材", [self.HIT], [], {}):
                    pass
        self.assertEqual(post.call_count, 1)

    def test_without_fallback_the_error_propagates(self):
        os.environ.pop("STUDY_LLM_FALLBACK_BASE_URL")
        os.environ.pop("STUDY_LLM_FALLBACK_MODEL")
        tutor = Tutor()
        self.assertEqual(len(tutor.providers), 1)
        with patch("study.tutor.requests.post", side_effect=requests.ConnectionError("down")):
            with self.assertRaises(TutorError):
                tutor.generate("问题", "qa", "教材", [self.HIT], [], {})


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = create_app({"TESTING": True, "DATA_ROOT": self.root, "SECRET_KEY": "unit-test-secret-" * 4,
                               "SESSION_COOKIE_SECURE": False, "REGISTRATION_OPEN": True, "INVITE_CODE": ""})
        self.db = self.app.extensions["database"]
        self.app.extensions["embedder"].configured = False
        self.a, self.a_id, self.a_csrf = self.register("student_a")
        self.b, self.b_id, self.b_csrf = self.register("student_b")
        self.book_a, self.chunk_a = self.seed_book(self.a_id, "教材甲", "行政诉讼管辖制度采用甲教材观点。")
        self.book_b, self.chunk_b = self.seed_book(self.a_id, "教材乙", "行政诉讼管辖制度采用乙教材观点。")

    def tearDown(self):
        self.app.extensions["stats_stop"].set()
        self.app.extensions["seed_thread"].join(timeout=300)
        self.app.extensions["index_executor"].shutdown(wait=True)
        self.temp.cleanup()

    def register(self, username):
        client = self.app.test_client()
        token = client.get("/api/auth/me").get_json()["csrf_token"]
        response = client.post("/api/auth/register", json={"username": username, "password": "test-password-long"},
                               headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        return client, data["user"]["id"], data["csrf_token"]

    def seed_book(self, owner, title, text):
        book_id = uid()
        source = self.root / (book_id + ".md")
        source.write_text(text * 4, encoding="utf-8")
        with self.db.connect() as db:
            db.execute("INSERT INTO books(id,owner_id,title,filename,source_path,status,created_at,chunk_count,section_count) VALUES(?,?,?,?,?,'ready',?,1,1)",
                       (book_id, owner, title, title + ".md", source.name, now()))
            chunk_id = db.execute("INSERT INTO chunks(owner_id,book_id,ordinal,section,text) VALUES(?,?,1,'第一章',?)",
                                  (owner, book_id, text)).lastrowid
            db.execute("INSERT INTO chunks_fts(rowid,tokens) VALUES(?,?)", (chunk_id, index_tokens(text)))
        return book_id, chunk_id

    def conversation(self, book):
        response = self.a.post(f"/api/books/{book}/conversations", json={}, headers={"X-CSRF-Token": self.a_csrf})
        self.assertEqual(response.status_code, 201)
        return response.get_json()["conversation"]["id"]

    def test_auth_and_csrf_are_required(self):
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get("/api/books").status_code, 401)
        self.assertEqual(self.a.post("/api/books", data={}).status_code, 403)
        self.assertEqual(self.a.post("/api/auth/logout", json={}, headers={"X-CSRF-Token": "é"}).status_code, 403)

    def test_foreign_account_cannot_access_book_paths(self):
        paths = [f"/api/books/{self.book_a}", f"/api/books/{self.book_a}/source",
                 f"/api/books/{self.book_a}/chunks/{self.chunk_a}", f"/api/books/{self.book_a}/conversations"]
        for path in paths:
            self.assertEqual(self.b.get(path).status_code, 404, path)
        # Builtin books are shared, but student_b must see nothing else.
        foreign = self.b.get("/api/books").get_json()["books"]
        self.assertTrue(all(book["builtin"] for book in foreign))
        self.assertNotIn(self.book_a, {book["id"] for book in foreign})
        self.assertEqual(self.b.delete(f"/api/books/{self.book_a}", headers={"X-CSRF-Token": self.b_csrf}).status_code, 404)

    def test_conversations_and_chunks_cannot_cross_books(self):
        cid = self.conversation(self.book_a)
        self.assertEqual(self.a.get(f"/api/books/{self.book_b}/conversations/{cid}").status_code, 404)
        self.assertEqual(self.a.get(f"/api/books/{self.book_b}/chunks/{self.chunk_a}").status_code, 404)
        response = self.a.post(f"/api/books/{self.book_b}/conversations/{cid}/messages",
                               json={"message": "管辖"}, headers={"X-CSRF-Token": self.a_csrf})
        self.assertEqual(response.status_code, 404)

    def test_chunk_detail_exposes_adjacent_chunks_for_browsing(self):
        # A lone chunk sits at both document edges: no neighbours either side.
        data = self.a.get(f"/api/books/{self.book_a}/chunks/{self.chunk_a}").get_json()
        self.assertIsNone(data["prev"])
        self.assertIsNone(data["next"])
        # Insert surrounding chunks; ordinals, not row ids, define reading order.
        with self.db.connect() as db:
            before = db.execute("INSERT INTO chunks(owner_id,book_id,ordinal,section,text) VALUES(?,?,0,'第一章','前文')",
                                (self.a_id, self.book_a)).lastrowid
            after = db.execute("INSERT INTO chunks(owner_id,book_id,ordinal,section,text) VALUES(?,?,2,'第二章','后文')",
                               (self.a_id, self.book_a)).lastrowid
        data = self.a.get(f"/api/books/{self.book_a}/chunks/{self.chunk_a}").get_json()
        self.assertEqual((data["prev"]["id"], data["prev"]["ordinal"]), (before, 0))
        self.assertEqual((data["next"]["id"], data["next"]["ordinal"]), (after, 2))
        # Neighbours never leak across books or accounts.
        self.assertEqual(self.b.get(f"/api/books/{self.book_a}/chunks/{self.chunk_a}").status_code, 404)

    def test_conversation_delete_cascades_and_stays_scoped(self):
        first = self.conversation(self.book_a)
        second = self.conversation(self.book_a)
        with self.db.connect() as db:
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                       ("m-del", self.a_id, self.book_a, first, "user", "问题", "qa", "{}", now()))
        # A foreign account cannot delete someone else's conversation.
        self.assertEqual(self.b.delete(f"/api/books/{self.book_a}/conversations/{second}",
                                       headers={"X-CSRF-Token": self.b_csrf}).status_code, 404)
        self.assertEqual(self.a.delete(f"/api/books/{self.book_a}/conversations/{first}",
                                       headers={"X-CSRF-Token": self.a_csrf}).status_code, 200)
        with self.db.connect() as db:
            self.assertIsNone(db.execute("SELECT id FROM messages WHERE conversation_id=?", (first,)).fetchone())
            self.assertIsNotNone(db.execute("SELECT id FROM conversations WHERE id=?", (second,)).fetchone())
        self.assertEqual(self.a.get(f"/api/books/{self.book_a}/conversations/{first}").status_code, 404)
        # Deleting the same conversation twice is a 404, not an error leak.
        self.assertEqual(self.a.delete(f"/api/books/{self.book_a}/conversations/{first}",
                                       headers={"X-CSRF-Token": self.a_csrf}).status_code, 404)

    def test_answer_feedback_scopes_toggles_and_logs(self):
        conversation = self.conversation(self.book_a)
        with self.db.connect() as db:
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                       ("q1", self.a_id, self.book_a, conversation, "user", "什么是管辖", "qa", "{}", "2026-01-01T00:00:00"))
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                       ("a1", self.a_id, self.book_a, conversation, "assistant", "回答", "qa",
                        json.dumps({"content": "回答"}), "2026-01-01T00:00:01"))
        path = f"/api/books/{self.book_a}/conversations/{conversation}/messages/a1/feedback"
        # Guards: anonymous 401, foreign account 404, invalid rating 400,
        # and user-side messages cannot be rated.
        anonymous = self.app.test_client()
        token = anonymous.get("/api/auth/me").get_json()["csrf_token"]
        self.assertEqual(anonymous.post(path, json={"rating": 1},
                                        headers={"X-CSRF-Token": token}).status_code, 401)
        self.assertEqual(self.b.post(path, json={"rating": 1},
                                     headers={"X-CSRF-Token": self.b_csrf}).status_code, 404)
        self.assertEqual(self.a.post(path, json={"rating": 5},
                                     headers={"X-CSRF-Token": self.a_csrf}).status_code, 400)
        user_path = f"/api/books/{self.book_a}/conversations/{conversation}/messages/q1/feedback"
        self.assertEqual(self.a.post(user_path, json={"rating": 1},
                                     headers={"X-CSRF-Token": self.a_csrf}).status_code, 400)
        # Rate, flip, and clear; history always carries the current verdict.
        base = {"X-CSRF-Token": self.a_csrf}
        self.assertEqual(self.a.post(path, json={"rating": 1}, headers=base).get_json()["feedback"], 1)
        self.assertEqual(self.a.post(path, json={"rating": -1}, headers=base).get_json()["feedback"], -1)
        self.assertIsNone(self.a.post(path, json={"rating": 0}, headers=base).get_json()["feedback"])
        self.a.post(path, json={"rating": 1}, headers=base)
        messages = self.a.get(f"/api/books/{self.book_a}/conversations/{conversation}").get_json()["messages"]
        ratings = {row["id"]: row["feedback"] for row in messages}
        self.assertEqual(ratings["a1"], 1)
        self.assertIsNone(ratings["q1"])
        # The verdict lands in the activity log with the triggering question.
        with self.db.connect() as db:
            row = db.execute("SELECT detail FROM app_logs WHERE event='answer_feedback' "
                             "ORDER BY id DESC").fetchone()
        self.assertIn("满意", row["detail"])
        self.assertIn("管辖", row["detail"])

    def test_feedback_reasons_archive_and_period_stats(self):
        conversation = self.conversation(self.book_a)
        with self.db.connect() as db:
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                       ("q2", self.a_id, self.book_a, conversation, "user", "什么是回避", "qa", "{}", "2026-01-01T00:00:00"))
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                       ("a2", self.a_id, self.book_a, conversation, "assistant", "回答二", "qa",
                        json.dumps({"content": "回答二"}), "2026-01-01T00:00:01"))
        path = f"/api/books/{self.book_a}/conversations/{conversation}/messages/a2/feedback"
        base = {"X-CSRF-Token": self.a_csrf}
        # A dislike carries its reason; an upvote discards any supplied reason.
        self.assertEqual(self.a.post(path, json={"rating": -1, "reason": " 太简略 "}, headers=base)
                         .get_json()["feedback"], -1)
        self.assertEqual(self.a.post(path, json={"rating": 1, "reason": "应该忽略"}, headers=base)
                         .get_json()["feedback"], 1)
        with self.db.connect() as db:
            row = db.execute("SELECT rating, reason FROM feedback WHERE message_id='a2'").fetchone()
            archive = db.execute("SELECT rating, reason, question FROM feedback_archive ORDER BY id").fetchall()
        self.assertEqual((row["rating"], row["reason"]), (1, ""))
        self.assertEqual([(item["rating"], item["reason"]) for item in archive], [(-1, "太简略"), (1, "")])
        self.assertEqual(archive[0]["question"], "什么是回避")
        # Backdate the clicks by 4 days so exactly one period has elapsed.
        from datetime import datetime, timedelta, timezone
        stamp = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
        with self.db.connect() as db:
            db.execute("UPDATE feedback_archive SET created_at=?", (stamp,))
        self.assertGreaterEqual(self.app.extensions["refresh_feedback_stats"](), 1)
        with self.db.connect() as db:
            stat = db.execute("SELECT up, down, clears, reasons, questions FROM feedback_stats").fetchone()
            log = db.execute("SELECT detail FROM app_logs WHERE event='feedback_stats' ORDER BY id DESC").fetchone()
        self.assertEqual((stat["up"], stat["down"], stat["clears"]), (1, 1, 0))
        self.assertIn("太简略", stat["reasons"])
        self.assertIn("什么是回避", stat["questions"])
        self.assertIn("满意 1 · 不满意 1", log["detail"])
        # Refreshing again must not duplicate the closed period.
        self.assertEqual(self.app.extensions["refresh_feedback_stats"](), 0)

    def test_chunk_match_scopes_and_degrades(self):
        path = f"/api/books/{self.book_a}/chunks/{self.chunk_a}/match"
        # Auth, scope and validation guards mirror the chunk detail endpoint.
        anonymous = self.app.test_client()
        token = anonymous.get("/api/auth/me").get_json()["csrf_token"]
        self.assertEqual(anonymous.post(path, json={"question": "管辖"},
                                        headers={"X-CSRF-Token": token}).status_code, 401)
        self.assertEqual(self.b.post(path, json={"question": "管辖"},
                                     headers={"X-CSRF-Token": self.b_csrf}).status_code, 404)
        self.assertEqual(self.a.post(path, json={"question": ""},
                                     headers={"X-CSRF-Token": self.a_csrf}).status_code, 400)
        # An unconfigured embedder degrades to empty ranges, never an error.
        response = self.a.post(path, json={"question": "管辖"},
                               headers={"X-CSRF-Token": self.a_csrf})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["ranges"], [])
        # With a working embedder the matching sentence range comes back.
        embedder = self.app.extensions["embedder"]
        vectors = [[1.0, 0.0], [1.0, 0.0]]
        with patch.object(embedder, "configured", True), \
                patch.object(embedder, "embed_texts", return_value=(vectors, "fp")):
            response = self.a.post(path, json={"question": "管辖"},
                                   headers={"X-CSRF-Token": self.a_csrf})
        self.assertEqual(response.get_json()["ranges"], [[0, 16]])

    def test_retrieval_and_generation_only_receive_selected_book(self):
        cid = self.conversation(self.book_a)
        seen = []

        def fake_generate(question, mode, title, hits, previous, retrieval):
            seen.extend(hits)
            return {"content": "测试回答", "paragraphs": [], "quiz": [], "citations": [],
                    "grounded": False, "retrieval": retrieval}

        def fake_stream(question, mode, title, hits, previous, retrieval):
            # Same contract as the real stream: record the evidence, then emit
            # one final answer without touching the model.
            seen.extend(hits)
            yield ("result", {"content": "测试回答", "paragraphs": [], "quiz": [], "citations": [],
                              "grounded": False, "retrieval": retrieval})

        with patch.object(self.app.extensions["tutor"], "generate", side_effect=fake_generate), \
                patch.object(self.app.extensions["tutor"], "generate_stream", side_effect=fake_stream):
            response = self.a.post(f"/api/books/{self.book_a}/conversations/{cid}/messages",
                                   json={"message": "管辖"}, headers={"X-CSRF-Token": self.a_csrf})
            # The SSE body must be read while the patch is active: streamed
            # responses are lazy, the generator only runs when consumed.
            body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('"type": "answer"', body)
        self.assertEqual({hit["id"] for hit in seen}, {self.chunk_a})
        self.assertTrue(all("乙教材" not in hit["text"] for hit in seen))
        history = self.a.get(f"/api/books/{self.book_a}/conversations/{cid}").get_json()["messages"]
        self.assertEqual([message["role"] for message in history], ["user", "assistant"])

    def test_invalid_section_and_mode_are_rejected(self):
        cid = self.conversation(self.book_a)
        for payload in ({"message": "管辖", "section": "不存在的章节"}, {"message": "管辖", "mode": []}):
            response = self.a.post(f"/api/books/{self.book_a}/conversations/{cid}/messages", json=payload,
                                   headers={"X-CSRF-Token": self.a_csrf})
            self.assertEqual(response.status_code, 400)

    def test_delete_cascades_and_chunk_ids_never_recur(self):
        self.conversation(self.book_a)
        self.assertEqual(self.a.delete(f"/api/books/{self.book_a}", headers={"X-CSRF-Token": self.a_csrf}).status_code, 200)
        with self.db.connect() as db:
            self.assertIsNone(db.execute("SELECT rowid FROM chunks_fts WHERE rowid=?", (self.chunk_a,)).fetchone())
            self.assertIsNone(db.execute("SELECT id FROM conversations WHERE book_id=?", (self.book_a,)).fetchone())
            maximum = db.execute("SELECT max(id) FROM chunks").fetchone()[0]
            db.execute("DELETE FROM books WHERE owner_id=?", (self.a_id,))
        _, new_chunk = self.seed_book(self.a_id, "新教材", "新教材的正文内容。")
        self.assertGreater(new_chunk, maximum)

    def test_database_rejects_mismatched_owner_foreign_key(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.connect() as db:
                db.execute("INSERT INTO chunks(owner_id,book_id,ordinal,section,text) VALUES(?,?,8,'bad','bad')", (self.b_id, self.book_a))

    def test_upload_rejects_pdf_and_unsafe_extensions(self):
        response = self.a.post("/api/books", data={"file": (io.BytesIO(b"not a document"), "book.pdf")},
                               headers={"X-CSRF-Token": self.a_csrf})
        self.assertEqual(response.status_code, 400)

    def test_read_requests_do_not_refresh_login_cookie(self):
        self.assertNotIn("Set-Cookie", self.a.get("/api/books").headers)
        self.assertEqual(self.a.post("/api/auth/logout", json={}, headers={"X-CSRF-Token": self.a_csrf}).status_code, 200)
        self.assertEqual(self.a.get("/api/books").status_code, 401)

    def test_upload_indexes_markdown_larger_than_parser_buffer(self):
        content = ("# 管辖\n\n" + "行政诉讼管辖制度的教材正文。" * 10000).encode("utf-8")
        with patch.object(self.app.extensions["index_executor"], "submit", side_effect=lambda fn, *args: fn(*args)):
            response = self.a.post("/api/books", data={"file": (io.BytesIO(content), "large.md")},
                                   headers={"X-CSRF-Token": self.a_csrf})
        self.assertEqual(response.status_code, 202)
        book_id = response.get_json()["book"]["id"]
        detail = self.a.get(f"/api/books/{book_id}").get_json()
        self.assertEqual(detail["book"]["status"], "ready")
        self.assertGreater(detail["book"]["chunk_count"], 1)
        self.assertEqual(self.b.get(f"/api/books/{book_id}").status_code, 404)

    def test_reindex_invalidates_conversations_and_old_citations(self):
        cid = self.conversation(self.book_a)
        with patch.object(self.app.extensions["index_executor"], "submit", side_effect=lambda fn, *args: fn(*args)):
            response = self.a.post(f"/api/books/{self.book_a}/reindex", json={}, headers={"X-CSRF-Token": self.a_csrf})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.a.get(f"/api/books/{self.book_a}").get_json()["book"]["status"], "ready")
        self.assertEqual(self.a.get(f"/api/books/{self.book_a}/conversations/{cid}").status_code, 404)
        self.assertEqual(self.a.get(f"/api/books/{self.book_a}/chunks/{self.chunk_a}").status_code, 404)


class MigrationTests(unittest.TestCase):
    def test_legacy_schema_migrates_and_preserves_history(self):
        # Reproduce the pre-builtin schema (composite foreign keys), then boot
        # the app on it: the shared-books migration must keep every row.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            legacy = sqlite3.connect(root / "study.sqlite3")
            legacy.executescript("""
                CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL, username_key TEXT NOT NULL UNIQUE,
                                    password_hash TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE books (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id),
                                    title TEXT NOT NULL, filename TEXT NOT NULL, source_path TEXT NOT NULL,
                                    status TEXT NOT NULL DEFAULT 'queued', error TEXT NOT NULL DEFAULT '',
                                    chunk_count INTEGER NOT NULL DEFAULT 0, section_count INTEGER NOT NULL DEFAULT 0,
                                    index_backend TEXT NOT NULL DEFAULT 'lexical', created_at TEXT NOT NULL,
                                    UNIQUE(owner_id, id));
                CREATE TABLE chunks (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id TEXT NOT NULL, book_id TEXT NOT NULL,
                                     ordinal INTEGER NOT NULL, section TEXT NOT NULL, page INTEGER, text TEXT NOT NULL,
                                     embedding TEXT, embedding_space TEXT,
                                     FOREIGN KEY(owner_id, book_id) REFERENCES books(owner_id, id) ON DELETE CASCADE,
                                     UNIQUE(book_id, ordinal));
                CREATE TABLE conversations (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, book_id TEXT NOT NULL,
                                            title TEXT NOT NULL, created_at TEXT NOT NULL,
                                            FOREIGN KEY(owner_id, book_id) REFERENCES books(owner_id, id) ON DELETE CASCADE,
                                            UNIQUE(owner_id, book_id, id));
                CREATE TABLE messages (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, book_id TEXT NOT NULL,
                                       conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
                                       mode TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
                                       FOREIGN KEY(owner_id, book_id, conversation_id)
                                       REFERENCES conversations(owner_id, book_id, id) ON DELETE CASCADE);
                CREATE TABLE app_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                                       owner_id TEXT NOT NULL DEFAULT '', level TEXT NOT NULL,
                                       event TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '');
            """)
            legacy.execute("INSERT INTO users VALUES('u1','legacy','legacy','hash','2026-01-01')")
            legacy.execute("INSERT INTO books(id,owner_id,title,filename,source_path,status,created_at) "
                           "VALUES('b1','u1','旧书','旧书.md','旧书.md','ready','2026-01-01')")
            legacy.execute("INSERT INTO chunks(owner_id,book_id,ordinal,section,text) VALUES('u1','b1',1,'第一章','旧内容')")
            legacy.execute("INSERT INTO conversations VALUES('c1','u1','b1','旧对话','2026-01-01')")
            legacy.execute("INSERT INTO messages VALUES('m1','u1','b1','c1','user','旧问题','qa','{}','2026-01-01')")
            legacy.commit()
            legacy.close()

            app = create_app({"TESTING": True, "DATA_ROOT": root, "SECRET_KEY": "unit-test-secret-" * 4,
                              "SESSION_COOKIE_SECURE": False, "REGISTRATION_OPEN": True, "INVITE_CODE": ""})
            try:
                database = app.extensions["database"]
                # Run builtin seeding synchronously so cleanup never races it.
                with patch.object(app.extensions["index_executor"], "submit",
                                  side_effect=lambda fn, *args: fn(*args)):
                    deadline = time.monotonic() + 60
                    while time.monotonic() < deadline:
                        with database.connect() as db:
                            rows = db.execute("SELECT status FROM books WHERE owner_id='builtin'").fetchall()
                        if len(rows) >= 2 and all(row["status"] == "ready" for row in rows):
                            break
                        time.sleep(0.05)
                with database.connect() as db:
                    sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='conversations'").fetchone()["sql"]
                    self.assertNotIn("REFERENCES books(owner_id, id)", sql)
                    self.assertEqual(db.execute("SELECT count(*) FROM conversations WHERE id='c1'").fetchone()[0], 1)
                    self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE id='m1'").fetchone()[0], 1)
                    # Deleting the book must still cascade through the new schema.
                    db.execute("DELETE FROM books WHERE id='b1'")
                    self.assertEqual(db.execute("SELECT count(*) FROM conversations WHERE owner_id='u1'").fetchone()[0], 0)
                    self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE owner_id='u1'").fetchone()[0], 0)
            finally:
                app.extensions["stats_stop"].set()
                app.extensions["seed_thread"].join(timeout=300)
                app.extensions["index_executor"].shutdown(wait=True)


class TestCodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = create_app({"TESTING": True, "DATA_ROOT": self.root, "SECRET_KEY": "unit-test-secret-" * 4,
                               "SESSION_COOKIE_SECURE": False, "REGISTRATION_OPEN": False, "INVITE_CODE": "",
                               "TEST_CODES": ("CODEALPHA1", "CODEBETA22")})
        self.db = self.app.extensions["database"]
        self.app.extensions["embedder"].configured = False

    def tearDown(self):
        self.app.extensions["stats_stop"].set()
        self.app.extensions["seed_thread"].join(timeout=300)
        self.app.extensions["index_executor"].shutdown(wait=True)
        self.temp.cleanup()

    def register(self, username, test_code="", password="test-password-long"):
        client = self.app.test_client()
        token = client.get("/api/auth/me").get_json()["csrf_token"]
        response = client.post("/api/auth/register",
                               json={"username": username, "password": password, "test_code": test_code},
                               headers={"X-CSRF-Token": token})
        return client, response

    def test_me_exposes_open_codes(self):
        data = self.app.test_client().get("/api/auth/me").get_json()
        self.assertFalse(data["registration_open"])
        self.assertTrue(data["test_code_registration"])

    def test_code_registers_binds_and_login_follows(self):
        client, response = self.register("tester_a", "codealpha1")  # lowercase input normalises
        self.assertEqual(response.status_code, 200)
        user_id = response.get_json()["user"]["id"]
        with self.db.connect() as db:
            row = db.execute("SELECT * FROM test_codes WHERE code='CODEALPHA1'").fetchone()
        self.assertEqual(row["bound_username"], "tester_a")
        self.assertEqual(row["bound_user_id"], user_id)
        self.assertTrue(row["bound_at"])
        login = client.post("/api/auth/login", json={"username": "tester_a", "password": "test-password-long"},
                            headers={"X-CSRF-Token": response.get_json()["csrf_token"]})
        self.assertEqual(login.status_code, 200)

    def test_wrong_reused_and_missing_codes_are_rejected(self):
        _, wrong = self.register("tester_x", "NOSUCHCODE0")
        self.assertEqual(wrong.status_code, 403)
        _, no_code = self.register("tester_y")
        self.assertEqual(no_code.status_code, 403)  # closed registration without a code
        _, first = self.register("tester_z", "CODEALPHA1")
        self.assertEqual(first.status_code, 200)
        _, reused = self.register("tester_w", "CODEALPHA1")
        self.assertEqual(reused.status_code, 403)

    def test_duplicate_username_keeps_code_unbound(self):
        self.register("tester_a", "CODEALPHA1")
        _, clash = self.register("tester_A", "CODEBETA22")  # same casefolded key
        self.assertEqual(clash.status_code, 409)
        with self.db.connect() as db:
            row = db.execute("SELECT bound_username FROM test_codes WHERE code='CODEBETA22'").fetchone()
        self.assertEqual(row["bound_username"], "")

    def test_all_codes_bound_closes_registration_entry(self):
        for index, code in enumerate(("CODEALPHA1", "CODEBETA22")):
            _, response = self.register(f"taker{index}", code)
            self.assertEqual(response.status_code, 200)
        data = self.app.test_client().get("/api/auth/me").get_json()
        self.assertFalse(data["test_code_registration"])

    def test_beta_accounts_retire_and_seeding_is_idempotent(self):
        from werkzeug.security import generate_password_hash
        _, response = self.register("early_user", "CODEALPHA1")
        self.assertEqual(response.status_code, 200)
        with self.db.connect() as db:
            db.execute("INSERT INTO users VALUES('beta01id','beta01','beta01',?,?)",
                       (generate_password_hash("x" * 20), now()))
            book_id = uid()
            db.execute("INSERT INTO books(id,owner_id,title,filename,source_path,status,created_at) "
                       "VALUES(?,?,?,?,?,'ready',?)", (book_id, "beta01id", "owned", "owned.md", "owned.md", now()))
            db.execute("INSERT INTO conversations VALUES('c1','beta01id',?,?,?)",
                       (book_id, "chat", now()))
        # Re-run create_app over the same data root: codes reseed, beta01 is
        # gone, and the code bound before the restart stays bound.
        app2 = create_app({"TESTING": True, "DATA_ROOT": self.root, "SECRET_KEY": "unit-test-secret-" * 4,
                           "SESSION_COOKIE_SECURE": False, "REGISTRATION_OPEN": False, "INVITE_CODE": "",
                           "TEST_CODES": ("CODEALPHA1", "CODEBETA22")})
        try:
            db2 = app2.extensions["database"]
            with db2.connect() as db:
                self.assertIsNone(db.execute("SELECT 1 FROM users WHERE username_key='beta01'").fetchone())
                self.assertIsNone(db.execute("SELECT 1 FROM conversations WHERE owner_id='beta01id'").fetchone())
                self.assertIsNone(db.execute("SELECT 1 FROM books WHERE owner_id='beta01id'").fetchone())
                self.assertEqual(db.execute("SELECT count(*) FROM test_codes").fetchone()[0], 2)
                bound = db.execute("SELECT bound_username FROM test_codes WHERE code='CODEALPHA1'").fetchone()
                self.assertEqual(bound["bound_username"], "early_user")
        finally:
            app2.extensions["stats_stop"].set()
            app2.extensions["seed_thread"].join(timeout=300)
            app2.extensions["index_executor"].shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
