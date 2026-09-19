"""Rolling conversation summaries for long study conversations.

Adapted from the data-analysis agent's history compaction: once the part of
a conversation not yet covered by the summary grows past a character budget,
one lightweight LLM call condenses it into a structured summary stored on
the conversation row. Later prompts carry that summary as untrusted
historical context while the newest questions keep resolving pronouns, so
long study sessions remember early Q&A without growing every payload.

Compaction runs after an answer is delivered and never blocks chat: any
failure keeps the conversation exactly as it was.
"""

import logging

from .tutor import CITATION_MARKER

LOG = logging.getLogger(__name__)

# Compact when the not-yet-summarized messages reach this many content chars
# (roughly 2.5k tokens): about 6-10 Q&A pairs in a textbook study session.
COMPACT_THRESHOLD_CHARS = 9000
# The newest Q&A pairs stay out of the summary; they are the live tail.
KEEP_RECENT_PAIRS = 3
# Below this many uncompacted messages a summary call is not worthwhile.
MIN_MESSAGES_FOR_COMPACT = 6
# Hard cap on chars fed to the summarizer in one call.
MAX_SUMMARY_INPUT_CHARS = 16000
# Reject absurdly long summaries (the request itself caps tokens as well).
MAX_SUMMARY_CHARS = 12000
# After this many consecutive failures a conversation stops compacting until
# the process restarts or a later manual success resets the breaker.
COMPACTION_FAILURE_LIMIT = 3

SUMMARY_SYSTEM = (
    "你是对话摘要助手。唯一任务是基于给出的教材学习对话写出结构化摘要，"
    "只输出摘要本身，不加开场白、解释或评论。"
)

SUMMARY_PROMPT_TEMPLATE = """\
下面是一段教材学习对话中需要压缩的较早部分。用户在使用一个只依据教材原文回答的学习助手
（提问、要求讲解、梳理要点、生成自测题），每条回答都基于当时检索到的教材片段并带引用标记。{prior_summary_note}

硬性要求：
- 保留全部具体结论：定义、条文编号、构成要件、数字、日期、名称、例外情形。写「教材讲了管辖制度」没有价值，写「级别管辖的案件由中级人民法院一审」才有价值。
- 保留教材术语、制度名与章节名的原文用词，便于后续继续检索。
- 保留用户的明确纠正、限定和偏好，尽量沿用原话。
- 区分教材观点与用户自己的疑问或猜测，不把未经教材支持的说法写成事实。
- 摘要中不得出现 [C1] 这类引用标记。

<conversation_to_summarize>
{conversation_text}
</conversation_to_summarize>

用下面的标题输出结构化摘要（确无内容的小节可省略）：

## 1. 学习目标与范围
用户在学什么教材、围绕哪些章节或主题提问。

## 2. 已解答问题与核心结论（最重要）
逐条记录问题与结论，保留具体表述和数字。

## 3. 用户纠正与偏好
用户对回答方式、深度、口径的明确要求和纠正。

## 4. 未解决或待续的问题
证据不足未能回答、或用户表示要继续追问的线索。

## 5. 当前学习进度
对话最近停在哪里，最后一个话题是什么。
"""


def strip_citations(text):
    """Drop inline [Cn] markers: their label numbering is per-turn and must
    never leak into the summary or later prompts."""
    return CITATION_MARKER.sub("", text or "")


def split_for_summary(messages, keep_pairs=KEEP_RECENT_PAIRS):
    """Index where the verbatim tail starts: the last `keep_pairs` user
    questions plus everything after them stay out of the summary."""
    starts = [index for index, message in enumerate(messages)
              if message.get("role") == "user"]
    if len(starts) <= keep_pairs:
        return len(messages)
    return starts[len(starts) - keep_pairs]


def _pending_after_mark(messages, summary_mark=""):
    """The un-compacted tail: everything after the watermark message."""
    if not summary_mark:
        return messages
    index = next((position for position, message in enumerate(messages)
                  if message.get("id") == summary_mark), -1)
    return messages[index + 1:] if index >= 0 else messages


def should_compact(messages, summary_mark="", threshold=COMPACT_THRESHOLD_CHARS):
    """True when the messages after the watermark are big enough to summarize.

    `messages` is oldest-first and must carry id/role/content keys. The
    watermark is the id of the last message already covered by a summary;
    messages before it never count toward the threshold again.
    """
    pending = _pending_after_mark(messages, summary_mark)
    if len(pending) < MIN_MESSAGES_FOR_COMPACT:
        return False
    return sum(len(message.get("content") or "") for message in pending) >= threshold


