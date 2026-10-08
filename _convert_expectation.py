"""Convert 行政法上合法预期之保护.docx into a structured Markdown builtin book."""
import re
import sys
import zipfile
from xml.etree import ElementTree as ET
from pathlib import Path

sys.path.insert(0, ".")
from study.documents import _W, _docx_paragraph_text, _squeeze

SRC = Path(r"E:\本学期课程\部门行政法（读书报告）\行政法上合法预期之保护 (余凌云著, 余凌云, 1966- author, 余凌雲) (z-library.sk, 1lib.sk, z-lib.sk).docx")
DST = Path("builtin_books/行政法上合法预期之保护.md")
NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

with zipfile.ZipFile(SRC) as archive:
    document = ET.fromstring(archive.read("word/document.xml"))
body = document.find("w:body", NS)
lines = []
for node in body.iter(_W + "p"):
    text = _squeeze(_docx_paragraph_text(node))
    if text:
        lines.append(text)
print("paragraphs:", len(lines))

def norm(t):
    t = re.sub(r"\s+", "", t)
    return re.sub(r"(\d{1,4})$", "", t) if re.search(r"\d{1,4}$", t) and len(t) > 6 else t

PART = re.compile(r"^第[一二三四五六七八九十]+编")
SECTION = re.compile(r"^[一二三四五六七八九十]+\s*、")
SUB = re.compile(r"^\d{1,2}\s*\.\s*\d?\.?\s*\S")
CAPTION = re.compile(r"^(图|表)\s*\d")

def structural(t):
    return (len(t) <= 60 and not re.search(r"[。；]", t)
            and not CAPTION.match(t) and not t.startswith("①")
            and (PART.match(t) or SECTION.match(t) or SUB.match(t)))

# ---- main TOC block (目录 marker ... last page-numbered entry) ----
toc_idx = next(i for i, t in enumerate(lines) if re.fullmatch(r"目\s*录", t))
toc_entries = {}   # normalized title -> printed page
j = toc_idx + 1
last = toc_idx
while j < len(lines) and j - last < 30:
    t = lines[j]
    m = re.search(r"(\d{1,4})$", t)
    if m and len(t) < 70 and not re.search(r"[。；]", t):
        title = re.sub(r"\s+", "", t[:m.start()])
        # continuation of a previous page-less entry
        if title and title not in toc_entries:
            toc_entries[title] = int(m.group(1))
        last = j
        j += 1
        continue
    if not m and not re.search(r"[。；]", t) and len(t) < 70 and j <= last + 2:
        # Two-line TOC entry: register the page-less title with the page
        # found on the following line.
        for k in range(j + 1, min(j + 3, len(lines))):
            m2 = re.search(r"(\d{1,4})$", lines[k])
            if m2 and len(lines[k]) < 70:
                title = re.sub(r"\s+", "", t)
                if title and title not in toc_entries:
                    toc_entries[title] = int(m2.group(1))
                break
        j += 1
        continue
    if re.search(r"[。；]", t) or len(t) > 70:
        break
    j += 1
toc_end = last + 1
print("TOC block:", toc_idx, "->", toc_end, "entries:", len(toc_entries))

drop = set(range(0, toc_end))  # front matter + TOC block

# ---- duplicate suppression: mini-TOC listings vs running headers ----
kept = [i for i in range(len(lines)) if i not in drop]
seen_norm = {}
# index of next duplicate and prose-between flag
prose_after = {}
for pos, i in enumerate(kept):
    t = lines[i]
    prose_after[i] = any(len(lines[k]) > 60 or re.search(r"[。；]", lines[k]) for k in kept[pos + 1:pos + 40])

def is_footnote(t):
    return bool(re.match(r"^[①②③④⑤⑥⑦⑧⑨⑩*]", t)) or t.startswith("参见")

def span_prose_count(a, b):
    return sum(1 for k in range(a, b)
               if (len(lines[k]) > 60 or re.search(r"[。；]", lines[k])) and not is_footnote(lines[k]))

# ---- cluster detection: a dense run of structural lines is a chapter
# mini-TOC listing (>=8 members, gaps <=4 lines); body headings are isolated.
flags = {i: structural(lines[i]) for i in kept}
clusters = []
current = []
for i in kept:
    if flags[i]:
        if not current or i - current[-1] <= 4:
            current.append(i)
        else:
            clusters.append(current)
            current = [i]
if current:
    clusters.append(current)
toc_cluster = set()
for cluster in clusters:
    if len(cluster) >= 8:
        # A chapter mini-TOC lists every heading, then the body repeats the
        # first ones right below: texts appearing twice inside the cluster
        # keep only their LAST occurrence (the body heading).
        by_norm = {}
        for i in cluster:
            by_norm.setdefault(norm(lines[i]), []).append(i)
        for positions in by_norm.values():
            # Part titles are always real body headings, never listings.
            positions = [i for i in positions if not PART.match(lines[i]) or len(lines[i]) > 40]
            if not positions:
                continue
            if len(positions) >= 2:
                toc_cluster.update(positions[:-1])
            else:
                toc_cluster.update(positions)
print("mini-TOC clusters dropped:", sum(len(c) for c in clusters if len(c) >= 8),
      "clusters:", sum(1 for c in clusters if len(c) >= 8))

body_out = []
emitted_titles = set()
for i in kept:
    if i in toc_cluster:
        continue
    t = lines[i]
    n = norm(t)
    if PART.match(t) and len(t) <= 40:
        # Truncated running headers prefix an already-emitted part title.
        if n in emitted_titles or any(n != e and e.startswith(n) for e in emitted_titles):
            continue
        emitted_titles.add(n)
        body_out.append(("", f"## {t}", toc_entries.get(n)))
        continue
    if n in toc_entries and n not in emitted_titles and len(t) <= 60 and not re.search(r"[。；]", t) and not structural(t):
        emitted_titles.add(n)
        body_out.append(("", f"### {t}", toc_entries.get(n)))
        continue
    if structural(t):
        if SECTION.match(t):
            body_out.append(("", "#### " + re.sub(r"^([一二三四五六七八九十]+)\s*、", r"\1、", t), None))
        else:
            body_out.append(("", "##### " + re.sub(r"^(\d{1,2})\s*\.\s*(\d)", r"\1.\2", t), None))
        continue
    body_out.append((t, None, None))

# assemble markdown
md = []
for text, heading, page in body_out:
    if heading:
        md.append("")
        md.append(heading)
        md.append("")
    else:
        md.append(text)
out = "\n\n".join(md).strip()
# Strip PDF→docx converter metadata (JSON keys, braces, attribution) trailing the document.
out = "\n".join(l for l in out.splitlines()
                if not re.match(r'^"[a-z0-9_]+":', l) and l.strip() not in {"{", "}", "[", "]"}
                and not re.match(r"^(Document generated by|Images have been)", l)).strip()
DST.write_text(out, encoding="utf-8")
print("written:", DST, "chars:", len(out))

from study.documents import parse_document, split_sections
sections = parse_document(DST, DST.name)
chunks = split_sections(sections)
big = [s for s in sections if len(s["text"]) > 20000]
print("sections:", len(sections), "chunks:", len(chunks), "big:", len(big))
for s in sections[:14]:
    print("  ", len(s["text"]), f"p{s.get('page')}", s["section"][:56])
for s in big[:3]:
    print("  BIG:", len(s["text"]), s["section"][:50])
