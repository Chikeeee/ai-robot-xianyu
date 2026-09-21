# -*- coding: utf-8 -*-
"""检索链路诊断：复刻服务启动时的入库 + search_detailed 内部各阶段，看正确分块到底在哪一步掉队。

只读脚本：不写知识库、不改 .env，直接调用项目真实代码路径。
用法：python scripts/diagnose_retrieval.py
"""
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1,api.deepseek.com")

from app.rag.fusion import reciprocal_rank_fusion  # noqa: E402
from app.rag.retriever import kb  # noqa: E402

DATA = Path(r"D:\dsh\ai-robot-agent\data")
SUPPORTED = {".md", ".markdown", ".txt", ".pdf", ".docx"}

# 1) 复刻服务启动的入库过程
for p in sorted(x for x in DATA.glob("*") if x.is_file() and x.suffix.lower() in SUPPORTED):
    n = kb.ingest_file(p)
    print(f"ingest {p.name}: {n} chunks (total {kb.chunk_count})")

QUERIES = [
    ("Q1 投诉/法律风险", "遇到投诉、辱骂或者法律风险的问题，客服该怎么处理？", "ai客服.md#1"),
    ("Q2 转人工话术", "通用的转人工话术是什么？", "ai客服.md#1"),
    ("Q3 售前收费/试用", "产品怎么收费？支持试用吗？", "^ai客服.md#(4|6)$"),
    ("Q4 退货运费(原知识库)", "退货运费谁承担？", "knowledge_base.md#4"),
]

PARAM_SETS = [
    ("baseline  top_k=5  vec=15 bm25=15 fusion=10", dict(top_k=5, vt=15, bt=15, ft=10)),
    ("放宽召回  top_k=8  vec=20 bm25=20 fusion=20", dict(top_k=8, vt=20, bt=20, ft=20)),
    ("更宽      top_k=10 vec=30 bm25=30 fusion=30", dict(top_k=10, vt=30, bt=30, ft=30)),
]


def probe(query, top_k, vt, bt, ft):
    vec = kb.search_vector(query, vt)
    bm = kb.search_bm25(query, bt)
    ids_v = [d.metadata.get("doc_id") for d in vec]
    ids_b = [d.metadata.get("doc_id") for d in bm]
    fused = reciprocal_rank_fusion([ids_v, ids_b], top_k=ft)
    doc_map = {d.metadata.get("doc_id"): d for d in vec + bm}
    final = [doc_map[i] for i in fused if i in doc_map][:top_k]
    return ids_v, ids_b, fused, [d.metadata.get("doc_id") for d in final]


lines = []
for name, params in PARAM_SETS:
    lines.append("=" * 100)
    lines.append(f"参数: {name}")
    lines.append("=" * 100)
    for tag, q, expect in QUERIES:
        ids_v, ids_b, fused, final = probe(q, **params)
        lines.append(f"\n{tag}  期望命中: {expect}")
        lines.append(f"  向量 top: {ids_v}")
        lines.append(f"  BM25 top: {ids_b}")
        lines.append(f"  RRF 融合: {fused}")
        lines.append(f"  >>> 最终进 prompt: {final}")
        lines.append(f"  期望块是否在最终结果里: {expect in final}")

out = "\n".join(lines)
io.open(r"D:\dsh\ai-robot-agent\.logs\retrieval_diagnosis.txt", "w", encoding="utf-8").write(out)
print("\nwritten -> .logs\\retrieval_diagnosis.txt")
