#!/usr/bin/env python3
"""/conditional-range-decision 调用示例（仅依赖 Python 3 标准库）。

在 /client-range-decision 的 Range/If-Range 决策之前，先评估客户端的
GET 前置条件（If-None-Match / If-Modified-Since），演示：
  1. If-None-Match 为 "*"：条件命中，304 且不生成 plan；
  2. If-None-Match 标签弱比较命中（客户端 W/"v3-strong" 对服务端强
     "v3-strong"）：304；
  3. If-None-Match 未命中：复用 /client-range-decision 的 206 决策，
     plan 逐块模拟回源后走 /assemble；
  4. 仅携带 If-Modified-Since：过去时刻 → 未修改 304；更早时刻 →
     206；
  5. If-None-Match 存在时 If-Modified-Since 被忽略（即使其值非法也
     不报 422）。

用法：
    python examples/conditional_range_decision_example.py

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
LAST_MODIFIED = "Wed, 21 Oct 2015 07:28:00 GMT"
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


def decide(
    range_header: str | None = "bytes=0-99",
    *,
    etag: str | None = ETAG,
    last_modified: str | None = None,
    if_none_match: str | None = None,
    if_modified_since: str | None = None,
) -> dict:
    body: dict = {
        "object_length": OBJECT_LENGTH,
        "max_gap": MAX_GAP,
        "fetch_limit": FETCH_LIMIT,
    }
    if etag is not None:
        body["etag"] = etag
    if last_modified is not None:
        body["last_modified"] = last_modified
    if range_header is not None:
        body["range_header"] = range_header
    if if_none_match is not None:
        body["if_none_match"] = if_none_match
    if if_modified_since is not None:
        body["if_modified_since"] = if_modified_since
    return call("/conditional-range-decision", body)


def show(title: str, d: dict) -> None:
    print(f"== {title} ==")
    print(f"决策状态码: {d['status_code']}（{d['reason']}）")
    for name, value in d["headers"].items():
        print(f"{name}: {value}")
    cond = d["condition"]
    print(
        "条件: present="
        f"{cond['condition_present']}, matched={cond['matched']}, "
        f"matched_by={cond['matched_by']}"
    )
    print()


def main() -> None:
    obj = bytes((i % 256 for i in range(OBJECT_LENGTH)))

    # 1. If-None-Match: * 始终命中（代理侧对象存在）→ 304，无 plan。
    d = decide(if_none_match="*")
    show('If-None-Match: "*"', d)
    assert d["status_code"] == 304 and d["decision"] is None
    assert d["headers"]["ETag"] == ETAG

    # 2. 客户端持弱标签、服务端为同值强标签：弱比较命中 → 304。
    d = decide(if_none_match='W/"v3-strong"')
    show('If-None-Match: W/"v3-strong"（弱比较）', d)
    assert d["status_code"] == 304

    # 3. 标签未命中：落入 /client-range-decision 的 206，plan 可回源重组。
    d = decide(range_header="bytes=0-99", if_none_match='"stale-version"')
    show('If-None-Match: "stale-version" 未命中', d)
    assert d["status_code"] == 206 and d["decision"]["plan"] is not None

    plan = d["decision"]["plan"]
    contents = [
        {
            "block_index": blk["index"],
            "data_b64": base64.b64encode(
                obj[blk["start"] : blk["end"] + 1]
            ).decode("ascii"),
        }
        for blk in plan["blocks"]
    ]
    assembled = call("/assemble", {
        "object_length": OBJECT_LENGTH,
        "blocks": plan["blocks"],
        "mappings": plan["mappings"],
        "contents": contents,
        "cached_blocks": plan.get("cached_blocks", []),
    })
    first = assembled["ranges"][0]
    assert base64.b64decode(first["data_b64"]) == obj[0:100]
    print(f"  206 plan 已提交 /assemble，重组第 0 段 {first['length']} 字节\n")

    # 4. 仅 If-Modified-Since：同一时刻/未来 → 304；过去 → 206。
    d = decide(
        etag=None,
        last_modified=LAST_MODIFIED,
        if_modified_since=LAST_MODIFIED,
    )
    show("If-Modified-Since 等于当前 Last-Modified", d)
    assert d["status_code"] == 304
    assert d["headers"]["Last-Modified"] == LAST_MODIFIED

    d = decide(
        range_header="bytes=0-99",
        etag=None,
        last_modified=LAST_MODIFIED,
        if_modified_since="Tue, 20 Oct 2015 07:28:00 GMT",
    )
    show("If-Modified-Since 早于当前 Last-Modified", d)
    assert d["status_code"] == 206

    # 5. If-None-Match 存在时 If-Modified-Since 被忽略（非法值也不报错）。
    d = decide(
        range_header="bytes=0-99",
        if_none_match='"stale-version"',
        if_modified_since="not-a-valid-date",
    )
    show("INM 未命中 + 非法 IMS 被忽略", d)
    assert d["status_code"] == 206
    assert d["condition"]["if_modified_since_evaluated"] is False

    print("全部示例断言通过。")


if __name__ == "__main__":
    main()
