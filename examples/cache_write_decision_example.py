#!/usr/bin/env python3
"""/cache-write-decision 调用示例（仅依赖 Python 3 标准库）。

脚本完整演示一次典型流程：
  1. 调用 /plan 规划回源块；
  2. 模拟上游对每个 Range 请求返回 206 Partial Content（携带
     Content-Range、强 ETag、Cache-Control、Age、Vary 与响应体）；
  3. 用 /verify-fetch-batch 验收整批分块并确认强 ETag；
  4. 逐块调用 /cache-write-decision 判断能否写入共享缓存：可缓存时
     取回剩余秒数与稳定缓存键，并验证请求头字段名大小写不影响缓存键；
  5. 演示两种拒绝场景：Cache-Control: no-store，以及请求携带
     Authorization 而响应未显式允许共享缓存。

用法：
    python examples/cache_write_decision_example.py

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
OBJECT_URI = "https://objects.example.com/demo/object.bin"
# 模拟上游当前对象版本的强 ETag（弱标签 W/ 会被服务拒绝）。
ETAG = '"demo-v1"'
# 模拟上游 206 响应携带的缓存相关头：共享缓存 120 秒（s-maxage 优先于
# max-age=60）、已在链路缓存停留 20 秒、按 Accept-Encoding 区分变体。
UPSTREAM_CACHE_CONTROL = "s-maxage=120, max-age=60, public"
UPSTREAM_AGE = "20"
UPSTREAM_VARY = "Accept-Encoding"
# 代理收到的原请求头（决定缓存键中的 Vary 成分）。
CLIENT_HEADERS = {"Accept-Encoding": "gzip", "User-Agent": "range-demo/1.0"}


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


def decide(start: int, end: int, **over) -> dict:
    """对闭区间 [start, end] 的分块调用 /cache-write-decision。"""
    body = {
        "uri": OBJECT_URI,
        "start": start,
        "end": end,
        "etag": ETAG,
        "request_headers": CLIENT_HEADERS,
        "cache_control": UPSTREAM_CACHE_CONTROL,
        "age": UPSTREAM_AGE,
        "vary": UPSTREAM_VARY,
    }
    body.update(over)
    return post_json("/cache-write-decision", body)


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
    responses = [
        {
            "block_index": block["index"],
            "status_code": 206,
            "content_range": (
                f"bytes {block['start']}-{block['end']}/{OBJECT_LENGTH}"
            ),
            "etag": ETAG,
            "body_b64": base64.b64encode(
                obj[block["start"] : block["end"] + 1]
            ).decode("ascii"),
        }
        for block in plan["blocks"]
    ]

    # 3. 验收整批回源，确认全批统一的强 ETag。
    verified = post_json(
        "/verify-fetch-batch",
        {
            "object_length": OBJECT_LENGTH,
            "blocks": plan["blocks"],
            "responses": responses,
            "expected_etag": ETAG,
        },
    )
    assert verified["etag"] == ETAG
    print("confirmed etag:", verified["etag"])

    # 4. 逐块判定能否写入共享缓存。s-maxage=120 优先于 max-age=60，
    #    扣除 Age=20 后剩余 100 秒；缓存键随块区间变化。
    keys = set()
    for block in plan["blocks"]:
        decision = decide(block["start"], block["end"])
        assert decision["cacheable"] and decision["reason"] == "cacheable"
        assert decision["remaining_seconds"] == 100  # 120 - 20
        assert decision["vary_headers"] == ["accept-encoding"]
        keys.add(decision["cache_key"])
        print(
            f"block [{block['start']},{block['end']}] -> cacheable, "
            f"remaining={decision['remaining_seconds']}s, "
            f"key={decision['cache_key'][:16]}..."
        )
    assert len(keys) == len(plan["blocks"]), "不同块的缓存键必须不同"

    # 4b. 请求头字段名大小写不影响缓存键。
    d1 = decide(0, 99, request_headers={"Accept-Encoding": "gzip"})
    d2 = decide(0, 99, request_headers={"accept-encoding": "gzip"})
    assert d1["cache_key"] == d2["cache_key"], "字段名大小写不得影响缓存键"
    # Vary 取值变化则缓存键变化（不同变体不串缓存）。
    d3 = decide(0, 99, request_headers={"Accept-Encoding": "br"})
    assert d3["cache_key"] != d1["cache_key"]
    print("cache key: 字段名大小写不影响结果，Vary 取值区分变体")

    # 5a. no-store：响应禁止任何缓存写入。
    d = decide(0, 99, cache_control="no-store")
    assert not d["cacheable"] and d["reason"] == "no_store"
    assert d["cache_key"] is None and d["remaining_seconds"] is None
    print("no-store -> 拒绝写入:", d["reason"])

    # 5b. 请求携带 Authorization 且响应未显式允许共享（无
    #     must-revalidate / public / s-maxage）：共享缓存不得写入。
    d = decide(
        0,
        99,
        request_headers={"Authorization": "Bearer secret-token"},
        cache_control="max-age=300",
    )
    assert not d["cacheable"] and d["reason"] == "authorization_not_shareable"
    print("Authorization 未获共享许可 -> 拒绝写入:", d["reason"])
    # 响应显式 public 时同一块即可写入（仍扣除默认的 Age=20）。
    d = decide(
        0,
        99,
        request_headers={"Authorization": "Bearer secret-token"},
        cache_control="max-age=300, public",
    )
    assert d["cacheable"] and d["remaining_seconds"] == 280  # 300 - 20
    print("响应显式 public -> 允许写入:", d["reason"])

    print("OK: cache-write-decision 判定、剩余秒数与稳定缓存键均符合预期")


if __name__ == "__main__":
    main()
