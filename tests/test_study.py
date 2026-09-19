"""Offline regression cases. Run explicitly with unittest; no real model calls."""

import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import requests

from study.app import create_app, now, uid
from study.compaction import (
    COMPACT_THRESHOLD_CHARS, KEEP_RECENT_PAIRS, compact_conversation,
    compaction_circuit_open, context_usage, record_compaction_result,
    should_compact, split_for_summary, strip_citations,
)
from study.documents import parse_document, split_sections
from study.rag import (EmbeddingClient, api_url, index_tokens, normalize, retrieve,
                       semantic_sentence_ranges, split_sentences)
from study.tutor import (AGENT_MAX_CALLS, AGENT_MAX_ROUNDS, AGENT_MAX_WEB_CALLS,
                         Tutor, TutorError, _visible_stream_text, parse_model_json,
                         validate_answer)


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

    @staticmethod
    def build_docx(paragraphs, styles=None):
        W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        body = "".join(
            f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
            for text in paragraphs)
        styles_xml = ""
        if styles:
            entries = "".join(
                f'<w:style w:type="paragraph" w:styleId="{sid}">'
                f'<w:name w:val="{name}"/></w:style>'
                for sid, name in styles)
            styles_xml = f'<?xml version="1.0"?><w:styles xmlns:w="{W}">{entries}</w:styles>'
        document = (f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body>{body}'
                    f'<w:sectPr/></w:body></w:document>')
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", document)
            if styles_xml:
                archive.writestr("word/styles.xml", styles_xml)
        return buffer.getvalue()

    def test_docx_paragraphs_and_heading_heuristics(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "book.docx"
            path.write_bytes(self.build_docx([
                "第一章 合同的订立", "当事人订立合同应当遵循诚信原则，秉持诚实，恪守承诺。",
                "1.1 要约与承诺", "要约是希望与他人订立合同的意思表示。要约应当内容具体确定。",
                "第2章 合同的效力 25", "合同效力章节的正文说明，用于验证页码页眉不提升标题。",
            ]))
            sections = parse_document(path, path.name)
            # _squeeze drops spaces between CJK characters ("第一章 合同" ->
            # "第一章合同"); the space before digits ("效力 25") survives.
            self.assertEqual([s["section"] for s in sections],
                             ["第一章合同的订立", "第一章合同的订立 / 1.1 要约与承诺"])
            joined = "".join(s["text"] for s in sections)
            self.assertIn("要约是希望与他人订立合同", joined)
            self.assertIn("第2章合同的效力 25", joined)

    def test_broken_docx_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "broken.docx"
            path.write_bytes(b"not a zip at all")
            with self.assertRaises(ValueError):
                parse_document(path, path.name)
            # A zip without word/document.xml is also rejected.
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("unrelated.txt", "hello")
            path.write_bytes(buffer.getvalue())
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


class AgentToolTests(unittest.TestCase):
    """The agent loop: tool_call assembly, scoped searches, stable labels."""
    HIT = {"id": 1, "text": "管辖制度。", "section": "第一章", "ordinal": 1, "page": None}
    HIT2 = {"id": 2, "text": "级别管辖分工。", "section": "第二章", "ordinal": 2, "page": 12}
    ANSWER1 = '{"paragraphs":[{"text":"依据教材回答[C1]。","citations":["C1"]}],"quiz":[]}'
    ANSWER2 = '{"paragraphs":[{"text":"管辖制度依法确定[C1]，分工另有依据[C2]。","citations":["C2","C1"]}],"quiz":[]}'

    def setUp(self):
        patcher = patch.dict(os.environ, {
            "STUDY_LLM_BASE_URL": "https://primary.test/v1", "STUDY_LLM_API_KEY": "k1",
            "STUDY_LLM_MODEL": "primary"})
        patcher.start()
        self.addCleanup(patcher.stop)
        # A developer .env may define a fallback provider; the default cases
        # here assume a single provider so failures propagate directly.
        for key in [key for key in os.environ if key.startswith("STUDY_LLM_FALLBACK_")]:
            os.environ.pop(key)
        self.tutor = Tutor()
        self.assertEqual(len(self.tutor.providers), 1)

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

    @staticmethod
    def sse_tool_chunk(index, call_id, name, arguments, finish=None):
        entry = {"index": index, "function": {}}
        if call_id:
            entry["id"] = call_id
        if name:
            entry["function"]["name"] = name
        if arguments:
            entry["function"]["arguments"] = arguments
        choice = {"delta": {"tool_calls": [entry]}}
        if finish:
            choice["finish_reason"] = finish
        return ("data: " + json.dumps({"choices": [choice]}, ensure_ascii=False)).encode()

    def test_agent_searches_then_answers_with_stable_labels(self):
        searches, requests_out = [], []

        def search(query, limit=6):
            searches.append((query, limit))
            # The second search overlaps the first (repeat + new chunk).
            return [self.HIT] if len(searches) == 1 else [self.HIT2, self.HIT]

        def post(url, **kwargs):
            requests_out.append(kwargs["json"])
            if len(requests_out) == 1:
                return self.stream([
                    self.sse_chunk("让我先检索教材。"),  # narration, never a preview
                    self.sse_tool_chunk(0, "call_1", "search_book", '{"query": "管辖'),
                    self.sse_tool_chunk(0, None, None, '制度", "limit": 3}', finish="tool_calls"),
                    b"data: [DONE]"])
            if len(requests_out) == 2:
                return self.stream([
                    self.sse_tool_chunk(0, "call_2", "search_book", '{"query": "级别管辖"}',
                                        finish="tool_calls"),
                    b"data: [DONE]"])
            return self.stream([self.sse_chunk(self.ANSWER2, finish="stop"), b"data: [DONE]"])

        events = []
        with patch("study.tutor.requests.post", side_effect=post):
            for event in self.tutor.agent_stream("什么是管辖制度", "qa", "教材", search, [], {}):
                events.append(event)
        kinds = [kind for kind, _ in events]
        # The executed searches used the assembled tool arguments.
        self.assertEqual(searches, [("管辖制度", 3), ("级别管辖", 6)])
        self.assertEqual(kinds.count("search"), 2)
        self.assertLess(kinds.index("search"), kinds.index("result"))
        # Only the final round's JSON streams; tool-round narration does not.
        self.assertEqual(kinds.count("delta"), 1)
        # The second search round carries the assistant tool_calls plus a tool
        # reply whose labels stay stable across overlapping results.
        followup = requests_out[2]["messages"]
        self.assertEqual(followup[2]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(followup[3]["tool_call_id"], "call_1")
        self.assertEqual(followup[5]["tool_call_id"], "call_2")
        self.assertEqual([row["label"] for row in json.loads(followup[5]["content"])["results"]],
                         ["C2", "C1"])
        result = events[-1][1]
        self.assertTrue(result["grounded"])
        self.assertEqual(result["retrieval"]["evidence"], 2)
        self.assertEqual([ref["chunk_id"] for ref in result["citations"]], [1, 2])
        self.assertEqual([ref["label"] for ref in result["citations"]], ["C1", "C2"])

    def test_agent_auto_rescue_when_model_skips_search(self):
        requests_out = []

        def search(query, limit=6):
            return [self.HIT]

        def post(url, **kwargs):
            requests_out.append(kwargs["json"])
            return self.stream([self.sse_chunk(self.ANSWER1, finish="stop"), b"data: [DONE]"])

        events = []
        with patch("study.tutor.requests.post", side_effect=post):
            for event in self.tutor.agent_stream("管辖", "qa", "教材", search, [], {}):
                events.append(event)
        infos = [value for kind, value in events if kind == "search"]
        self.assertTrue(infos and infos[0].get("auto"))
        self.assertEqual(len(requests_out), 2)
        # The auto round attaches evidence as a user message, not a tool reply.
        self.assertIn("自动检索", requests_out[1]["messages"][2]["content"])
        self.assertTrue(events[-1][1]["grounded"])

    def test_agent_round_budget_exhaustion_raises(self):
        def search(query, limit=6):
            return [self.HIT]

        def post(url, **kwargs):
            return self.stream([
                self.sse_tool_chunk(0, None, "search_book", '{"query": "再搜"}', finish="tool_calls"),
                b"data: [DONE]"])

        with patch("study.tutor.requests.post", side_effect=post) as post_mock:
            with self.assertRaises(TutorError):
                for _ in self.tutor.agent_stream("问题", "qa", "教材", search, [], {}):
                    pass
        self.assertEqual(post_mock.call_count, AGENT_MAX_ROUNDS)

    def test_agent_call_cap_rejects_extra_searches(self):
        executed = []

        def search(query, limit=6):
            executed.append(query)
            return [self.HIT]

        def post(url, **kwargs):
            return self.stream([
                self.sse_tool_chunk(0, None, "search_book", '{"query": "a"}'),
                self.sse_tool_chunk(1, None, "search_book", '{"query": "b"}', finish="tool_calls"),
                b"data: [DONE]"])

        with patch("study.tutor.requests.post", side_effect=post) as post_mock:
            with self.assertRaises(TutorError):
                for _ in self.tutor.agent_stream("问题", "qa", "教材", search, [], {}):
                    pass
        self.assertEqual(post_mock.call_count, AGENT_MAX_ROUNDS)
        # Beyond the cap the tool is rejected without executing the search.
        self.assertEqual(len(executed), AGENT_MAX_CALLS)

    def test_agent_rejects_unknown_tool_and_invalid_arguments(self):
        searches = []

        def search(query, limit=6):
            searches.append(query)
            return [self.HIT]

        requests_out = []

        def post(url, **kwargs):
            requests_out.append(kwargs["json"])
            if len(requests_out) == 1:
                return self.stream([
                    self.sse_tool_chunk(0, "x1", "delete_book", "{}"),
                    self.sse_tool_chunk(1, "x2", "search_book", "not-json", finish="tool_calls"),
                    b"data: [DONE]"])
            if len(requests_out) == 2:
                return self.stream([
                    self.sse_tool_chunk(0, "x3", "search_book", '{"query": "管辖"}', finish="tool_calls"),
                    b"data: [DONE]"])
            return self.stream([self.sse_chunk(self.ANSWER1, finish="stop"), b"data: [DONE]"])

        events = []
        with patch("study.tutor.requests.post", side_effect=post):
            for event in self.tutor.agent_stream("问题", "qa", "教材", search, [], {}):
                events.append(event)
        # The rejected calls surface as error statuses; the loop continues.
        infos = [value for kind, value in events if kind == "search"]
        self.assertEqual(sum(bool(info.get("error")) for info in infos), 2)
        self.assertEqual(searches, ["管辖"])
        replies = [json.loads(requests_out[1]["messages"][index]["content"]) for index in (3, 4)]
        self.assertIn("error", replies[0])
        self.assertIn("error", replies[1])
        self.assertTrue(events[-1][1]["grounded"])

    def test_agent_switches_provider_before_first_delta(self):
        with patch.dict(os.environ, {
                "STUDY_LLM_FALLBACK_BASE_URL": "https://fallback.test/v1",
                "STUDY_LLM_FALLBACK_API_KEY": "k2", "STUDY_LLM_FALLBACK_MODEL": "fallback"}):
            tutor = Tutor()
            self.assertEqual(len(tutor.providers), 2)

            def search(query, limit=6):
                return [self.HIT]

            requests_out = []

            def post(url, **kwargs):
                requests_out.append((url, kwargs["json"]))
                if "primary" in url:
                    if sum("primary" in u for u, _ in requests_out) == 1:
                        return self.stream([
                            self.sse_tool_chunk(0, "call_1", "search_book", '{"query": "管辖"}',
                                                finish="tool_calls"),
                            b"data: [DONE]"])
                    # Round 2 fails twice: one blind retry, then give up.
                    raise requests.ConnectionError("flaky")
                return self.stream([self.sse_chunk(self.ANSWER1, finish="stop"), b"data: [DONE]"])

            events = []
            with patch("study.tutor.requests.post", side_effect=post) as post_mock:
                for event in tutor.agent_stream("问题", "qa", "教材", search, [], {}):
                    events.append(event)
        # One search round, one failed round (with retry), one fallback answer.
        self.assertEqual(post_mock.call_count, 4)
        self.assertTrue(any(kind == "search" for kind, _ in events))
        self.assertTrue(events[-1][1]["grounded"])
        # The fallback starts from a clean, provider-agnostic context that
        # carries the pooled evidence as a plain user message.
        fallback_messages = requests_out[-1][1]["messages"]
        self.assertEqual([message["role"] for message in fallback_messages],
                         ["system", "user", "user"])
        self.assertIn("C1", fallback_messages[-1]["content"])

    WEB_HITS = [{"title": "修正案通过", "url": "https://example.com/law", "site": "示例网",
                 "snippet": "条文已由最新修正案更新。"}]
    ANSWER_WEB = ('{"paragraphs":[{"text":"教材说明该制度[C1]，其现行状态可参考网络资料[W1]。",'
                  '"citations":["C1","W1"]}],"quiz":[]}')

    def test_agent_web_search_yields_w_citations(self):
        web_queries, requests_out = [], []

        def search(query, limit=6):
            return [self.HIT]

        def web_search(query):
            web_queries.append(query)
            return list(self.WEB_HITS)

        def post(url, **kwargs):
            requests_out.append(kwargs["json"])
            if len(requests_out) == 1:
                return self.stream([
                    self.sse_tool_chunk(0, "call_1", "search_book", '{"query": "管辖"}', finish="tool_calls"),
                    b"data: [DONE]"])
            if len(requests_out) == 2:
                return self.stream([
                    self.sse_tool_chunk(0, "call_2", "web_search", '{"query": "修正案 现行"}',
                                        finish="tool_calls"),
                    b"data: [DONE]"])
            return self.stream([self.sse_chunk(self.ANSWER_WEB, finish="stop"), b"data: [DONE]"])

        events = []
        with patch("study.tutor.requests.post", side_effect=post):
            for event in self.tutor.agent_stream("管辖制度现在还适用吗", "qa", "教材", search, [], {},
                                                 web_search=web_search):
                events.append(event)
        self.assertEqual(web_queries, ["修正案 现行"])
        infos = [value for kind, value in events if kind == "search"]
        self.assertEqual([info.get("web") for info in infos], [None, True])
        # The web tool reply carries W-labelled reference rows for the model.
        web_reply = json.loads(requests_out[2]["messages"][5]["content"])
        self.assertEqual([row["label"] for row in web_reply["results"]], ["W1"])
        self.assertEqual(web_reply["results"][0]["url"], "https://example.com/law")
        result = events[-1][1]
        self.assertTrue(result["grounded"])
        self.assertEqual([ref["label"] for ref in result["citations"]], ["C1", "W1"])
        web_ref = result["citations"][1]
        self.assertEqual(web_ref["kind"], "web")
        self.assertEqual(web_ref["url"], "https://example.com/law")
        self.assertNotIn("section", web_ref)
        self.assertIn("联网", result["notice"])

    def test_agent_web_call_cap_rejects_extra_searches(self):
        executed = []

        def search(query, limit=6):
            return [self.HIT]

        def web_search(query):
            executed.append(query)
            return list(self.WEB_HITS)

        def post(url, **kwargs):
            return self.stream([
                self.sse_tool_chunk(0, None, "web_search", '{"query": "再搜"}', finish="tool_calls"),
                b"data: [DONE]"])

        with patch("study.tutor.requests.post", side_effect=post):
            with self.assertRaises(TutorError):
                for _ in self.tutor.agent_stream("问题", "qa", "教材", search, [], {},
                                                 web_search=web_search):
                    pass
        # The dedicated web budget caps lookups even while rounds remain.
        self.assertEqual(len(executed), AGENT_MAX_WEB_CALLS)

    def test_agent_rejects_fabricated_web_labels(self):
        bad_answer = ('{"paragraphs":[{"text":"编造来源[W9]。","citations":["W9"]}],"quiz":[]}')

        def search(query, limit=6):
            return [self.HIT]

        requests_out = []

        def post(url, **kwargs):
            requests_out.append(kwargs["json"])
            if len(requests_out) == 1:
                return self.stream([
                    self.sse_tool_chunk(0, "call_1", "search_book", '{"query": "管辖"}', finish="tool_calls"),
                    b"data: [DONE]"])
            return self.stream([self.sse_chunk(bad_answer, finish="stop"), b"data: [DONE]"])

        with patch("study.tutor.requests.post", side_effect=post):
            # W9 was never returned by any web_search call, so the whole
            # answer is rejected even though a book search did happen.
            with self.assertRaises(TutorError):
                for _ in self.tutor.agent_stream("问题", "qa", "教材", search, [], {}, web_search=lambda q: []):
                    pass

    def test_agent_web_tool_absent_rejects_the_call(self):
        searches = []

        def search(query, limit=6):
            searches.append(query)
            return [self.HIT]

        requests_out = []

        def post(url, **kwargs):
            requests_out.append(kwargs["json"])
            if len(requests_out) == 1:
                return self.stream([
                    self.sse_tool_chunk(0, "x1", "web_search", '{"query": "最新"}', finish="tool_calls"),
                    b"data: [DONE]"])
            return self.stream([self.sse_chunk(self.ANSWER1, finish="stop"), b"data: [DONE]"])

        events = []
        with patch("study.tutor.requests.post", side_effect=post):
            # No web_search callable: the tool must not be offered, and a
            # model calling it anyway gets an error reply, never an execution.
            for event in self.tutor.agent_stream("问题", "qa", "教材", search, [], {}):
                events.append(event)
        infos = [value for kind, value in events if kind == "search"]
        self.assertTrue(all(info.get("error") for info in infos if info.get("web")))
        replies = [json.loads(requests_out[1]["messages"][3]["content"])]
        self.assertIn("error", replies[0])
        self.assertTrue(events[-1][1]["grounded"])

    ANSWER_BOLD = ('{"paragraphs":[{"text":"**级别管辖**指按级别分工[C1]，详见下图。",'
                   '"citations":["C1"]}],"quiz":[]}')
    DIAGRAM_ARGS = ('{"kind":"flowchart","title":"管辖流程","code":'
                    '"```mermaid\\nflowchart TD\\nA[起诉] --> B[立案]\\n```"}')

    def test_agent_draw_diagram_attaches_to_answer(self):
        requests_out = []

        def search(query, limit=6):
            return [self.HIT]

        def post(url, **kwargs):
            requests_out.append(kwargs["json"])
            if len(requests_out) == 1:
                return self.stream([
                    self.sse_tool_chunk(0, "call_1", "search_book", '{"query": "管辖"}', finish="tool_calls"),
                    b"data: [DONE]"])
            if len(requests_out) == 2:
                return self.stream([
                    self.sse_tool_chunk(0, "call_2", "draw_diagram", self.DIAGRAM_ARGS, finish="tool_calls"),
                    b"data: [DONE]"])
            return self.stream([self.sse_chunk(self.ANSWER_BOLD, finish="stop"), b"data: [DONE]"])

        events = []
        with patch("study.tutor.requests.post", side_effect=post):
            for event in self.tutor.agent_stream("管辖流程是什么", "qa", "教材", search, [], {}):
                events.append(event)
        # The diagram tool ships in the schema alongside search_book.
        self.assertIn("draw_diagram", [t["function"]["name"] for t in requests_out[0]["tools"]])
        infos = [value for kind, value in events if kind == "search"]
        self.assertTrue(infos and infos[1].get("diagram") and not infos[1].get("error"))
        result = events[-1][1]
        # Fences are stripped; kind/title survive verbatim.
        self.assertEqual(result["diagrams"],
                         [{"kind": "flowchart", "title": "管辖流程",
                           "code": "flowchart TD\nA[起诉] --> B[立案]"}])
        # Bold emphasis survives validation for the client to render.
        self.assertIn("**级别管辖**", result["paragraphs"][0]["text"])

    def test_agent_diagram_validation_rejects_abuse(self):
        ctx = {"diagrams": []}
        inject = {"name": "draw_diagram",
                  "arguments": json.dumps({"kind": "mindmap", "title": "注入",
                                           "code": "flowchart TD\nA[<script>alert(1)</script>]"})}
        reply, info = Tutor._run_diagram_call(inject, ctx)
        self.assertIn("error", reply)
        self.assertTrue(info.get("error") and info.get("diagram"))
        # Bad JSON and empty titles are rejected without raising.
        reply, info = Tutor._run_diagram_call({"name": "draw_diagram", "arguments": "not-json"}, ctx)
        self.assertIn("error", reply)
        reply, info = Tutor._run_diagram_call(
            {"name": "draw_diagram", "arguments": '{"kind":"flowchart","title":"  ","code":"x"}'}, ctx)
        self.assertIn("error", reply)
        self.assertEqual(ctx["diagrams"], [])

    def test_agent_diagram_budget_caps_at_three(self):
        ctx = {"diagrams": []}
        call = {"name": "draw_diagram",
                "arguments": '{"kind":"mindmap","title":"图","code":"mindmap\\n根((x))"}'}
        for _ in range(5):
            reply, info = Tutor._run_diagram_call(call, ctx)
        self.assertEqual(len(ctx["diagrams"]), 3)
        # The 4th and 5th calls are rejected, never staged.
        self.assertEqual(info.get("error"), True)

    def test_validate_answer_accepts_w_labels_only_when_allowed(self):
        result = {"paragraphs": [{"text": "书内依据[C1]，网络补充[W1]。", "citations": ["C1", "W1"]}], "quiz": []}
        paragraphs, _quiz, used = validate_answer(result, {"C1", "W1"}, "qa")
        self.assertEqual(used, {"C1", "W1"})
        self.assertIn("[W1]", paragraphs[0]["text"])
        # A W label outside the turn's web pool is just as invalid as an
        # unknown C label, so fabricated sources cannot slip through.
        with self.assertRaises(ValueError):
            validate_answer(result, {"C1"}, "qa")
        with self.assertRaises(ValueError):
            validate_answer({"paragraphs": [{"text": "只讲网络[W1]。", "citations": ["W1"]}], "quiz": []},
                            set(), "qa")


class CompactionTests(unittest.TestCase):
    """Rolling conversation summaries: thresholds, head/tail split, payload."""

    @staticmethod
    def history(pairs, chars=120):
        messages = []
        for index in range(pairs):
            filler = "结" * chars
            messages.append({"id": f"q{index}", "role": "user", "content": f"问题{index} {filler}"})
            messages.append({"id": f"a{index}", "role": "assistant", "content": f"回答{index} [C1]{filler}"})
        return messages

    def test_should_compact_requires_size_count_and_respects_watermark(self):
        self.assertFalse(should_compact(self.history(3)))
        self.assertTrue(should_compact(self.history(12, chars=800)))
        # A few huge messages alone never trigger a summary call.
        self.assertFalse(should_compact(self.history(1, chars=COMPACT_THRESHOLD_CHARS)))
        # Messages already covered by the watermark do not count again.
        long = self.history(12, chars=800)
        self.assertFalse(should_compact(long + self.history(3), long[-1]["id"]))
        # A watermark id that no longer exists is ignored, not fatal.
        self.assertTrue(should_compact(long, "missing-id"))

    def test_context_usage_mirrors_the_watermark(self):
        messages = self.history(6, chars=100)
        full = context_usage(messages)
        self.assertEqual(full["pending_messages"], 12)
        self.assertEqual(full["pending_chars"], sum(len(m["content"]) for m in messages))
        self.assertEqual(full["threshold"], COMPACT_THRESHOLD_CHARS)
        # After a compaction watermark, only the un-compacted tail counts.
        tail = context_usage(messages, messages[4]["id"])
        self.assertEqual(tail["pending_messages"], 7)
        self.assertEqual(tail["pending_chars"], sum(len(m["content"]) for m in messages[5:]))
        # A stale watermark keeps the whole conversation pending.
        self.assertEqual(context_usage(messages, "gone")["pending_messages"], 12)
        # The meter crosses the threshold exactly when should_compact fires
        # (given enough messages), so the UI never misleads.
        big = self.history(12, chars=800)
        self.assertGreaterEqual(context_usage(big)["pending_chars"], COMPACT_THRESHOLD_CHARS)
        self.assertTrue(should_compact(big))

    def test_split_keeps_recent_pairs_out_of_summary(self):
        messages = self.history(6)
        tail_start = split_for_summary(messages, KEEP_RECENT_PAIRS)
        self.assertEqual(messages[tail_start]["role"], "user")
        self.assertEqual([item["id"] for item in messages[tail_start:]],
                         ["q3", "a3", "q4", "a4", "q5", "a5"])
        # Too few pairs: everything stays verbatim, nothing to summarize.
        self.assertEqual(split_for_summary(self.history(2), KEEP_RECENT_PAIRS), 4)

    def test_compact_conversation_covers_head_only_and_strips_citations(self):
        messages = self.history(8, chars=300)
        captured = []

        def summarize(prompt_messages):
            captured.append(prompt_messages)
            return "## 2. 已解答问题与核心结论\n- 级别管辖由中级人民法院一审"

        outcome = compact_conversation(messages, "旧摘要：已讨论起诉条件", summarize)
        summary, mark = outcome
        # 8 user turns: the last 3 stay verbatim, so the head ends after pair 4.
        self.assertEqual(mark, "a4")
        self.assertEqual(summary, "## 2. 已解答问题与核心结论\n- 级别管辖由中级人民法院一审")
        self.assertEqual([item["role"] for item in captured[0]], ["system", "user"])
        prompt = captured[0][1]["content"]
        self.assertIn("问题0", prompt)
        self.assertIn("旧摘要：已讨论起诉条件", prompt)
        self.assertNotIn("问题5", prompt)  # the verbatim tail never enters the summary
        # The instruction block legitimately names "[C1]"; the conversation
        # payload itself must carry no per-turn citation labels.
        self.assertNotIn("[C1]", prompt.split("<conversation_to_summarize>", 1)[1])

    def test_compact_conversation_survives_summarizer_failure(self):
        def explode(_):
            raise requests.ConnectionError("down")

        self.assertIsNone(compact_conversation(self.history(8), "", explode))
        self.assertIsNone(compact_conversation(self.history(8), "", lambda _: "   "))
        # Oversized output is rejected instead of ballooning later prompts.
        self.assertIsNone(compact_conversation(self.history(8, chars=100), "", lambda _: "x" * 20000))

    def test_strip_citations_removes_only_markers(self):
        self.assertEqual(strip_citations("结论[C1]继续[C2]"), "结论继续")
        self.assertEqual(strip_citations("普通句子"), "普通句子")
        self.assertEqual(strip_citations(None), "")

    def test_circuit_opens_after_repeated_failures(self):
        state = {}
        for _ in range(3):
            record_compaction_result(state, success=False)
        self.assertTrue(compaction_circuit_open(state))
        record_compaction_result(state, success=True)
        self.assertFalse(compaction_circuit_open(state))
        self.assertFalse(compaction_circuit_open(None))

    def test_agent_payload_carries_summary(self):
        with patch.dict(os.environ, {
                "STUDY_LLM_BASE_URL": "https://primary.test/v1", "STUDY_LLM_API_KEY": "k1",
                "STUDY_LLM_MODEL": "primary"}):
            for key in [key for key in os.environ if key.startswith("STUDY_LLM_FALLBACK_")]:
                os.environ.pop(key)
            tutor = Tutor()
            answer = '{"paragraphs":[{"text":"结论[C1]。","citations":["C1"]}],"quiz":[]}'
            tool_line = ("data: " + json.dumps({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "function": {"name": "search_book",
                 "arguments": "{\"query\": \"管辖\"}"}}]}, "finish_reason": "tool_calls"}]},
                ensure_ascii=False)).encode()
            answer_line = ("data: " + json.dumps({"choices": [
                {"delta": {"content": answer}, "finish_reason": "stop"}]}, ensure_ascii=False)).encode()
            payloads = []

            def post(url, **kwargs):
                payloads.append(kwargs["json"])
                if "tools" in kwargs["json"] and len(payloads) == 1:
                    return AgentToolTests.stream([tool_line, b"data: [DONE]"])
                return AgentToolTests.stream([answer_line, b"data: [DONE]"])

            def search(query, limit=6):
                return [AgentToolTests.HIT]

            with patch("study.tutor.requests.post", side_effect=post):
                events = list(tutor.agent_stream("管辖", "qa", "教材", search, [], {}, "",
                                                 "早期摘要：讨论了起诉条件"))
            self.assertTrue(events[-1][1]["grounded"])
            context = json.loads(payloads[0]["messages"][1]["content"])
            self.assertEqual(context["earlier_conversation_summary_untrusted"], "早期摘要：讨论了起诉条件")
            # Without a summary the context key is absent entirely.
            payloads.clear()
            with patch("study.tutor.requests.post", side_effect=post):
                list(tutor.agent_stream("管辖", "qa", "教材", search, [], {}))
            context = json.loads(payloads[0]["messages"][1]["content"])
            self.assertNotIn("earlier_conversation_summary_untrusted", context)


