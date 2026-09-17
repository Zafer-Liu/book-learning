"""UTF-8 textbook parsing with chapter-preserving, overlapping chunks."""

import re
from pathlib import Path

FORMATS = {".md", ".markdown", ".txt"}
MAX_CHARS = 4_000_000
MAX_CHUNKS = 10_000


def parse_document(path: Path, filename: str) -> list[dict]:
    suffix = Path(filename).suffix.lower()
    if suffix not in FORMATS:
        raise ValueError("请上传 Markdown 或 TXT；扫描 PDF 请先完成 OCR 并导出 Markdown。")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("文件不是 UTF-8 编码，请另存为 UTF-8 后上传。") from exc
    if "\x00" in text:
        raise ValueError("文件包含二进制内容，请上传纯文本教材。")
    if len(text) > MAX_CHARS:
        raise ValueError("教材超过 400 万字符，请按卷拆分后上传。")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if suffix != ".txt":
        text = re.sub(r"\A---\s*\n.*?\n(?:---|\.\.\.)\s*(?:\n|$)", "", text, count=1, flags=re.S)
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        # Images and their OCR filenames are not textual evidence.
        text = re.sub(r"!\[[^\]]*\]\([^\n]*?\)", "", text)
        text = re.sub(r"!\[[^\]]*\]\[[^\]]*\]", "", text)
        text = re.sub(r"<img\b[^>]*>", "", text, flags=re.I)
        text = re.sub(r"(?m)^\s*\[(?!\^)[^\]]+\]:\s*\S+.*$", "", text)
    if len(re.sub(r"[\W_]+", "", text)) < 30:
        raise ValueError("未找到足够的正文文字。图片不会被识别，请上传 OCR 后含正文的 Markdown。")
    if suffix == ".txt":
        return [{"text": text.strip(), "section": "正文", "page": None}]

    sections, headings, buffer = [], [], []
    label = "正文"
    fence = None
    lines = text.splitlines()
    index = 0

    def flush():
        body = "\n".join(buffer).strip()
        if body:
            sections.append({"text": body, "section": label, "page": None})
        buffer.clear()

    while index < len(lines):
        line = lines[index]
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            buffer.append(line)
            index += 1
            continue
        heading = None if fence else re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        level, title, skip = 0, "", 1
        if heading:
            level, title = len(heading.group(1)), heading.group(2)
        elif not fence and line.strip() and index + 1 < len(lines):
            underline = re.match(r"^\s{0,3}(={3,}|-{3,})\s*$", lines[index + 1])
            if underline:
                level = 1 if underline.group(1)[0] == "=" else 2
                title, skip = line.strip(), 2
        if level:
            flush()
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, title[:180]))
            label = " / ".join(item[1] for item in headings)
            buffer.append(title)
        else:
            buffer.append(line)
        index += skip
    flush()
    return sections


def split_sections(sections: list[dict], max_chars=1200, overlap=160) -> list[dict]:
    if not 0 <= overlap < max_chars:
        raise ValueError("Invalid chunk overlap")
    chunks = []
    for section in sections:
        text = section["text"].strip()
        start = 0
        while start < len(text):
            end = min(start + max_chars, len(text))
            if end < len(text):
                boundary = text.rfind("\n\n", start + max_chars // 2, end)
                if boundary >= 0:
                    end = boundary
            part = text[start:end].strip()
            if part:
                chunks.append({"ordinal": len(chunks) + 1, "text": part,
                               "section": section["section"], "page": section.get("page")})
            if len(chunks) > MAX_CHUNKS:
                raise ValueError("教材分块过多，请按卷拆分后上传。")
            if end == len(text):
                break
            start = max(start + 1, end - overlap)
    if not chunks:
        raise ValueError("教材没有可索引的正文。")
    return chunks
