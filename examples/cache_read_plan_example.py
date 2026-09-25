#!/usr/bin/env python3
"""/cache-read-plan 调用示例（仅依赖 Python 3 标准库）。

脚本完整演示一次典型的“写入 → 读取”缓存闭环：
  1. 调用 /plan 规划回源块；
  2. 模拟上游对每个 Range 请求返回 206 Partial Content（携带
     Content-Range、强 ETag、Cache-Control、Age、Vary 与响应体）；
  3. 用 /verify-fetch-batch 验收整批分块并确认强 ETag；
  4. 逐块调用 /cache-write-decision，把可缓存块的 cache_key、
     vary_headers、剩余秒数与写入时刻记录为候选缓存块元数据；
  5. 新请求到来时调用 /cache-read-plan：用当前 URI/ETag/请求头重算键，
     筛出可用块并直接得到可用于 /assemble 的 plan；
  6. 演示两种未采用场景：ETag 版本变化（key_mismatch）与块已过期
     （expired）；
  7. 用返回的 plan 调 /assemble 重组并逐字节核对。

用法：
    python examples/cache_read_plan_example.py

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
# 模拟“现在”：写入发生在 NOW-30，查询发生在 NOW。
STORED_AT = 1_758_699_970
NOW = STORED_AT + 30


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

    # 4. 逐块判定能否写入共享缓存，把可缓存块的元数据记录下来
    #    （stored_at 为写入时刻，remaining_seconds 由判定返回）。
    candidates = []
    for block in plan["blocks"]:
        wd = post_json(
            "/cache-write-decision",
            {
                "uri": OBJECT_URI,
                "start": block["start"],
                "end": block["end"],
                "etag": verified["etag"],
                "request_headers": CLIENT_HEADERS,
                "cache_control": UPSTREAM_CACHE_CONTROL,
                "age": UPSTREAM_AGE,
                "vary": UPSTREAM_VARY,
            },
        )
        assert wd["cacheable"], wd
        candidates.append(
            {
                "cache_id": f"cdn-{block['index']}",
                "start": block["start"],
                "end": block["end"],
                "cache_key": wd["cache_key"],
                "vary_headers": wd["vary_headers"],
                "stored_at": STORED_AT,
                "remaining_seconds": wd["remaining_seconds"],
            }
        )
        print(
            f"cached [{block['start']},{block['end']}] as cdn-{block['index']}, "
            f"remaining={wd['remaining_seconds']}s, key={wd['cache_key'][:16]}..."
        )

    # 5. 新请求到来：目标 [0,149] 与末尾 16 字节，候选块为上一步写入的
    #    元数据。当前请求头与写入时一致、ETag 未变 -> 全部可用；[0,99] 与
    #    [240,255] 命中缓存，[100,149] 回源。
    read_ranges = [
        {"type": "interval", "start": 0, "end": 149},
        {"type": "suffix", "last_n": 16},
    ]
    read = post_json(
        "/cache-read-plan",
        {
            "uri": OBJECT_URI,
            "object_length": OBJECT_LENGTH,
            "etag": ETAG,
            "request_headers": CLIENT_HEADERS,
            "now": NOW,
            "ranges": read_ranges,
            "max_gap": 8,
            "fetch_limit": 128,
            "candidates": candidates,
        },
    )
    for st in read["candidates"]:
        print(f"candidate {st['cache_id']}: adopted={st['adopted']} reason={st['reason']}")
    assert all(st["adopted"] for st in read["candidates"])
    plan = read["plan"]
    print(
        "fetch blocks:",
        [(b["start"], b["end"]) for b in plan["blocks"]],
        "cache blocks:",
        [(cb["cache_id"], cb["start"], cb["end"]) for cb in plan["cached_blocks"]],
    )
    assert [(b["start"], b["end"]) for b in plan["blocks"]] == [(100, 149)]

    # 6a. 版本变化：对象当前 ETag 变为 "demo-v2"，旧键重算不一致 ->
    #     所有候选块 key_mismatch，plan 退化为纯回源。
    read = post_json(
        "/cache-read-plan",
        {
            "uri": OBJECT_URI,
            "object_length": OBJECT_LENGTH,
            "etag": '"demo-v2"',
            "request_headers": CLIENT_HEADERS,
            "now": NOW,
            "ranges": read_ranges,
            "max_gap": 8,
            "fetch_limit": 128,
            "candidates": candidates,
        },
    )
    assert all(st["reason"] == "key_mismatch" for st in read["candidates"])
    assert read["plan"]["cached_blocks"] == []
    print("ETag 版本变化 -> 全部 key_mismatch，退化为纯回源")

    # 6b. 过期：查询时刻晚于 stored_at + remaining_seconds（相等也算过期）。
    read = post_json(
        "/cache-read-plan",
        {
            "uri": OBJECT_URI,
            "object_length": OBJECT_LENGTH,
            "etag": ETAG,
            "request_headers": CLIENT_HEADERS,
            "now": STORED_AT + 100,  # remaining_seconds=100，相等即过期
            "ranges": read_ranges,
            "max_gap": 8,
            "fetch_limit": 128,
            "candidates": candidates,
        },
    )
    assert all(st["reason"] == "expired" for st in read["candidates"])
    print("stored_at + remaining_seconds 不晚于 now -> 全部 expired")

    # 7. 用第 5 步返回的 plan 重组：缓存块内容来自本地缓存，回源块内容
    #    来自模拟上游，/assemble 逐字节核对。
    read = post_json(
        "/cache-read-plan",
        {
            "uri": OBJECT_URI,
            "object_length": OBJECT_LENGTH,
            "etag": ETAG,
            "request_headers": CLIENT_HEADERS,
            "now": NOW,
            "ranges": read_ranges,
            "max_gap": 8,
            "fetch_limit": 128,
            "candidates": candidates,
        },
    )
    plan = read["plan"]
    assembled = post_json(
        "/assemble",
        {
            "object_length": OBJECT_LENGTH,
            "blocks": plan["blocks"],
            "mappings": plan["mappings"],
            "contents": [
                {
                    "block_index": b["index"],
                    "data_b64": base64.b64encode(obj[b["start"] : b["end"] + 1]).decode(),
                }
                for b in plan["blocks"]
            ],
            "cached_blocks": plan["cached_blocks"],
            "cache_contents": [
                {
                    "cache_id": cb["cache_id"],
                    "data_b64": base64.b64encode(obj[cb["start"] : cb["end"] + 1]).decode(),
                }
                for cb in plan["cached_blocks"]
            ],
        },
    )
    got = [base64.b64decode(r["data_b64"]) for r in assembled["ranges"]]
    assert got == [obj[0:150], obj[240:256]], "重组结果必须与对象字节一致"
    print("assemble 重组 [0,149] 与末尾 16 字节逐字节一致")

    print("OK: cache-read-plan 筛选、排序与 plan 均符合预期")


if __name__ == "__main__":
    main()
