# -*- coding: utf-8 -*-
"""验证 BM25 索引与 _documents 是否错位（多文件入库后怀疑 add_documents 是覆盖而非累加）。"""
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1,api.deepseek.com")

from app.rag.retriever import kb  # noqa: E402

DATA = Path(r"D:\dsh\ai-robot-agent\data")
SUPPORTED = {".md", ".markdown", ".txt", ".pdf", ".docx"}

for p in sorted(x for x in DATA.glob("*") if x.is_file() and x.suffix.lower() in SUPPORTED):
    kb.ingest_file(p)

docs = kb._documents
corpus = kb._bm25._corpus

lines = []
lines.append(f"_documents 块数 = {len(docs)}")
lines.append(f"BM25 corpus 条数 = {len(corpus)}   <- 若不等，说明 BM25 只索引了最后一次入库的文件")
lines.append(f"BM25 ready = {kb._bm25.ready}")
lines.append("")
lines.append("逐条比对 _documents[i].page_content 与 BM25 corpus[i]（前 20 条）：")
mismatch = 0
for i in range(min(20, len(docs), len(corpus))):
    a = docs[i].page_content.replace("\n", " ")[:50]
    b = corpus[i].replace("\n", " ")[:50]
    same = a == b
    if not same:
        mismatch += 1
    lines.append(f"[{i}] doc_id={docs[i].metadata.get('doc_id'):<18} 一致={same}")
    lines.append(f"      _documents : {a}")
    lines.append(f"      bm25 corpus: {b}")
lines.append("")
lines.append(f"前 20 条中错位条数 = {mismatch}")

# 用真实查询验证：BM25 命中的 index 拿到的文档，内容是否真的与查询相关
q = "退货运费谁承担？"
idx = kb._bm25.search(q, 5)
lines.append("")
lines.append(f"查询「{q}」BM25 原始 index = {idx}")
for i in idx:
    lines.append(f"  index {i} -> _documents[{i}] = {docs[i].metadata.get('doc_id')} | 内容: {docs[i].page_content.replace(chr(10), ' ')[:60]}")
    lines.append(f"            BM25 corpus[{i}] 实际内容: {corpus[i].replace(chr(10), ' ')[:60] if i < len(corpus) else 'N/A'}")

io.open(r"D:\dsh\ai-robot-agent\.logs\bm25_alignment.txt", "w", encoding="utf-8").write("\n".join(lines))
print("written -> .logs\\bm25_alignment.txt")
print("docs =", len(docs), "| bm25 corpus =", len(corpus))