def context_usage(messages, summary_mark="", threshold=COMPACT_THRESHOLD_CHARS):
    """Live meter data for the UI: how much un-compacted context the next
    model call will carry, against the threshold that triggers compression.
    Mirrors should_compact's watermark semantics exactly."""
    pending = _pending_after_mark(messages, summary_mark)
    return {"pending_messages": len(pending),
            "pending_chars": sum(len(message.get("content") or "") for message in pending),
            "threshold": threshold}


def _render_segments(messages):
    """Oldest-first (is_user, line) segments with citations stripped."""
    segments = []
    for message in messages:
        content = strip_citations(message.get("content") or "").strip()
        if not content:
            continue
        segments.append((message.get("role") == "user", content))
    return segments


def _bounded_text(segments, max_chars=MAX_SUMMARY_INPUT_CHARS):
    """User lines have retention priority; fill the rest from newest to
    oldest, then restore chronology (mirrors the analytics agent's rule)."""
    selected, used = set(), 0
    for index, (is_user, text) in enumerate(segments):
        if not is_user:
            continue
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(text) > remaining:
            text = text[:remaining]
            segments[index] = (True, text)
        selected.add(index)
        used += len(text) + 2
    for index in range(len(segments) - 1, -1, -1):
        if index in selected:
            continue
        is_user, text = segments[index]
        if not text:
            continue
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(text) > remaining:
            text = text[-remaining:]
            segments[index] = (is_user, "[earlier part omitted] " + text)
        selected.add(index)
        used += len(segments[index][1]) + 2
    return "\n\n".join(
        ("[USER] " if segments[index][0] else "[BOOK] ") + segments[index][1]
        for index in sorted(selected))


def build_summary_messages(head, prior_summary=""):
    """Full chat messages for the summarizer call over the older `head` part."""
    note = ""
    prior = strip_citations(prior_summary or "").strip()
    if prior:
        note = ("\n\n此前已有一份更早对话的摘要。请把它与下面的新内容合并为一份更新后的摘要，"
                "仍然有效的信息必须保留：\n<existing_summary>\n"
                + prior[:MAX_SUMMARY_INPUT_CHARS // 2] + "\n</existing_summary>")
    prompt = SUMMARY_PROMPT_TEMPLATE.format(
        prior_summary_note=note,
        conversation_text=_bounded_text(_render_segments(head)))
    return [{"role": "system", "content": SUMMARY_SYSTEM},
            {"role": "user", "content": prompt}]


def compact_conversation(messages, prior_summary, summarize,
                         keep_pairs=KEEP_RECENT_PAIRS):
    """Summarize the older part of a conversation.

    messages:  oldest-first list of {id, role, content} dicts.
    summarize: callable taking the built chat messages and returning text;
               may raise, in which case compaction silently gives up.
    Returns (summary, mark_id) — or None when there is nothing to summarize
    or the summarizer fails; the caller then keeps the conversation as-is.
    """
    try:
        tail_start = split_for_summary(messages, keep_pairs)
        head = messages[:tail_start]
        if not head:
            return None
        summary = str(summarize(build_summary_messages(head, prior_summary)) or "").strip()
        if not summary or len(summary) > MAX_SUMMARY_CHARS:
            LOG.warning("[compaction] rejected summary output (empty or over %d chars)",
                        MAX_SUMMARY_CHARS)
            return None
        LOG.info("[compaction] summarized %d of %d messages into %d chars",
                 len(head), len(messages), len(summary))
        return summary, head[-1]["id"]
    except Exception as exc:
        LOG.warning("[compaction] summarization failed: %s — keeping conversation as-is", exc)
        return None


def compaction_circuit_open(state):
    return bool((state or {}).get("circuit_open"))


def record_compaction_result(state, *, success):
    """Update a conversation-owned breaker dict; three consecutive failures
    open the circuit so a broken summarizer stops adding latency."""
    if state is None:
        return
    if success:
        state["consecutive_failures"] = 0
        state["circuit_open"] = False
        return
    failures = int(state.get("consecutive_failures") or 0) + 1
    state["consecutive_failures"] = failures
    if failures >= COMPACTION_FAILURE_LIMIT:
        state["circuit_open"] = True
