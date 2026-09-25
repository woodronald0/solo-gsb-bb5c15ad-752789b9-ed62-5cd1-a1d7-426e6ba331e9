#!/usr/bin/env python3
"""/range-response 调用示例（仅依赖 Python 3 标准库）。

脚本走完 /plan → 模拟上游 206 回源 → /assemble → /range-response 全链路，
分别演示：
  1. 单段范围：直接得到可下发的 206 响应（范围字节 + Content-Range）；
  2. 多段范围：得到 multipart/byteranges 完整二进制正文，并回交给
     /multipart-parse 验证生成结果可被严格解析且逐字节一致。

用法：
    python examples/range_response_example.py

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
CONTENT_TYPE = "application/octet-stream"


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


def plan_assemble(obj: bytes, ranges: list[dict]) -> dict:
    """/plan → 用对象字节模拟逐块回源 → /assemble，返回重组结果。"""
    plan = call("/plan", {
        "object_length": OBJECT_LENGTH,
        "ranges": ranges,
        "max_gap": 8,
        "fetch_limit": 128,
    })
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
    })


def show_result(title: str, result: dict) -> bytes:
    print(f"== {title} ==")
    print(f"HTTP {result['status_code']}")
    for name, value in result["headers"].items():
        print(f"{name}: {value}")
    body = base64.b64decode(result["body_b64"])
    assert len(body) == result["content_length"], "Content-Length 与正文不一致"
    print(f"(正文 {len(body)} 字节，media_type={result['media_type']})\n")
    return body


def main() -> None:
    obj = bytes((i % 256 for i in range(OBJECT_LENGTH)))

    # 1) 单段：206 直接输出范围字节与 Content-Range。
    assembled_single = plan_assemble(
        obj, [{"type": "interval", "start": 0, "end": 99}]
    )
    single = call("/range-response", {
        "object_length": OBJECT_LENGTH,
        "ranges": assembled_single["ranges"],
        "content_type": CONTENT_TYPE,
    })
    single_body = show_result("单段 206（bytes=0-99）", single)
    assert single_body == obj[0:100], "单段正文与对象范围不一致"
    assert single["headers"]["Content-Range"] == "bytes 0-99/256"

    # 2) 多段：multipart/byteranges，按下标顺序包含两段；回交给
    #    /multipart-parse 做严格解析，验证生成结果可直接作为 HTTP 响应下发。
    ranges = [
        {"type": "interval", "start": 0, "end": 99},
        {"type": "suffix", "last_n": 16},
    ]
    assembled_multi = plan_assemble(obj, ranges)
    multi = call("/range-response", {
        "object_length": OBJECT_LENGTH,
        "ranges": assembled_multi["ranges"],
        "content_type": CONTENT_TYPE,
    })
    multi_body = show_result("多段 206（bytes=0-99 与末尾 16 字节）", multi)
    assert multi["media_type"] == "multipart/byteranges"
    assert multi["boundary"], "多段响应必须返回 boundary"

    parsed = call("/multipart-parse", {
        "object_length": OBJECT_LENGTH,
        "ranges": ranges,
        "boundary": multi["boundary"],
        "expected_content_type": CONTENT_TYPE,
        "body_b64": multi["body_b64"],
    })
    assert base64.b64decode(parsed["ranges"][0]["data_b64"]) == obj[0:100]
    assert base64.b64decode(parsed["ranges"][1]["data_b64"]) == obj[240:256]

    print("OK: /range-response 的单段正文与多段 multipart 均与原对象逐字节一致，")
    print("    且生成的 multipart/byteranges 可被 /multipart-parse 严格解析。")


if __name__ == "__main__":
    main()
