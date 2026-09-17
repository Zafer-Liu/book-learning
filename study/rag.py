"""Book-filtered lexical, FTS5 and optional cloud-vector reciprocal rank fusion."""

import hashlib
import math
import os
import re
from collections import defaultdict
from functools import lru_cache
from urllib.parse import urlsplit

import jieba
import requests

STOP_WORDS = frozenset("请 请问 帮我 帮 帮助 解释 讲解 梳理 总结 自测 出题 教材 本书 本章 章节 这个 那个 什么 如何 为什么 一个 一些 进行 关于 根据 内容 知识点 的 了 是 在 和 与 或 就 都 吗 呢 把 将 用 以及 并且 可以 我 你 它 继续 详细 简单 通俗 学习 核心 要点 重点 这本书 这本 给我 三道 问题 自测题 练习 理解 当前 范围".split())


@lru_cache(maxsize=128)
def terms(text: str) -> list[str]:
    words = jieba.cut_for_search(text[:6000].lower())
    return list(dict.fromkeys(word.strip() for word in words
                             if re.search(r"[\w\u4e00-\u9fff]", word)
                             and word.strip() not in STOP_WORDS and len(word.strip()) > 1))[:64]


def tokenize(text: str) -> str:
    return " ".join(terms(text))


def index_tokens(text: str) -> str:
    return " ".join(word.strip() for word in jieba.cut_for_search(text.lower())
                    if word.strip() and re.search(r"\w", word))


def api_url(base: str, resource: str) -> str:
    parts = urlsplit(base)
    local = parts.hostname in {"localhost", "127.0.0.1", "::1"}
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("模型服务地址必须是有效的 HTTP(S) 地址。")
    if parts.scheme != "https" and not local:
        raise ValueError("非本机模型服务必须使用 HTTPS。")
    if parts.query or parts.fragment:
        raise ValueError("模型服务地址不能包含查询参数或片段。")
    root = base.rstrip("/")
    # Bases carrying an explicit version path (e.g. /v1, /v4) are complete
    # prefixes; only bare hosts get /v1 appended.
    if parts.path.strip("/") == "":
        root += "/v1"
    return root + "/" + resource


def normalize(vector) -> list[float]:
    if not isinstance(vector, list) or not 16 <= len(vector) <= 8192:
        raise ValueError("向量服务返回了无效维度。")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
        raise ValueError("向量服务返回了无效数值。")
    norm = math.sqrt(sum(v * v for v in vector))
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("向量服务返回了空向量。")
    return [v / norm for v in vector]


class EmbeddingClient:
    def __init__(self):
        primary = os.getenv("STUDY_EMBED_BASE_URL", "").strip()
        self.base = primary or os.getenv("BAA_CLOUD_EMBED_URL", "").strip()
        if primary:
            self.key = os.getenv("STUDY_EMBED_API_KEY", "").strip()
            self.model = (os.getenv("STUDY_EMBED_MODEL") or "bge-large-zh").strip()
        else:
            self.key = os.getenv("BAA_CLOUD_EMBED_TOKEN", "").strip()
            self.model = (os.getenv("BAA_CLOUD_EMBED_MODEL") or "bge-large-zh").strip()
        self.configured = bool(self.base and self.model)
        # Reuse pooled connections: remote TLS handshakes can cost seconds,
        # while a warm connection answers in milliseconds.
        self.session = requests.Session()

    def embed_texts(self, texts: list[str]) -> tuple[list[list[float]], str]:
        if not self.configured:
            raise ValueError("未配置向量模型，使用关键词检索。")
        url = api_url(self.base, "embeddings")
        vectors, dimension = [], None
        try:
            for offset in range(0, len(texts), 16):
                batch = texts[offset:offset + 16]
                headers = {"Authorization": "Bearer " + self.key} if self.key else {}
                with self.session.post(url, json={"model": self.model, "input": batch},
                                       headers=headers, timeout=(15, 30), allow_redirects=False) as response:
                    response.raise_for_status()
                    data = response.json()["data"]
                if not isinstance(data, list) or len(data) != len(batch):
                    raise ValueError("向量返回数量不匹配。")
                mapping = {item["index"]: normalize(item["embedding"]) for item in data}
                if set(mapping) != set(range(len(batch))):
                    raise ValueError("向量索引不完整。")
                for index in range(len(batch)):
                    vector = mapping[index]
                    dimension = dimension or len(vector)
                    if len(vector) != dimension:
                        raise ValueError("向量模型维度发生变化，请重新索引。")
                    vectors.append(vector)
        except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
            raise ValueError("向量服务暂不可用，已降级为关键词检索。") from exc
        fingerprint = hashlib.sha256(f"{url}|{self.model}|{dimension}".encode()).hexdigest()[:24]
        return vectors, fingerprint


def lexical_score(query: str, text: str) -> float:
    query_terms = terms(query)
    if not query_terms:
        return 0.0
    haystack = text.lower()
    meaningful = "".join(query_terms)
    phrase = 1.2 if len(meaningful) >= 2 and meaningful in haystack else 0.0
    words = [word for word in query_terms if re.search(r"[a-z0-9]", word)]
    english = 0.45 * sum(bool(re.search(r"\b" + re.escape(w) + r"\b", haystack)) for w in words) / max(1, len(words))
    grams = set()
    for word in query_terms:
        for run in re.findall(r"[\u4e00-\u9fff]+", word):
            for size in (2, 3):
                grams.update(run[i:i + size] for i in range(len(run) - size + 1))
    chinese = 0.9 * sum(gram in haystack for gram in grams) / max(1, len(grams))
    return phrase + english + chinese


