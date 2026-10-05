"""Convert the trust-law txt export into a structured Markdown builtin book."""
import re
import pathlib

SRC = pathlib.Path(r"C:\Users\Administrator\Downloads\衡平法与信托的原理(套装共2册) (格雷厄姆·弗戈) (z-library.sk, 1lib.sk, z-lib.sk).txt")
DST = pathlib.Path("builtin_books/衡平法与信托的原理.md")

lines = SRC.read_text(encoding="utf-8").splitlines()

# ---- locate the two printed TOC blocks (front of vol.1 and vol.2) ----
toc_starts = [i for i, l in enumerate(lines) if l.strip() == "目录"]
assert len(toc_starts) == 2, toc_starts

def toc_span(start):
    """From the 目录 marker: the TOC is a dense run of structural title
    lines (this export lists bare titles, no page numbers). The run breaks
    at the first prose line (long or sentence-punctuated); if fewer than 6
    structural lines were seen before prose, this 目录 marker has no real
    TOC after it. The run ends BEFORE the body repeats those titles."""
    j = start + 1
    last_structural = None
    run_end = start
    count = 0
    while j < len(lines) and j - (last_structural or start) < 40:
        raw = lines[j].strip()
        if not raw:
            j += 1
            continue
        if re.match(r"^(第[一二三四五六七八九十百]+部分|第\d{1,2}章|\d{1,2}\.\d{1,2}(\.\d{1,2})?)　", raw) \
                and len(raw) <= 60 and not raw.startswith("图"):
            last_structural = j
            count += 1
            run_end = j + 1
            j += 1
            continue
        if raw == "返回总目录" or raw == "目录结束":
            return (start, run_end if count >= 6 else start + 1)
        # Prose reached: stop; if density was too low this marker had no TOC.
        if count < 6:
            return (start, start + 1)
        break
    if count < 6:
        return (start, start + 1)
    return start, run_end

span1, span2 = toc_span(toc_starts[0]), toc_span(toc_starts[1])
print("TOC spans:", span1, span2)

drop = set(range(*span1)) | set(range(*span2))
# Front matter: everything before the TOC (CIP, ISBN, dedication) is noise.
front_end = span1[0]

out = []
for i, line in enumerate(lines):
    if i in drop or i < front_end:
        continue
    stripped = line.strip()
    if not stripped:
        continue
    # Structural headings use an ideographic space; convert to # marks.
    if re.match(r"^第[一二三四五六七八九十百]+部分　", stripped):
        out.append("")
        out.append("## " + stripped.replace("　", " "))
        continue
    m = re.match(r"^第(\d{1,2})章　(.+)$", stripped)
    if m:
        out.append("")
        out.append(f"### 第{m.group(1)}章 {m.group(2).strip()}")
        continue
    m = re.match(r"^(\d{1,2}\.\d{1,2})　(.+)$", stripped)
    if m:
        out.append("")
        out.append(f"#### {m.group(1)} {m.group(2).strip()}")
        continue
    m = re.match(r"^(\d{1,2}\.\d{1,2}\.\d{1,2})　(.+)$", stripped)
    if m:
        out.append("")
        out.append(f"##### {m.group(1)} {m.group(2).strip()}")
        continue
    out.append(stripped)

text = "\n\n".join(x for x in out).strip()
# Squeeze the OCR double-space artifacts inside CJK runs.
text = re.sub(r"(?<=[\u3400-\u9fff])  +(?=[\u3400-\u9fff])", "", text)
DST.write_text(text, encoding="utf-8")
print("written:", DST, "chars:", len(text))

# ---- verify with the production parser ----
import sys
sys.path.insert(0, ".")
from study.documents import parse_document, split_sections
sections = parse_document(DST, DST.name)
pages = sum(1 for s in sections if s.get("page"))
chunks = split_sections(sections)
names = [s["section"] for s in sections]
print("sections:", len(sections), "with-page:", pages, "chunks:", len(chunks))
print("first labels:", names[:6])
big = [s for s in sections if len(s["text"]) > 20000]
print("big sections:", [(len(s["text"]), s["section"][:40]) for s in big[:4]])
