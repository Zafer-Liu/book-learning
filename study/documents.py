"""UTF-8 textbook parsing with chapter-preserving, overlapping chunks."""

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

FORMATS = {".md", ".markdown", ".txt", ".docx"}
MAX_CHARS = 4_000_000
MAX_CHUNKS = 10_000

_DOCX_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
_W = "{" + _DOCX_NS["w"] + "}"
_CJK = "\u3400-\u9fff"
_NUMBER = r"[一二三四五六七八九十百零〇两\d]+"


def _squeeze(text):
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(rf"(?<=[{_CJK}]) +(?=[{_CJK}])", "", text)


def _docx_paragraph_text(node):
    parts = []
    for child in node.iter():
        if child.tag == _W + "t":
            parts.append(child.text or "")
        elif child.tag == _W + "tab":
            parts.append("\t")
        elif child.tag in {_W + "br", _W + "cr"}:
            parts.append("\n")
    return "".join(parts)


# Heading heuristics cover styled Word documents and OCR exports where every
# paragraph shares one style. Page-numbered running headers stay plain text.
def _docx_heading_level(text, style_name, outline):
    if style_name:
        match = re.match(r"heading\s*(\d)", style_name, re.I)
        if match:
            return min(int(match.group(1)) + 1, 6)
    if outline is not None:
        try:
            return min(int(outline) + 1, 6)
        except ValueError:
            pass
    if not text or len(text) > 60 or re.search(r"[。！？；：]", text) or re.search(r"\s\d{1,4}\.?$", text):
        return 0
    major = re.fullmatch(rf"第({_NUMBER})(分编|编|章|节)\s*(\S.*)", text)
    if major:
        return {"编": 2, "分编": 3, "章": 4, "节": 5}[major.group(2)]
    if re.fullmatch(rf"\d{{1,2}}\.\d{{1,2}}\.\d{{1,2}}\s+\S.*", text):
        return 6
    if re.fullmatch(r"\d{1,2}\.\d{1,2}\s+\S.*", text):
        return 5
    return 0


def docx_to_markdown(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            if "word/document.xml" not in archive.namelist():
                raise ValueError("DOCX 中没有 word/document.xml，可能不是 Word 文档。")
            if archive.getinfo("word/document.xml").file_size > 300 * 1024 * 1024:
                raise ValueError("DOCX 正文过大，请按卷拆分后上传。")
            document = ET.fromstring(archive.read("word/document.xml"))
            styles = {}
            if "word/styles.xml" in archive.namelist():
                style_root = ET.fromstring(archive.read("word/styles.xml"))
                for style in style_root.findall("w:style", _DOCX_NS):
                    name = style.find("w:name", _DOCX_NS)
                    outline = style.find("w:pPr/w:outlineLvl", _DOCX_NS)
                    styles[style.get(_W + "styleId")] = (
                        name.get(_W + "val") if name is not None else "",
                        outline.get(_W + "val") if outline is not None else None,
                    )
    except (zipfile.BadZipFile, ET.ParseError) as exc:
        raise ValueError("DOCX 文件已损坏或不是有效的 Word 文档。") from exc
    body = document.find("w:body", _DOCX_NS)
    if body is None:
        raise ValueError("DOCX 结构异常，缺少正文。")
    # Text boxes (covers, watermarks) duplicate their text through the
    # enclosing paragraph; skip them in the outer traversal.
    nested = {p for box in body.iter(_W + "txbxContent") for p in box.iter(_W + "p")}
    lines = []
    for node in body.iter(_W + "p"):
        if node in nested:
            continue
        text = _squeeze(_docx_paragraph_text(node))
        if not text:
            continue
        style = node.find("w:pPr/w:pStyle", _DOCX_NS)
        outline = node.find("w:pPr/w:outlineLvl", _DOCX_NS)
        style_name, style_outline = styles.get(style.get(_W + "val", ""), ("", None)) if style is not None else ("", None)
        level = _docx_heading_level(text, style_name, style_outline if outline is None else outline.get(_W + "val"))
        lines.append(("#" * level + " " + text) if level else text)
    result = "\n\n".join(lines)
    if "\x00" in result or "\ufffd" in result:
        raise ValueError("DOCX 中包含无法转换的字符。")
    if len(re.sub(r"[\W_]+", "", result)) < 30:
        raise ValueError("未找到足够的正文文字。DOCX 里的图片不会被识别，纯扫描件请先 OCR。")
    return result


def parse_document(path: Path, filename: str) -> list[dict]:
    suffix = Path(filename).suffix.lower()
    if suffix not in FORMATS:
        raise ValueError("请上传 Markdown、TXT 或 DOCX；扫描 PDF 请先完成 OCR。")
    if suffix == ".docx":
        text = docx_to_markdown(path)
    else:
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


def split_sections(sections: list[dict], max_chars=1200, overlap=160, include_offsets=False) -> list[dict]:
    """Optionally include code-point offsets into stripped sections joined by two newlines."""
    if not 0 <= overlap < max_chars:
        raise ValueError("Invalid chunk overlap")
    chunks = []
    source_offset = 0
    for section in sections:
        text = section["text"].strip()
        start = 0
        while start < len(text):
            end = min(start + max_chars, len(text))
            if end < len(text):
                boundary = text.rfind("\n\n", start + max_chars // 2, end)
                if boundary >= 0:
                    end = boundary
            raw = text[start:end]
            part = raw.strip()
            if part:
                chunk = {"ordinal": len(chunks) + 1, "text": part,
                         "section": section["section"], "page": section.get("page")}
                if include_offsets:
                    chunk["source_start"] = source_offset + start + len(raw) - len(raw.lstrip())
                    chunk["source_end"] = chunk["source_start"] + len(part)
                chunks.append(chunk)
            if len(chunks) > MAX_CHUNKS:
                raise ValueError("教材分块过多，请按卷拆分后上传。")
            if end == len(text):
                break
            start = max(start + 1, end - overlap)
        source_offset += len(text) + 2
    if not chunks:
        raise ValueError("教材没有可索引的正文。")
    return chunks
