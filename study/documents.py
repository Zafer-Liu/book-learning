"""UTF-8 textbook parsing with chapter-preserving, overlapping chunks."""

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

FORMATS = {".md", ".markdown", ".txt", ".docx"}
MAX_CHARS = 4_000_000
MAX_CHUNKS = 10_000
# Bump whenever parsing/chunking behaviour changes: stored indexes carry the
# version they were built with, and the builtin-book seeder reindexes (and
# invalidates conversations) when it sees a different one.
#   2 — self-closing fences, OCR-spaced labels, title-section merging,
#       sentence-boundary chunk tails.
#   3 — printed-TOC harvesting: TOC blocks dropped from the body, titles
#       form the heading skeleton, printed page numbers attached.
PARSER_VERSION = 3

_DOCX_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
_W = "{" + _DOCX_NS["w"] + "}"
_CJK = "\u3400-\u9fff"
_NUMBER = r"[一二三四五六七八九十百零〇两\d]+"


def _squeeze(text):
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(rf"(?<=[{_CJK}]) +(?=[{_CJK}])", "", text)


# OCR noise: CJK characters separated by single spaces ("诉 讼 行 为"),
# including the full-width spacing variant. Applied to headings AND body
# text that came from OCR exports.
_SPACED_CJK = re.compile(rf"(?<=[{_CJK}]) (?=[{_CJK}])")

# --- Table-of-contents awareness -------------------------------------
# Textbook exports almost always carry a TOC page before the body. It is the
# authoritative structure (titles + printed page numbers), so we (a) drop it
# from the indexed body — a TOC page pollutes retrieval — and (b) use its
# entries as a heading skeleton with page numbers no OCR body line can offer.

_TOC_TITLE = re.compile(rf"^\s*第{_NUMBER}?(?:分编|[编章节篇部分])|"
                        rf"^\s*{_NUMBER}\s*[、．.]|"
                        rf"^\s*\d{{1,2}}(?:[．.]\d{{1,2}}){{0,2}}\s+")


def _toc_normalize(text: str) -> str:
    """Fold a title for matching: drop OCR spaces, dots and page digits."""
    text = _SPACED_CJK.sub("", text)
    return re.sub(r"[\s.·…·]+\(?\d{1,4}\)?\s*$", "", text).strip()


def _toc_level(title: str) -> int:
    if re.match(rf"^第{_NUMBER}[编篇]", title):
        return 1
    if re.match(rf"^第{_NUMBER}[部分]", title):
        return 2
    if re.match(rf"^第{_NUMBER}章", title):
        return 3
    if re.match(rf"^第{_NUMBER}节", title):
        return 4
    if re.match(rf"^{_NUMBER}\s*[、．.]", title):
        return 5
    return 6


def _toc_candidate(raw: str):
    """One 'title …… page' line in any spacing flavour, or None."""
    raw = raw.strip()
    if not 2 <= len(raw) <= 70:
        return None
    match = re.search(r"[\s.·…]*\(?(\d{1,4})\)?\s*$", raw)
    if not match or match.start() == 0:
        return None
    title = _toc_normalize(raw)
    if not title or len(title) > 60 or re.search(r"[。！？；，：/]", title):
        return None
    if len(re.sub(r"[\W_]+", "", title)) < 4:
        return None
    return title, int(match.group(1))


def _extract_toc(lines):
    """Harvest the book's title→page skeleton.

    Two sources:
    1. Printed TOC blocks — a cluster of 6+ "title … page" candidates within
       a 5-line sliding window (blank/junk lines tolerated between them).
       The whole cluster span is dropped from the indexed body.
    2. Scattered running headers ("第一节 宪法实施    319" at page tops) —
       kept in the body but collected as entries when frequent enough, so
       real body headings (which carry no page digits) resolve to pages.

    Returns (entries, spans): entries map normalized titles to (level, page).
    """
    count = len(lines)
    candidates = []
    for i in range(count):
        found = _toc_candidate(lines[i])
        if found:
            candidates.append((i, found[0], found[1]))

    entries, spans = {}, []
    cluster = []

    def emit(cluster_items, drop):
        if len(cluster_items) < 6:
            return
        if drop:
            spans.append((cluster_items[0][0], cluster_items[-1][0] + 1))
        for _, title, page in cluster_items:
            if title not in entries:
                entries[title] = (_toc_level(title), page)

    for item in candidates:
        if cluster and item[0] - cluster[-1][0] > 5:
            emit(cluster, drop=True)
            cluster = []
        cluster.append(item)
    emit(cluster, drop=True)

    # Running-header harvest: structural titles with page digits scattered
    # through the text (never a TOC cluster — those were consumed above).
    headers = [(i, t, p) for i, t, p in candidates
               if _TOC_TITLE.match(t) and not any(s <= i < e for s, e in spans)]
    if len(headers) >= 10:
        for _, title, page in headers:
            if title not in entries:
                entries[title] = (_toc_level(title), page)
    return entries, spans


