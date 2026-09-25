#!/usr/bin/env python3
"""/verify-fetch-batch 调用示例（仅依赖 Python 3 标准库）。

脚本完整演示一次典型流程：
  1. 调用 /plan 规划回源块；
  2. 模拟上游对每个 Range 请求返回 206 Partial Content（携带
     Content-Range、强 ETag 与响应体）；
  3. 把各块的状态码、Content-Range、ETag 与 Base64 响应体按块编号提交给
     /verify-fetch-batch 严格校验；
  4. 用校验通过后返回的 contents（无需任何改动）连同 blocks、mappings
     直接提交 /assemble，取回原请求范围并逐字节核对。

用法：
    python examples/verify_fetch_batch_example.py

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
# 模拟上游当前对象版本的强 ETag（弱标签 W/ 会被服务拒绝）。
ETAG = '"demo-v1"'


def post_json(path: str, payload: dict) -> dict:
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
        print(f"POST {path} -> HTTP {exc.code}: {exc.read().decode()}")
        sys.exit(1)


def fake_upstream_fetch(obj: bytes, start: int, end: int) -> dict:
    """模拟上游对 Range: bytes=start-end 的 206 响应要素。"""
    return {
        "status_code": 206,
        "content_range": f"bytes {start}-{end}/{len(obj)}",
        "etag": ETAG,
        "body": obj[start : end + 1],
    }


def main() -> None:
    obj = bytes((i % 256 for i in range(OBJECT_LENGTH)))

    # 1. 规划：请求 [0,99] 与末尾 16 字节。
    plan = post_json(
        "/plan",
        {
            "object_length": OBJECT_LENGTH,
            "ranges": [
                {"type": "interval", "start": 0, "end": 99},
                {"type": "suffix", "last_n": 16},
            ],
            "max_gap": 8,
            "fetch_limit": 128,
        },
    )
    print("blocks:", [(b["index"], b["start"], b["end"]) for b in plan["blocks"]])

    # 2. 按 range_header 逐块“回源”（这里用本地对象模拟）。
    responses = []
    for block in plan["blocks"]:
        upstream = fake_upstream_fetch(obj, block["start"], block["end"])
        responses.append(
            {
                "block_index": block["index"],
                "status_code": upstream["status_code"],
                "content_range": upstream["content_range"],
                "etag": upstream["etag"],
                "body_b64": base64.b64encode(upstream["body"]).decode("ascii"),
            }
        )

    # 3. 校验整批回源：状态码/Content-Range/字节数/强 ETag 全部一致才通过。
    verified = post_json(
        "/verify-fetch-batch",
        {
            "object_length": OBJECT_LENGTH,
            "blocks": plan["blocks"],
            "responses": responses,
            "expected_etag": ETAG,
        },
    )
    print("confirmed etag:", verified["etag"])
    assert verified["etag"] == ETAG
    assert [c["block_index"] for c in verified["contents"]] == sorted(
        c["block_index"] for c in verified["contents"]
    )

    # 4. contents 原样提交 /assemble（无需重新编码或调整顺序）。
    assembled = post_json(
        "/assemble",
        {
            "object_length": OBJECT_LENGTH,
            "blocks": plan["blocks"],
            "mappings": plan["mappings"],
            "contents": verified["contents"],
        },
    )

    expected_spans = [(0, 99), (240, 255)]
    for item, (start, end) in zip(assembled["ranges"], expected_spans):
        actual = base64.b64decode(item["data_b64"])
        assert actual == obj[start : end + 1], f"范围 {item['index']} 内容不一致"
    print("OK: verify-fetch-batch 校验通过，assemble 重组结果与原对象逐字节一致")


if __name__ == "__main__":
    main()
