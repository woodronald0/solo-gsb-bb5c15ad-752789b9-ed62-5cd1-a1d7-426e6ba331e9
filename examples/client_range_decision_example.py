#!/usr/bin/env python3
"""/client-range-decision 调用示例（仅依赖 Python 3 标准库）。

把模拟客户端的 Range / If-Range 请求头交给服务做前置决策，演示：
  1. 单段可满足范围：206 决策 + plan，逐块模拟回源后走 /assemble；
  2. 多段（闭区间 + 开放终点 + suffix，其中夹一个不可满足段）：206
     multipart/byteranges 决策，不可满足段被跳过；
  3. 全部不可满足：416 决策（Content-Range: bytes */总长）；
  4. If-Range 不匹配（旧 ETag）：忽略 Range，完整对象 200 决策。

用法：
    python examples/client_range_decision_example.py

环境变量：
    RANGE_PLANNER_URL  服务地址（默认 http://127.0.0.1:8080）
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = os.environ.get("RANGE_PLANNER_URL", "http://127.0.0.1:8080")

OBJECT_LENGTH = 256
ETAG = '"v3-strong"'
MAX_GAP = 8
FETCH_LIMIT = 128


def call(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code} on {path}: {exc.read().decode()}")
        sys.exit(1)


def decide(range_header: str | None, if_range: str | None = None,
           cached_blocks: list[dict] | None = None) -> dict:
    body: dict = {
        "object_length": OBJECT_LENGTH,
        "etag": ETAG,
        "max_gap": MAX_GAP,
        "fetch_limit": FETCH_LIMIT,
    }
    if range_header is not None:
        body["range_header"] = range_header
    if if_range is not None:
        body["if_range"] = if_range
    if cached_blocks is not None:
        body["cached_blocks"] = cached_blocks
    return call("/client-range-decision", body)


def show_decision(title: str, d: dict) -> None:
    print(f"== {title} ==")
    print(f"决策状态码: {d['status_code']}（{d['reason']}）")
    for name, value in d["headers"].items():
        print(f"{name}: {value}")
    if d["segments"]:
        for seg in d["segments"]:
            state = f"[{seg['start']},{seg['end']}]" if seg["satisfiable"] \
                else "跳过"
            print(f"  段{seg['index']} {seg['kind']}: {seg['reason']} {state}")
    print()


def fetch_and_assemble(obj: bytes, d: dict) -> dict:
    """按 206 决策的 plan 逐块模拟回源，再交给 /assemble 重组。"""
    plan = d["plan"]
    contents = []
    for blk in plan["blocks"]:
        s, e = blk["start"], blk["end"]
        contents.append({
            "block_index": blk["index"],
            "data_b64": base64.b64encode(obj[s : e + 1]).decode("ascii"),
        })
    return call("/assemble", {
        "object_length": OBJECT_LENGTH,
        "blocks": plan["blocks"],
        "mappings": plan["mappings"],
        "contents": contents,
        "cached_blocks": plan.get("cached_blocks", []),
    })


def main() -> None:
    obj = bytes((i % 256 for i in range(OBJECT_LENGTH)))

    # 1. 单段闭区间：206，单段 Content-Range 已确定。
    d = decide("bytes=0-99")
    show_decision("单段 bytes=0-99", d)
    assert d["status_code"] == 206 and d["media_type"] == "single"
    assembled = fetch_and_assemble(obj, d)
    first = assembled["ranges"][0]
    assert base64.b64decode(first["data_b64"]) == obj[0:100]
    print("  /assemble 重组第 0 段字节数：", first["length"], "\n")

    # 2. 多段混合 + 夹一个不可满足段：206 multipart，段 3 被跳过。
    d = decide("bytes=0-9, 240-, -16, 1000-2000")
    show_decision("多段 bytes=0-9, 240-, -16, 1000-2000", d)
    assert d["status_code"] == 206
    assert d["media_type"] == "multipart/byteranges"
    assert d["satisfied_count"] == 3 and d["skipped_count"] == 1

    # 3. 全部不可满足：416。
    d = decide("bytes=1000-2000")
    show_decision("不可满足 bytes=1000-2000", d)
    assert d["status_code"] == 416
    assert d["headers"]["Content-Range"] == f"bytes */{OBJECT_LENGTH}"

    # 4. If-Range 不匹配：完整对象 200，不产生 plan。
    d = decide("bytes=0-99", if_range='"stale-version"')
    show_decision('If-Range 为旧 ETag "stale-version"', d)
    assert d["status_code"] == 200 and d["reason"] == "if_range_mismatch"
    assert d["plan"] is None

    # 5. If-Range 匹配：照常 206。
    d = decide("bytes=0-99", if_range=ETAG)
    show_decision(f"If-Range 为当前 ETag {ETAG}", d)
    assert d["status_code"] == 206 and d["if_range_matched"] is True

    print("全部示例断言通过。")


if __name__ == "__main__":
    main()