def retrieve(query: str, chunks: list[dict], fts_ids: list[int], embedder: EmbeddingClient, limit=6) -> dict:
    if not chunks:
        return {"hits": [], "backend": "lexical+fts5", "degraded": True}
    allowed = {chunk["id"]: chunk for chunk in chunks}
    lexical = {key: lexical_score(query, chunk["text"]) for key, chunk in allowed.items()}
    vector_scores, vector_active = {}, False
    try:
        spaces = {chunk.get("embedding_space") for chunk in chunks if chunk.get("embedding")}
        if embedder.configured and spaces:
            vectors, space = embedder.embed_texts([query[:4000]])
            q = vectors[0]
            for key, chunk in allowed.items():
                value = chunk.get("embedding")
                if value and chunk.get("embedding_space") == space and len(value) == len(q):
                    score = sum(a * b for a, b in zip(q, value))
                    if math.isfinite(score):
                        vector_scores[key] = score
            vector_active = bool(vector_scores)
    except ValueError:
        pass
    lexical_rank = sorted((key for key in allowed if lexical[key] >= 0.12), key=lambda k: (-lexical[k], k))[:limit * 3]
    vector_rank = sorted((key for key in vector_scores if vector_scores[key] >= 0.55), key=lambda k: (-vector_scores[k], k))[:limit * 3]
    fts_rank = list(dict.fromkeys(key for key in fts_ids if key in allowed and lexical[key] >= 0.12))[:limit * 4]
    scores = defaultdict(float)
    for ranking in (lexical_rank, vector_rank, fts_rank):
        for rank, key in enumerate(ranking, 1):
            scores[key] += 1 / (60 + rank)
    threshold = 0.02 if vector_active else 0.015
    candidates = [key for key, score in scores.items()
                  if score >= threshold or vector_scores.get(key, 0) >= 0.72]
    candidates.sort(key=lambda key: (-scores[key], -vector_scores.get(key, 0), key))
    hits = []
    for key in candidates[:limit]:
        hit = {k: v for k, v in allowed[key].items() if k not in {"embedding", "embedding_space"}}
        hit.update(rrf_score=scores[key], lexical_score=lexical[key], vector_score=vector_scores.get(key, 0))
        hits.append(hit)
    return {"hits": hits, "backend": "vector+lexical+fts5" if vector_active else "lexical+fts5",
            "degraded": not vector_active}


SENTENCE_BREAK = re.compile(r"[^。！？!?；;\n]*[。！？!?；;\n]+|[^。！？!?；;\n]+")
# Sentence-level semantic highlight thresholds (bge-m3 cosine similarity).
# Calibrated on production: related sentences score 0.62-0.85 (even with zero
# keyword overlap), unrelated questions top out near 0.40.
SENTENCE_MATCH_MIN = 0.50   # a sentence must clear this to be highlighted
SENTENCE_MATCH_BEST = 0.55  # the best sentence must clear this, else nothing shows
SENTENCE_MATCH_TOP = 3


def split_sentences(text: str, min_length: int = 10, limit: int = 48) -> list[list[int]]:
    """Character spans [start, end) partitioning the text into sentences.

    Too-short fragments (numbering, stray punctuation, blank lines) cannot
    stand alone as a highlight, so they merge into the following sentence;
    a trailing fragment merges into the previous one.
    """
    spans = [(m.start(), m.end()) for m in SENTENCE_BREAK.finditer(text) if m.end() > m.start()]
    merged: list[list[int]] = []
    pending: tuple[int, int] | None = None
    for start, end in spans:
        if pending is not None:
            start, pending = pending[0], None
        if end - start < min_length:
            pending = (start, end)
            continue
        merged.append([start, end])
    if pending is not None:
        if merged:
            merged[-1][1] = pending[1]
        else:
            merged.append([pending[0], pending[1]])
    return merged[:limit]


def semantic_sentence_ranges(embedder: EmbeddingClient, question: str, text: str,
                             min_score: float = SENTENCE_MATCH_MIN,
                             best_score: float = SENTENCE_MATCH_BEST,
                             top: int = SENTENCE_MATCH_TOP) -> list[list[int]]:
    """Character spans of the sentences most similar to the question.

    Best-effort evidence locating for the reference panel: an unconfigured
    embedder, a service outage or uniformly low similarity all return [] so
    callers can fall back to keyword highlighting alone.
    """
    spans = split_sentences(text)
    if not spans or not embedder.configured:
        return []
    try:
        vectors, _ = embedder.embed_texts(
            [question[:4000]] + [text[start:end] for start, end in spans])
    except ValueError:
        return []
    if len(vectors) != len(spans) + 1:
        return []
    query = vectors[0]
    scored = [(sum(a * b for a, b in zip(query, vectors[i + 1])), i) for i in range(len(spans))]
    if max(score for score, _ in scored) < best_score:
        return []
    picked = sorted((pair for pair in scored if pair[0] >= min_score),
                    key=lambda pair: (-pair[0], pair[1]))[:top]
    return [spans[i] for _, i in sorted(picked, key=lambda pair: pair[1])]