class ChatCompactionEndpointTests(unittest.TestCase):
    """The chat pipeline compacts long conversations after the answer lands."""

    ANSWER = '{"paragraphs":[{"text":"依据教材回答[C1]。","citations":["C1"]}],"quiz":[]}'
    SUMMARY = "## 2. 已解答问题与核心结论\n- 级别管辖由中级人民法院一审"

    class FakeStream:
        def __init__(self, lines):
            self.lines = lines

        def raise_for_status(self): pass

        def __enter__(self): return self

        def __exit__(self, *args): return False

        def iter_lines(self):
            yield from self.lines

    class FakePlain:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self): pass

        def __enter__(self): return self

        def __exit__(self, *args): return False

        def json(self):
            return {"choices": [{"message": {"content": self.content}}]}

    def setUp(self):
        env = patch.dict(os.environ, {
            "STUDY_LLM_BASE_URL": "https://primary.test/v1", "STUDY_LLM_API_KEY": "k1",
            "STUDY_LLM_MODEL": "primary"})
        env.start()
        self.addCleanup(env.stop)
        for key in [key for key in os.environ if key.startswith("STUDY_LLM_FALLBACK_")]:
            os.environ.pop(key)
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "DATA_ROOT": Path(self.temp.name),
                               "SECRET_KEY": "unit-test-secret-" * 4,
                               "SESSION_COOKIE_SECURE": False, "REGISTRATION_OPEN": True})
        self.db = self.app.extensions["database"]
        self.app.extensions["embedder"].configured = False
        self.client = self.app.test_client()
        token = self.client.get("/api/auth/me").get_json()["csrf_token"]
        response = self.client.post("/api/auth/register",
                                    json={"username": "compacter", "password": "test-password-long",
                                          "api_key": "sk-unit-compaction"},
                                    headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 200)
        self.owner = response.get_json()["user"]["id"]
        self.csrf = response.get_json()["csrf_token"]
        self.book = uid()
        text = "行政诉讼管辖制度采用甲教材观点。"
        source = Path(self.temp.name) / (self.book + ".md")
        source.write_text(text * 4, encoding="utf-8")
        with self.db.connect() as db:
            db.execute("INSERT INTO books(id,owner_id,title,filename,source_path,status,created_at,chunk_count,section_count) "
                       "VALUES(?,?,?,?,?,'ready',?,1,1)",
                       (self.book, self.owner, "教材甲", "教材甲.md", source.name, now()))
            chunk_id = db.execute("INSERT INTO chunks(owner_id,book_id,ordinal,section,text) VALUES(?,?,1,'第一章',?)",
                                  (self.owner, self.book, text)).lastrowid
            db.execute("INSERT INTO chunks_fts(rowid,tokens) VALUES(?,?)", (chunk_id, index_tokens(text)))
        created = self.client.post(f"/api/books/{self.book}/conversations", json={},
                                   headers={"X-CSRF-Token": self.csrf})
        self.assertEqual(created.status_code, 201)
        self.conversation = created.get_json()["conversation"]["id"]

    def tearDown(self):
        self.app.extensions["stats_stop"].set()
        self.app.extensions["seed_thread"].join(timeout=300)
        self.app.extensions["index_executor"].shutdown(wait=True)
        self.temp.cleanup()

    def seed_history(self, pairs, chars=600, prefix=""):
        with self.db.connect() as db:
            for index in range(pairs):
                filler = "结" * chars
                db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                           (f"{prefix}q{index}", self.owner, self.book, self.conversation, "user",
                            f"问题{index} {filler}", "qa", "{}", f"2026-01-01T00:{index:02d}:00"))
                db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                           (f"{prefix}a{index}", self.owner, self.book, self.conversation, "assistant",
                            f"回答{index} {filler}", "qa", json.dumps({"content": f"回答{index} {filler}"}),
                            f"2026-01-01T00:{index:02d}:01"))

    def backend(self, agent_payloads=None, summary_calls=None):
        tool_line = ("data: " + json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "function": {"name": "search_book",
             "arguments": "{\"query\": \"管辖\"}"}}]}, "finish_reason": "tool_calls"}]},
            ensure_ascii=False)).encode()
        answer_line = ("data: " + json.dumps({"choices": [
            {"delta": {"content": self.ANSWER}, "finish_reason": "stop"}]}, ensure_ascii=False)).encode()

        def post(url, **kwargs):
            payload = kwargs["json"]
            if not payload.get("stream"):
                if summary_calls is not None:
                    summary_calls.append(payload)
                return self.FakePlain(self.SUMMARY)
            if agent_payloads is not None:
                agent_payloads.append(payload)
            # The opening round of every agent run carries the tools schema and
            # no tool result yet; it asks search_book, the next round answers.
            has_tool_result = any(message.get("role") == "tool" for message in payload["messages"])
            if "tools" in payload and not has_tool_result:
                return self.FakeStream([tool_line, b"data: [DONE]"])
            return self.FakeStream([answer_line, b"data: [DONE]"])

        return post

    def ask(self, message):
        response = self.client.post(f"/api/books/{self.book}/conversations/{self.conversation}/messages",
                                    json={"message": message}, headers={"X-CSRF-Token": self.csrf})
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('"answer"', body)
        return body

    def test_long_conversation_compacts_after_answer(self):
        self.seed_history(8)  # 16 seeded messages; with the new turn past the budget
        agent_payloads, summary_calls = [], []
        post = self.backend(agent_payloads, summary_calls)

        with patch("study.tutor.requests.post", side_effect=post):
            body = self.ask("级别管辖")
        self.assertIn("compacted", body)
        self.assertEqual(len(summary_calls), 1)
        with self.db.connect() as db:
            row = db.execute("SELECT summary, summary_mark FROM conversations WHERE id=?",
                             (self.conversation,)).fetchone()
        self.assertIn("级别管辖由中级人民法院一审", row["summary"])
        # The last 3 user turns stay verbatim: the head ends after pair 5.
        self.assertEqual(row["summary_mark"], "a5")

        # The next turn feeds the stored summary to the model and, with the
        # watermark now close, does not trigger another summary call.
        with patch("study.tutor.requests.post", side_effect=post):
            self.ask("再讲一遍")
        context = json.loads(agent_payloads[-1]["messages"][1]["content"])
        self.assertEqual(context["earlier_conversation_summary_untrusted"], row["summary"])
        self.assertEqual(len(summary_calls), 1)

    def test_short_conversation_never_compacts(self):
        self.seed_history(2, chars=200)
        summary_calls = []
        with patch("study.tutor.requests.post", side_effect=self.backend(None, summary_calls)):
            body = self.ask("级别管辖")
        self.assertNotIn("compacted", body)
        self.assertEqual(summary_calls, [])

    def test_compaction_failure_keeps_the_answer(self):
        self.seed_history(8)

        def post(url, **kwargs):
            payload = kwargs["json"]
            if not payload.get("stream"):
                raise requests.ConnectionError("summarizer down")
            return self.backend()(url, **kwargs)

        with patch("study.tutor.requests.post", side_effect=post):
            body = self.ask("级别管辖")
        self.assertNotIn("compacted", body)
        with self.db.connect() as db:
            row = db.execute("SELECT summary, summary_mark FROM conversations WHERE id=?",
                             (self.conversation,)).fetchone()
        self.assertEqual((row["summary"], row["summary_mark"]), ("", ""))

    @staticmethod
    def context_event(body):
        """Parse the last "context" SSE event from a chat response body."""
        found = None
        for line in body.splitlines():
            if line.startswith("data: ") and '"context"' in line:
                payload = json.loads(line[6:])
                if payload.get("type") == "context":
                    found = payload["context"]
        return found

    def test_context_event_tracks_pending_and_drops_after_compaction(self):
        # Short conversation: the meter carries every message, no summary yet.
        self.seed_history(2, chars=200)
        with patch("study.tutor.requests.post", side_effect=self.backend()):
            body = self.ask("级别管辖")
        context = self.context_event(body)
        self.assertIsNotNone(context)
        self.assertEqual(context["threshold"], COMPACT_THRESHOLD_CHARS)
        self.assertEqual(context["pending_messages"], 6)  # 4 seeded + question + answer
        seeded = 2 * (len("问题0 ") + 200) + 2 * (len("回答0 ") + 200)
        self.assertEqual(context["pending_chars"], seeded + len("级别管辖") + len("依据教材回答[C1]。"))
        self.assertEqual(context["summary_chars"], 0)

        # Long conversation: compaction runs first, then the meter reports
        # only the un-compacted tail plus the folded summary size. A fresh
        # conversation keeps the seeded ids unique.
        created = self.client.post(f"/api/books/{self.book}/conversations", json={},
                                   headers={"X-CSRF-Token": self.csrf})
        self.assertEqual(created.status_code, 201)
        self.conversation = created.get_json()["conversation"]["id"]
        self.seed_history(8, prefix="L")
        with patch("study.tutor.requests.post", side_effect=self.backend()):
            body = self.ask("级别管辖")
        self.assertIn("compacted", body)
        context = self.context_event(body)
        # 8 seeded pairs; the head ends after pair 5, leaving pairs 6-7 plus
        # the fresh question and answer pending.
        self.assertEqual(context["pending_messages"], 6)
        self.assertLess(context["pending_chars"], COMPACT_THRESHOLD_CHARS)
        self.assertEqual(context["summary_chars"], len(self.SUMMARY))

        # The history endpoint reports the same meter state on reload.
        data = self.client.get(f"/api/books/{self.book}/conversations/{self.conversation}").get_json()
        self.assertEqual(data["context"]["pending_messages"], context["pending_messages"])
        self.assertEqual(data["context"]["pending_chars"], context["pending_chars"])
        self.assertEqual(data["context"]["summary_chars"], context["summary_chars"])


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = create_app({"TESTING": True, "DATA_ROOT": self.root, "SECRET_KEY": "unit-test-secret-" * 4,
                               "SESSION_COOKIE_SECURE": False, "REGISTRATION_OPEN": True})
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
        # Open registration now requires a BYO key instead of an invite code.
        response = client.post("/api/auth/register",
                               json={"username": username, "password": "test-password-long",
                                     "api_key": "sk-unit-" + username},
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

    def test_chunk_window_and_range_feed_for_scrolling(self):
        with self.db.connect() as db:
            for ordinal, text in ((0, "前文"), (2, "后文")):
                db.execute("INSERT INTO chunks(owner_id,book_id,ordinal,section,text) VALUES(?,?,?,?,?)",
                           (self.a_id, self.book_a, ordinal, "第一章", text))
        detail = self.a.get(f"/api/books/{self.book_a}/chunks/{self.chunk_a}").get_json()
        # The initial reader window is ordered reading context around the citation.
        self.assertEqual([item["ordinal"] for item in detail["window"]], [0, 1, 2])
        base = f"/api/books/{self.book_a}/chunks"
        before = self.a.get(f"{base}?anchor={self.chunk_a}&direction=before&count=10").get_json()
        self.assertEqual([item["ordinal"] for item in before["chunks"]], [0])
        after = self.a.get(f"{base}?anchor={self.chunk_a}&direction=after&count=10").get_json()
        self.assertEqual([item["ordinal"] for item in after["chunks"]], [2])
        # Missing direction or an out-of-range count is rejected; scope still applies.
        self.assertEqual(self.a.get(f"{base}?anchor={self.chunk_a}").status_code, 400)
        self.assertEqual(self.a.get(f"{base}?anchor={self.chunk_a}&direction=after&count=0").status_code, 400)
        self.assertEqual(self.b.get(f"{base}?anchor={self.chunk_a}&direction=after&count=5").status_code, 404)

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

        def fake_agent_stream(question, mode, title, search, previous, retrieval, user_key="", summary="", web_search=None):
            # Same contract as the real loop: drive one scoped search through
            # the provided closure, then emit a final answer without a model.
            hits = search(question, 6)
            seen.extend(hits)
            yield ("search", {"query": question, "count": len(hits)})
            yield ("search", {"query": "", "count": 0, "error": True})
            yield ("search", {"query": question, "count": 1, "auto": True})
            yield ("result", {"content": "测试回答", "paragraphs": [], "quiz": [], "citations": [],
                              "grounded": False, "retrieval": retrieval})

        with patch.object(self.app.extensions["tutor"], "agent_stream", side_effect=fake_agent_stream):
            response = self.a.post(f"/api/books/{self.book_a}/conversations/{cid}/messages",
                                   json={"message": "管辖"}, headers={"X-CSRF-Token": self.a_csrf})
            # The SSE body must be read while the patch is active: streamed
            # responses are lazy, the generator only runs when consumed.
            body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('"type": "answer"', body)
        self.assertIn('"stage": "search"', body)
        # Each tool call ships as a structured step the UI renders as a trace.
        self.assertIn(f'"search": {{"query": "管辖", "count": {len(seen)}}}', body)
        self.assertEqual({hit["id"] for hit in seen}, {self.chunk_a})
        self.assertTrue(all("乙教材" not in hit["text"] for hit in seen))
        history = self.a.get(f"/api/books/{self.book_a}/conversations/{cid}").get_json()["messages"]
        self.assertEqual([message["role"] for message in history], ["user", "assistant"])
        retrieval = history[-1]["retrieval"]
        self.assertEqual(retrieval["searches"], 3)
        self.assertEqual(retrieval["steps"][0], {"query": "管辖", "count": len(seen)})
        self.assertEqual(retrieval["steps"][1], {"query": "", "count": 0, "error": True})
        self.assertEqual(retrieval["steps"][2], {"query": "管辖", "count": 1, "auto": True})
        # Every tool call also lands as a dedicated, owner-scoped log row.
        with self.db.connect() as db:
            rows = db.execute("SELECT level, detail FROM app_logs WHERE event='agent_search' ORDER BY id").fetchall()
        self.assertEqual(len(rows), 3)
        self.assertIn(f"检索「管辖」· 命中 {len(seen)} 段", rows[0]["detail"])
        self.assertIn("问: 管辖", rows[0]["detail"])
        self.assertEqual(rows[1]["level"], "warning")
        self.assertIn("调用被拒绝", rows[1]["detail"])
        self.assertIn("自动检索「管辖」· 命中 1 段", rows[2]["detail"])

    def test_agent_failure_falls_back_to_classic_retrieval(self):
        cid = self.conversation(self.book_a)
        seen = []

        def failing_agent_stream(*args, **kwargs):
            raise TutorError("tools unsupported")

        def fake_stream(question, mode, title, hits, previous, retrieval, user_key="", summary=""):
            # The classic pipeline contract: fixed retrieval, one answer.
            seen.extend(hits)
            yield ("result", {"content": "测试回答", "paragraphs": [], "quiz": [], "citations": [],
                              "grounded": False, "retrieval": retrieval})

        with patch.object(self.app.extensions["tutor"], "agent_stream", side_effect=failing_agent_stream), \
                patch.object(self.app.extensions["tutor"], "generate_stream", side_effect=fake_stream):
            response = self.a.post(f"/api/books/{self.book_a}/conversations/{cid}/messages",
                                   json={"message": "管辖"}, headers={"X-CSRF-Token": self.a_csrf})
            body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('"type": "answer"', body)
        # No agent search happened, so nothing streamed before the fallback.
        self.assertNotIn('"stage": "search"', body)
        self.assertEqual({hit["id"] for hit in seen}, {self.chunk_a})

    def test_invalid_section_and_mode_are_rejected(self):
        cid = self.conversation(self.book_a)
        for payload in ({"message": "管辖", "section": "不存在的章节"}, {"message": "管辖", "mode": []}):
            response = self.a.post(f"/api/books/{self.book_a}/conversations/{cid}/messages", json=payload,
                                   headers={"X-CSRF-Token": self.a_csrf})
            self.assertEqual(response.status_code, 400)

    def test_config_reports_web_search_availability(self):
        data = self.a.get("/api/config").get_json()
        self.assertFalse(data["web_search_configured"])
        self.app.extensions["web_search"].configured = True
        self.assertTrue(self.a.get("/api/config").get_json()["web_search_configured"])

    def test_web_toggle_routes_searches_steps_and_citations(self):
        cid = self.conversation(self.book_a)
        captured = {}
        web_reference = {"label": "W1", "kind": "web", "title": "来源", "url": "https://example.com/x",
                         "site": "站点", "snippet": "摘要"}
        book_reference = {"label": "C1", "chunk_id": self.chunk_a, "section": "第一章",
                          "page": None, "ordinal": 1, "excerpt": "行政诉讼管辖制度采用甲教材观点。"}

        def fake_agent_stream(question, mode, title, search, previous, retrieval, user_key="", summary="", web_search=None):
            captured["web_search"] = web_search
            hits = search(question, 6)
            yield ("search", {"query": question, "count": len(hits)})
            web_hits = web_search("现行状态") if web_search else []
            yield ("search", {"query": "现行状态", "count": len(web_hits), "web": True})
            yield ("result", {"content": "测试回答", "paragraphs": [{"text": "依据[C1]，补充[W1]。",
                                                                  "citations": ["C1", "W1"]}],
                              "quiz": [], "citations": [book_reference, web_reference],
                              "grounded": True, "retrieval": retrieval})

        searcher = self.app.extensions["web_search"]
        searcher.configured = True
        with patch.object(self.app.extensions["tutor"], "agent_stream", side_effect=fake_agent_stream), \
                patch.object(searcher, "search", return_value=[dict(web_reference)]) as web_call:
            response = self.a.post(f"/api/books/{self.book_a}/conversations/{cid}/messages",
                                   json={"message": "管辖", "web": True}, headers={"X-CSRF-Token": self.a_csrf})
            body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('"type": "answer"', body)
        self.assertIsNotNone(captured["web_search"])
        web_call.assert_called_once_with("现行状态", 6)
        # The live SSE step carries the web marker the UI renders distinctly.
        self.assertIn('"web": true', body)
        history = self.a.get(f"/api/books/{self.book_a}/conversations/{cid}").get_json()["messages"]
        retrieval = history[-1]["retrieval"]
        self.assertEqual(retrieval["searches"], 1)
        self.assertEqual(retrieval["web_searches"], 1)
        self.assertEqual(retrieval["steps"][1].get("web"), True)
        # Web citations persist on the message payload alongside book chunks.
        self.assertEqual(history[-1]["citations"][1]["label"], "W1")
        self.assertEqual(history[-1]["citations"][1]["url"], "https://example.com/x")
        with self.db.connect() as db:
            rows = db.execute("SELECT event, detail FROM app_logs "
                              "WHERE event IN ('agent_search','web_search') ORDER BY id").fetchall()
        self.assertEqual([row["event"] for row in rows], ["agent_search", "web_search"])
        self.assertIn("联网检索「现行状态」· 命中 1 条来源", rows[1]["detail"])

    def test_web_request_ignored_when_unconfigured(self):
        cid = self.conversation(self.book_a)
        captured = {}

        def fake_agent_stream(question, mode, title, search, previous, retrieval, user_key="", summary="", web_search=None):
            captured["web_search"] = web_search
            yield ("result", {"content": "测试回答", "paragraphs": [], "quiz": [], "citations": [],
                              "grounded": False, "retrieval": retrieval})

        with patch.object(self.app.extensions["tutor"], "agent_stream", side_effect=fake_agent_stream):
            response = self.a.post(f"/api/books/{self.book_a}/conversations/{cid}/messages",
                                   json={"message": "管辖", "web": True}, headers={"X-CSRF-Token": self.a_csrf})
            response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(captured["web_search"])

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
                              "SESSION_COOKIE_SECURE": False, "REGISTRATION_OPEN": True})
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
                               "SESSION_COOKIE_SECURE": False, "REGISTRATION_OPEN": False,
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
            db.execute("INSERT INTO users VALUES('beta01id','beta01','beta01',?,?,'')",
                       (generate_password_hash("x" * 20), now()))
            book_id = uid()
            db.execute("INSERT INTO books(id,owner_id,title,filename,source_path,status,created_at) "
                       "VALUES(?,?,?,?,?,'ready',?)", (book_id, "beta01id", "owned", "owned.md", "owned.md", now()))
            db.execute("INSERT INTO conversations(id,owner_id,book_id,title,created_at) "
                       "VALUES('c1','beta01id',?,?,?)", (book_id, "chat", now()))
        # Re-run create_app over the same data root: codes reseed, beta01 is
        # gone, and the code bound before the restart stays bound.
        app2 = create_app({"TESTING": True, "DATA_ROOT": self.root, "SECRET_KEY": "unit-test-secret-" * 4,
                           "SESSION_COOKIE_SECURE": False, "REGISTRATION_OPEN": False,
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


class ApiKeyAccountTests(unittest.TestCase):
    """Self-service registration with a personal key; the key replaces the
    site's primary provider key at generation time."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = create_app({"TESTING": True, "DATA_ROOT": self.root, "SECRET_KEY": "unit-test-secret-" * 4,
                               "SESSION_COOKIE_SECURE": False, "REGISTRATION_OPEN": True})
        self.db = self.app.extensions["database"]
        self.app.extensions["embedder"].configured = False

    def tearDown(self):
        self.app.extensions["stats_stop"].set()
        self.app.extensions["seed_thread"].join(timeout=300)
        self.app.extensions["index_executor"].shutdown(wait=True)
        self.temp.cleanup()

    def register(self, payload):
        client = self.app.test_client()
        token = client.get("/api/auth/me").get_json()["csrf_token"]
        response = client.post("/api/auth/register", json=payload, headers={"X-CSRF-Token": token})
        return client, response

    def test_open_registration_requires_key_or_code(self):
        _, neither = self.register({"username": "no_credential", "password": "test-password-long"})
        self.assertEqual(neither.status_code, 403)
        _, short = self.register({"username": "short_key", "password": "test-password-long",
                                  "api_key": "abc"})
        self.assertEqual(short.status_code, 403)
        client, with_key = self.register({"username": "byo_user", "password": "test-password-long",
                                          "api_key": "sk-personal-key-123"})
        self.assertEqual(with_key.status_code, 200)
        self.assertFalse("api_key" in with_key.get_json()["user"])  # never echoed back
        with self.db.connect() as db:
            stored = db.execute("SELECT api_key FROM users WHERE username_key='byo_user'").fetchone()
        self.assertEqual(stored["api_key"], "sk-personal-key-123")

    def test_api_key_endpoint_updates_and_clears(self):
        client, created = self.register({"username": "key_user", "password": "test-password-long",
                                         "api_key": "sk-personal-key-123"})
        csrf = created.get_json()["csrf_token"]
        me = client.get("/api/auth/me").get_json()
        self.assertTrue(me["api_key_set"])
        bad = client.post("/api/account/api-key", json={"api_key": "a b c"},
                          headers={"X-CSRF-Token": csrf})
        self.assertEqual(bad.status_code, 400)
        updated = client.post("/api/account/api-key", json={"api_key": "sk-rotated-key-456"},
                              headers={"X-CSRF-Token": csrf})
        self.assertEqual(updated.status_code, 200)
        self.assertTrue(updated.get_json()["api_key_set"])
        with self.db.connect() as db:
            row = db.execute("SELECT api_key FROM users WHERE username_key='key_user'").fetchone()
        self.assertEqual(row["api_key"], "sk-rotated-key-456")
        cleared = client.post("/api/account/api-key", json={"api_key": ""},
                              headers={"X-CSRF-Token": csrf})
        self.assertEqual(cleared.status_code, 200)
        self.assertFalse(cleared.get_json()["api_key_set"])
        with self.db.connect() as db:
            row = db.execute("SELECT api_key FROM users WHERE username_key='key_user'").fetchone()
        self.assertEqual(row["api_key"], "")

    def test_user_key_replaces_primary_provider_only(self):
        from study.tutor import Tutor
        with patch.dict(os.environ, {
            "STUDY_LLM_BASE_URL": "https://primary.example",
            "STUDY_LLM_API_KEY": "site-primary",
            "STUDY_LLM_MODEL": "m1",
            "STUDY_LLM_FALLBACK_BASE_URL": "https://fallback.example",
            "STUDY_LLM_FALLBACK_API_KEY": "site-fallback",
            "STUDY_LLM_FALLBACK_MODEL": "m2",
        }):
            tutor = Tutor()
            providers = tutor._providers_for("sk-personal-key-123")
            self.assertEqual([p["key"] for p in providers], ["sk-personal-key-123", "site-fallback"])
            self.assertEqual(tutor._providers_for(""), tutor.providers)


if __name__ == "__main__":
    unittest.main()