def _dehyphenate(lines):
    """Merge hard-wrapped paragraphs: a line that ends mid-clause joins the
    next line unless a blank line, heading or block marker separates them."""
    merged = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            merged.append("")
            continue
        if merged and merged[-1]:
            previous = merged[-1]
            # Previous line continues mid-sentence (no closing punctuation),
            # and this line is not a heading/list/fence start.
            continues = (not re.search(r"[。！？：；」”』)]\s*$", previous)
                         and not re.match(r"[#>*|\-`~\d]", stripped)
                         and len(previous) < 400)
            if continues:
                merged[-1] = previous + stripped
                continue
        merged.append(line)
    return merged


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
    text = _SPACED_CJK.sub("", text)
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

    all_lines = text.splitlines()
    # The printed TOC is the authoritative skeleton: capture its entries
    # (title -> level, page) and drop its line spans from the body.
    toc_entries, toc_spans = _extract_toc(all_lines)
    in_toc = [False] * len(all_lines)
    for start, end in toc_spans:
        for k in range(start, end):
            in_toc[k] = True
    sections, headings, buffer = [], [], []
    label = "正文"
    page_hint = None
    fence = None
    lines = all_lines
    index = 0

    def flush():
        body = "\n".join(buffer).strip()
        if body:
            sections.append({"text": body, "section": label, "page": page_hint[0] if page_hint else None})
        buffer.clear()

    while index < len(lines):
        line = lines[index]
        # Printed-TOC pages never enter the indexed body.
        if in_toc[index]:
            index += 1
            continue
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
        if marker and not fence:
            token, rest = marker.group(1), marker.group(2).strip()
            # Self-closing fence on one line ("```markdown ```") — an OCR
            # artifact around exercise boxes — must not swallow the rest of
            # the book as code.
            self_closing = token[0] == "`" and token in rest
            if not self_closing:
                fence = token
            buffer.append(line)
            index += 1
            continue
        if marker and fence:
            token = marker.group(1)
            if token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            buffer.append(line)
            index += 1
            continue
        heading = None if fence else re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        level, title, skip = 0, "", 1
        if heading:
            level, title = len(heading.group(1)), _SPACED_CJK.sub("", heading.group(2))
        elif not fence and line.strip() and index + 1 < len(lines):
            underline = re.match(r"^\s{0,3}(={3,}|-{3,})\s*$", lines[index + 1])
            if underline:
                level = 1 if underline.group(1)[0] == "=" else 2
                title, skip = _SPACED_CJK.sub("", line.strip()), 2
        if not level and not fence and toc_entries and line.strip() and len(line.strip()) <= 60:
            # TOC skeleton: a body line whose normalized form matches a TOC
            # entry is a heading even without # markers — with its printed
            # page number attached. Lines that themselves end in a page
            # number are running headers, not headings.
            stripped = line.strip()
            if not re.search(r"\d{1,4}\s*$", stripped):
                candidate = _toc_normalize(stripped)
                if candidate in toc_entries and not re.search(r"[。！？；，]", candidate):
                    entry_level, entry_page = toc_entries[candidate]
                    level, title = entry_level, candidate
                    page_hint = (entry_page,)
        if level:
            flush()
            page_hint = (toc_entries.get(title, (0, None))[1],) if toc_entries and title in toc_entries else page_hint
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, title[:180]))
            label = " / ".join(item[1] for item in headings)
            buffer.append(title)
        else:
            buffer.append(line)
        index += skip
    flush()
    # Merge title-only sections into their successor: the heading text is
    # already repeated as the section's first line, so a bare-title section
    # carries no evidence and only pollutes the chapter picker.
    merged = []
    for section in sections:
        body = section["text"].strip()
        if merged and len(re.sub(r"\s", "", body)) <= 20 and body == merged[-1]["section"].split(" / ")[-1]:
            merged[-1]["text"] = (body + "\n\n" + merged[-1]["text"].strip()).strip()
            merged[-1]["section"] = merged[-1]["section"].rsplit(" / ", 1)[0] or merged[-1]["section"]
            continue
        if merged and len(re.sub(r"\s", "", body)) <= 20 and body in merged[-1]["text"][:len(body) + 4]:
            # A tiny lead-in identical to the previous section's heading tail.
            merged[-1]["text"] = merged[-1]["text"]
            continue
        merged.append(section)
    return merged


def split_sections(sections: list[dict], max_chars=1200, overlap=160, include_offsets=False) -> list[dict]:
    """Optionally include code-point offsets into stripped sections joined by two newlines."""
    if not 0 <= overlap < max_chars:
        raise ValueError("Invalid chunk overlap")
    chunks = []
    source_offset = 0
    # Prefer sentence-final boundaries (。！？etc.) over paragraph breaks at
    # arbitrary positions: chunks ending mid-clause read broken in citations.
    SENTENCE_END = re.compile(r"[。！？；」”](?=[" + r"\s" + r"]|$)")

    for section in sections:
        text = _SPACED_CJK.sub("", section["text"].strip())
        start = 0
        while start < len(text):
            end = min(start + max_chars, len(text))
            if end < len(text):
                # First try a paragraph boundary, then the latest sentence end
                # past the midpoint; both keep chunks readable.
                boundary = text.rfind("\n\n", start + max_chars // 2, end)
                if boundary < 0:
                    window = text[start + max_chars // 2:end + 40]
                    best = None
                    for match in SENTENCE_END.finditer(window):
                        best = match
                    if best and start + max_chars // 2 + best.start() + 1 > start + max_chars // 2:
                        boundary = start + max_chars // 2 + best.start() + 1
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
