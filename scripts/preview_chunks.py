# -*- coding: utf-8 -*-
"""复刻 retriever._split_markdown 的分块结果，检查第 N 块到底装了什么（只读，不调服务）。"""
import io

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

CHUNK_SIZE = 400
CHUNK_OVERLAP = 80

path = r"D:\dsh\ai-robot-agent\data\ai客服.md"
text = io.open(path, encoding="utf-8-sig").read()

md_splitter = MarkdownHeaderTextSplitter(
    headers_to_split_on=[("#", "H1"), ("##", "H2"), ("###", "H3"), ("####", "H4")],
    strip_headers=False,
)
rec_splitter = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP,
    separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""],
)

max_section_len = max(CHUNK_SIZE * 2, CHUNK_SIZE + 200)
chunks = []
for section in md_splitter.split_text(text):
    if len(section.page_content) <= max_section_len:
        chunks.append(section)
    else:
        chunks.extend(rec_splitter.split_documents([section]))

lines = [f"总块数 = {len(chunks)}", ""]
for i, c in enumerate(chunks):
    body = c.page_content.replace("\n", " / ")
    lines.append(f"[{i}] len={len(c.page_content)} :: {body[:110]}")

io.open(r"D:\dsh\ai-robot-agent\.logs\chunks_preview.txt", "w", encoding="utf-8").write("\n".join(lines))
print("total chunks =", len(chunks))
