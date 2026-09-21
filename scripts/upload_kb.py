# -*- coding: utf-8 -*-
"""批量上传知识文件到正在运行的服务（POST /api/v1/ingest）。

用法：
    python scripts/upload_kb.py <文件或目录> [更多路径...]
    python scripts/upload_kb.py data/                # 上传整个目录（只挑支持的后缀）
    python scripts/upload_kb.py a.pdf b.docx         # 指定文件

说明：
- 只上传服务端支持的 .pdf / .docx / .md / .txt / .markdown，其他后缀跳过（服务端会返回 400）。
- 服务端限流是「按 IP、60 秒 30 次」，批量上传会撞 429；脚本遇到 429 会自动等待后重试。
- 知识库是进程内内存态：服务重启后本次上传的内容会丢失（启动只自动导入 data/knowledge_base.md）。
"""
import io
import os
import sys
import time
from pathlib import Path

import httpx

BASE = os.environ.get("AIROBOT_BASE", "http://127.0.0.1:8000")
SUPPORTED = {".pdf", ".docx", ".md", ".txt", ".markdown"}
MAX_RETRY_ON_429 = 5


def collect(paths):
    files = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            for child in sorted(p.rglob("*")):
                if child.is_file() and child.suffix.lower() in SUPPORTED:
                    files.append(child)
        elif p.is_file():
            if p.suffix.lower() in SUPPORTED:
                files.append(p)
            else:
                print(f"跳过（不支持的后缀）: {p}")
        else:
            print(f"跳过（不存在）: {p}")
    return files


def upload(client, path):
    for attempt in range(MAX_RETRY_ON_429 + 1):
        with io.open(path, "rb") as fh:
            files = {"file": (path.name, fh, "application/octet-stream")}
            t0 = time.perf_counter()
            r = client.post("/api/v1/ingest", files=files)
        dt = round(time.perf_counter() - t0, 2)
        if r.status_code == 429 and attempt < MAX_RETRY_ON_429:
            wait = 10 * (attempt + 1)
            print(f"  触发限流(429)，等 {wait}s 后重试 ({attempt + 1}/{MAX_RETRY_ON_429}) ...")
            time.sleep(wait)
            continue
        try:
            body = r.json()
        except Exception:
            body = r.text[:200]
        return r.status_code, dt, body
    return 429, 0.0, "重试次数用尽"


def main():
    paths = sys.argv[1:]
    if not paths:
        print(__doc__)
        return
    files = collect(paths)
    if not files:
        print("没有可上传的文件")
        return

    client = httpx.Client(base_url=BASE, timeout=600.0)
    try:
        total_before = client.get("/api/v1/stats").json().get("total_chunks")
    except Exception as exc:
        print(f"无法连接服务 {BASE}: {exc}（先跑 .\\start-local.ps1）")
        return

    print(f"服务: {BASE} ｜ 上传前知识库分块: {total_before} ｜ 待上传文件: {len(files)}")
    ok = failed = 0
    for path in files:
        status, dt, body = upload(client, path)
        if status == 200 and isinstance(body, dict):
            ok += 1
            print(f"[OK] {path.name}: {body.get('chunks')} 块（累计 {body.get('total_chunks')}） {dt}s")
        else:
            failed += 1
            print(f"[FAIL] {path.name}: http={status} {body}")

    total_after = client.get("/api/v1/stats").json().get("total_chunks")
    print(f"\n完成：成功 {ok} / 失败 {failed}；知识库分块 {total_before} -> {total_after}")


if __name__ == "__main__":
    main()
