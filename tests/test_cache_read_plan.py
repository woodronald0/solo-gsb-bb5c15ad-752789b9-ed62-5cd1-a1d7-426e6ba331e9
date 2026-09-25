# -*- coding: utf-8 -*-
"""POST /cache-read-plan 的行为测试。

覆盖：
- 采用：可用块进入 plan 并被目标范围引用；全部命中缓存时 blocks 为空；
  无候选块时退化为纯回源规划；suffix 范围照常规范化；
- 键匹配：按 /cache-write-decision 的键算法重算——ETag 版本、URI、区间、
  Vary 取值任一不符即 key_mismatch；字段名大小写、Vary 成员大小写与顺序、
  缺失 Vary 头（null 成分）与写入侧行为一致；
- 新鲜度：stored_at + remaining_seconds 严格大于 now 才可用（相等即过期）；
- 排序：可用块按到期时间降序、输入次序升序交给 /plan，重叠字节优先使用
  更耐久的块；到期时间相同按输入次序；
- 未采用原因：key_mismatch / expired / not_referenced 逐块给出；
- 端到端：返回的 plan 直接提交 /assemble 重组出正确字节；
- 422：重复 cache_id、非法区间（end < start、越界）、非法 cache_key、
  非法 vary_headers、非法时间字段（stored_at/remaining_seconds/now）、
  弱 ETag、空白 URI、非法请求头；错误结构带可定位 loc。

直接运行：python -m unittest discover -s tests
"""

from __future__ import annotations

import base64
import hashlib
import json
import unittest

from app.main import app

try:
    from starlette.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None  # type: ignore[assignment]

URI = "https://objects.example.com/bucket/file.bin"
ETAG = '"v3-strong"'
NOW = 1_758_700_000
OBJ = bytes(i % 256 for i in range(256))


def make_key(
    start: int,
    end: int,
    *,
    uri: str = URI,
    etag: str = ETAG,
    vary: list[list[object]] | None = None,
) -> str:
    """按 README 文档承诺的规范化构造计算缓存键（与写入侧逐字节一致）。"""
    material = {
        "uri": uri,
        "start": start,
        "end": end,
        "etag": etag,
        "vary": vary if vary is not None else [],
    }
    return hashlib.sha256(
        json.dumps(
            material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()


@unittest.skipIf(TestClient is None, "需要安装 starlette（随 fastapi 安装）")
class CacheReadPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    # ----------------------------- 构造工具 -----------------------------

    def cand(
        self,
        cache_id,
        start: int,
        end: int,
        *,
        cache_key: str | None = None,
        vary_headers: list[str] | None = None,
        stored_at: int = NOW - 100,
        remaining_seconds: int = 200,
        key_over: dict | None = None,
    ) -> dict:
        """构造候选块；cache_key 缺省时按给定成分用文档算法现算。"""
        if cache_key is None:
            over = {"start": start, "end": end}
            if key_over:
                over.update(key_over)
            vary = over.pop("vary", None)
            cache_key = make_key(vary=vary, **over)
        return {
            "cache_id": cache_id,
            "start": start,
            "end": end,
            "cache_key": cache_key,
            "vary_headers": vary_headers if vary_headers is not None else [],
            "stored_at": stored_at,
            "remaining_seconds": remaining_seconds,
        }

    def req(self, **over) -> dict:
        body = {
            "uri": URI,
            "object_length": 256,
            "etag": ETAG,
            "request_headers": {},
            "now": NOW,
            "ranges": [{"type": "interval", "start": 0, "end": 149}],
            "max_gap": 8,
            "fetch_limit": 128,
            "candidates": [],
        }
        body.update(over)
        return body

    def post(self, body: dict):
        return self.client.post("/cache-read-plan", json=body)

    def ok(self, resp) -> dict:
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def status_of(self, d: dict, index: int) -> dict:
        for st in d["candidates"]:
            if st["index"] == index:
                return st
        self.fail(f"响应缺少 candidates[{index}] 的状态：{d['candidates']}")

    def err(self, resp, code: str, loc: str | None = None) -> dict:
        self.assertEqual(resp.status_code, 422, resp.text)
        payload = resp.json()
        self.assertEqual(payload["error"], code, payload)
        detail = payload["detail"][0]
        self.assertIn("message", detail)
        if loc is not None:
            self.assertEqual(detail["loc"], loc, payload)
        return payload

    def err_invalid_request(self, resp) -> dict:
        self.assertEqual(resp.status_code, 422, resp.text)
        payload = resp.json()
        self.assertEqual(payload["error"], "invalid_request", payload)
        self.assertIsInstance(payload["detail"], list)
        self.assertTrue(payload["detail"])
        return payload

    # ----------------------------- 采用路径 -----------------------------

    def test_no_candidates_pure_fetch(self) -> None:
        d = self.ok(self.post(self.req()))
        self.assertEqual(d["candidates"], [])
        self.assertEqual(d["plan"]["cached_blocks"], [])
        self.assertEqual(
            [(b["start"], b["end"]) for b in d["plan"]["blocks"]],
            [(0, 127), (128, 149)],
        )
        for m in d["plan"]["mappings"]:
            for sl in m["slices"]:
                self.assertEqual(sl["source"], "fetch")

    def test_usable_block_adopted(self) -> None:
        body = self.req(
            candidates=[self.cand("cdn-a", 0, 99)],
        )
        d = self.ok(self.post(body))
        st = self.status_of(d, 0)
        self.assertTrue(st["adopted"])
        self.assertEqual(st["reason"], "adopted")
        self.assertEqual(st["cache_id"], "cdn-a")
        self.assertEqual(st["expires_at"], NOW - 100 + 200)
        # 缓存覆盖 [0,99]，剩余 [100,149] 回源。
        self.assertEqual(
            [(b["start"], b["end"]) for b in d["plan"]["blocks"]], [(100, 149)]
        )
        self.assertEqual(
            [(cb["cache_id"], cb["start"], cb["end"]) for cb in d["plan"]["cached_blocks"]],
            [("cdn-a", 0, 99)],
        )
        slices = d["plan"]["mappings"][0]["slices"]
        self.assertEqual(
            [(s["source"], s["cache_id"], s["length"]) for s in slices],
            [("cache", "cdn-a", 100), ("fetch", None, 50)],
        )

    def test_all_ranges_hit_cache_empty_blocks(self) -> None:
        body = self.req(
            candidates=[self.cand("cdn-a", 0, 200)],
        )
        d = self.ok(self.post(body))
        self.assertEqual(d["plan"]["blocks"], [])
        self.assertTrue(self.status_of(d, 0)["adopted"])

    def test_suffix_range_normalized(self) -> None:
        body = self.req(
            ranges=[{"type": "suffix", "last_n": 16}],
            candidates=[self.cand("cdn-a", 240, 255)],
        )
        d = self.ok(self.post(body))
        m = d["plan"]["mappings"][0]
        self.assertEqual((m["start"], m["end"]), (240, 255))
        self.assertEqual(d["plan"]["blocks"], [])
        self.assertTrue(self.status_of(d, 0)["adopted"])

    def test_integer_cache_id(self) -> None:
        body = self.req(candidates=[self.cand(7, 0, 99)])
        d = self.ok(self.post(body))
        self.assertTrue(self.status_of(d, 0)["adopted"])
        self.assertEqual(d["plan"]["cached_blocks"][0]["cache_id"], 7)

    # ----------------------------- 新鲜度 -----------------------------

    def test_expiry_is_strictly_later_than_now(self) -> None:
        # stored_at + remaining_seconds == now：相等即过期。
        expired = self.cand("cdn-a", 0, 99, stored_at=NOW - 50, remaining_seconds=50)
        d = self.ok(self.post(self.req(candidates=[expired])))
        st = self.status_of(d, 0)
        self.assertFalse(st["adopted"])
        self.assertEqual(st["reason"], "expired")
        self.assertEqual(st["expires_at"], NOW)
        self.assertEqual(d["plan"]["cached_blocks"], [])
        # 晚 1 秒即可用。
        fresh = self.cand("cdn-a", 0, 99, stored_at=NOW - 50, remaining_seconds=51)
        d = self.ok(self.post(self.req(candidates=[fresh])))
        self.assertTrue(self.status_of(d, 0)["adopted"])

    def test_expired_and_mismatch_coexist(self) -> None:
        cands = [
            self.cand("cdn-a", 0, 99, stored_at=NOW - 300, remaining_seconds=100),
            self.cand("cdn-b", 100, 149, key_over={"etag": '"v2-old"'}),
            self.cand("cdn-c", 0, 149),
        ]
        d = self.ok(self.post(self.req(candidates=cands)))
        self.assertEqual(self.status_of(d, 0)["reason"], "expired")
        self.assertEqual(self.status_of(d, 1)["reason"], "key_mismatch")
        self.assertEqual(self.status_of(d, 2)["reason"], "adopted")
        # 只有 cdn-c 进入 plan。
        self.assertEqual(
            [cb["cache_id"] for cb in d["plan"]["cached_blocks"]], ["cdn-c"]
        )

    # ----------------------------- 键匹配 -----------------------------

    def test_key_mismatch_when_etag_changed(self) -> None:
        # 写入时的旧版本键在当前 ETag 下重算不一致：版本不一致即键不匹配。
        body = self.req(candidates=[self.cand("cdn-a", 0, 99, key_over={"etag": '"v2-old"'})])
        d = self.ok(self.post(body))
        self.assertEqual(self.status_of(d, 0)["reason"], "key_mismatch")
        self.assertEqual(d["plan"]["cached_blocks"], [])

    def test_key_mismatch_when_uri_changed(self) -> None:
        body = self.req(
            candidates=[
                self.cand("cdn-a", 0, 99, key_over={"uri": "https://objects.example.com/bucket/other.bin"})
            ]
        )
        d = self.ok(self.post(body))
        self.assertEqual(self.status_of(d, 0)["reason"], "key_mismatch")

    def test_key_mismatch_when_interval_differs(self) -> None:
        # 键按候选块声明的区间重算；写入时区间不同则键不同。
        body = self.req(candidates=[self.cand("cdn-a", 0, 99, key_over={"end": 100})])
        d = self.ok(self.post(body))
        self.assertEqual(self.status_of(d, 0)["reason"], "key_mismatch")

    def test_vary_value_must_match_current_request(self) -> None:
        headers = {"Accept-Encoding": "gzip"}
        vary_headers = ["accept-encoding"]
        good = self.cand(
            "cdn-a", 0, 99,
            vary_headers=vary_headers,
            key_over={"vary": [["accept-encoding", "gzip"]]},
        )
        d = self.ok(self.post(self.req(request_headers=headers, candidates=[good])))
        self.assertEqual(self.status_of(d, 0)["reason"], "adopted")
        # 当前请求的 Vary 取值与写入时不同 -> 键不匹配（不同变体不串缓存）。
        bad = self.cand(
            "cdn-b", 0, 99,
            vary_headers=vary_headers,
            key_over={"vary": [["accept-encoding", "br"]]},
        )
        d = self.ok(self.post(self.req(request_headers=headers, candidates=[bad])))
        self.assertEqual(self.status_of(d, 0)["reason"], "key_mismatch")

    def test_vary_headers_case_and_order_normalized(self) -> None:
        # 候选块 vary_headers 的大小写与顺序不影响重算（与写入侧规范化一致）。
        headers = {"Accept": "text/html", "Accept-Encoding": "gzip"}
        key = make_key(0, 99, vary=[["accept", "text/html"], ["accept-encoding", "gzip"]])
        cand = self.cand(
            "cdn-a", 0, 99,
            cache_key=key,
            vary_headers=["Accept-Encoding", "accept", "ACCEPT-ENCODING"],
        )
        d = self.ok(self.post(self.req(request_headers=headers, candidates=[cand])))
        self.assertEqual(self.status_of(d, 0)["reason"], "adopted")

    def test_request_header_name_case_insensitive(self) -> None:
        cand = self.cand(
            "cdn-a", 0, 99,
            vary_headers=["accept-encoding"],
            key_over={"vary": [["accept-encoding", "gzip"]]},
        )
        for headers in ({"Accept-Encoding": "gzip"}, {"ACCEPT-ENCODING": "gzip"}):
            d = self.ok(self.post(self.req(request_headers=headers, candidates=[cand])))
            self.assertEqual(self.status_of(d, 0)["reason"], "adopted", headers)

    def test_missing_vary_header_matches_null_component(self) -> None:
        # 写入时请求缺失 Vary 头（null 成分），当前请求同样缺失 -> 键匹配。
        cand = self.cand(
            "cdn-a", 0, 99,
            vary_headers=["accept-encoding"],
            key_over={"vary": [["accept-encoding", None]]},
        )
        d = self.ok(self.post(self.req(request_headers={}, candidates=[cand])))
        self.assertEqual(self.status_of(d, 0)["reason"], "adopted")
        # 当前请求携带该头 -> 与 null 成分不符，键不匹配。
        d = self.ok(
            self.post(
                self.req(
                    request_headers={"Accept-Encoding": "gzip"}, candidates=[cand]
                )
            )
        )
        self.assertEqual(self.status_of(d, 0)["reason"], "key_mismatch")

    # ------------------------- 排序与重叠优先 -------------------------

    def test_overlap_prefers_longer_lived_block(self) -> None:
        # cdn-a 先输入但更早到期；重叠部分应归更耐久的 cdn-b。
        cands = [
            self.cand("cdn-a", 0, 99, stored_at=NOW - 50, remaining_seconds=100),
            self.cand("cdn-b", 50, 149, stored_at=NOW - 400, remaining_seconds=800),
        ]
        d = self.ok(self.post(self.req(candidates=cands)))
        self.assertTrue(self.status_of(d, 0)["adopted"])
        self.assertTrue(self.status_of(d, 1)["adopted"])
        slices = d["plan"]["mappings"][0]["slices"]
        self.assertEqual(
            [
                (s["source"], s["cache_id"], s["block_start"], s["block_end"],
                 s["offset_in_block"], s["length"])
                for s in slices
            ],
            [
                ("cache", "cdn-a", 0, 99, 0, 50),    # [0,49] 归 cdn-a
                ("cache", "cdn-b", 50, 149, 0, 100),  # [50,149] 归更耐久的 cdn-b
            ],
        )
        # plan.cached_blocks 按交给 /plan 的顺序回显：更耐久的在前。
        self.assertEqual(
            [cb["cache_id"] for cb in d["plan"]["cached_blocks"]],
            ["cdn-b", "cdn-a"],
        )

    def test_overlap_same_expiry_uses_input_order(self) -> None:
        cands = [
            self.cand("cdn-a", 0, 99),
            self.cand("cdn-b", 50, 149),
        ]
        d = self.ok(self.post(self.req(candidates=cands)))
        slices = d["plan"]["mappings"][0]["slices"]
        self.assertEqual(
            [(s["cache_id"], s["length"]) for s in slices],
            [("cdn-a", 100), ("cdn-b", 50)],
        )

    def test_not_referenced_when_no_range_overlap(self) -> None:
        # 可用但不与任何目标范围相交：不进入 plan，原因 not_referenced。
        cands = [
            self.cand("cdn-a", 0, 99),
            self.cand("cdn-b", 200, 255),
        ]
        d = self.ok(self.post(self.req(candidates=cands)))
        self.assertEqual(self.status_of(d, 0)["reason"], "adopted")
        st = self.status_of(d, 1)
        self.assertFalse(st["adopted"])
        self.assertEqual(st["reason"], "not_referenced")
        self.assertEqual(
            [cb["cache_id"] for cb in d["plan"]["cached_blocks"]], ["cdn-a"]
        )

    # --------------------------- 端到端重组 ---------------------------

    def test_plan_usable_for_assemble(self) -> None:
        body = self.req(candidates=[self.cand("cdn-a", 0, 99)])
        d = self.ok(self.post(body))
        plan = d["plan"]
        contents = [
            {
                "block_index": b["index"],
                "data_b64": base64.b64encode(OBJ[b["start"] : b["end"] + 1]).decode(),
            }
            for b in plan["blocks"]
        ]
        resp = self.client.post(
            "/assemble",
            json={
                "object_length": 256,
                "blocks": plan["blocks"],
                "mappings": plan["mappings"],
                "contents": contents,
                "cached_blocks": plan["cached_blocks"],
                "cache_contents": [
                    {"cache_id": "cdn-a", "data_b64": base64.b64encode(OBJ[0:100]).decode()}
                ],
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        assembled = resp.json()["ranges"][0]
        self.assertEqual(assembled["length"], 150)
        self.assertEqual(
            base64.b64decode(assembled["data_b64"]), OBJ[0:150]
        )

    # ----------------------------- 422：候选块 -----------------------------

    def test_duplicate_cache_id_422(self) -> None:
        cands = [self.cand("cdn-a", 0, 99), self.cand("cdn-a", 100, 149)]
        payload = self.err(
            self.post(self.req(candidates=cands)),
            "duplicate_cache_id",
            "candidates[1].cache_id",
        )
        self.assertEqual(payload["detail"][0]["cache_id"], "cdn-a")

    def test_invalid_interval_422(self) -> None:
        self.err(
            self.post(self.req(candidates=[self.cand("cdn-a", 100, 99)])),
            "invalid_interval",
            "candidates[0]",
        )

    def test_interval_out_of_bounds_422(self) -> None:
        self.err(
            self.post(self.req(candidates=[self.cand("cdn-a", 200, 256, key_over={"end": 256})])),
            "interval_out_of_bounds",
            "candidates[0]",
        )
        self.err(
            self.post(self.req(candidates=[self.cand("cdn-a", 0, 256, key_over={"end": 256})])),
            "interval_out_of_bounds",
            "candidates[0]",
        )

    def test_invalid_cache_key_422(self) -> None:
        for bad in ("", "xyz", "0" * 63, "A" * 64, "g" * 64):
            with self.subTest(cache_key=bad):
                self.err(
                    self.post(self.req(candidates=[self.cand("cdn-a", 0, 99, cache_key=bad)])),
                    "invalid_cache_key",
                    "candidates[0].cache_key",
                )
        # 超长键由字段级约束拦截（loc 同样可定位到 candidates[0].cache_key）。
        payload = self.err_invalid_request(
            self.post(self.req(candidates=[self.cand("cdn-a", 0, 99, cache_key="0" * 65)]))
        )
        self.assertEqual(
            payload["detail"][0]["loc"], ["body", "candidates", 0, "cache_key"]
        )

    def test_invalid_vary_headers_422(self) -> None:
        for bad in (["Accept Language"], ["*"], ["accept\rencoding"], [""], ["中文"]):
            with self.subTest(vary_headers=bad):
                self.err(
                    self.post(
                        self.req(candidates=[self.cand("cdn-a", 0, 99, vary_headers=bad)])
                    ),
                    "invalid_vary_headers",
                    "candidates[0].vary_headers[0]",
                )

    def test_time_fields_422(self) -> None:
        # 负的 stored_at / now、remaining_seconds < 1 均为字段级 422。
        self.err_invalid_request(
            self.post(self.req(candidates=[self.cand("cdn-a", 0, 99, stored_at=-1)]))
        )
        self.err_invalid_request(
            self.post(
                self.req(candidates=[self.cand("cdn-a", 0, 99, remaining_seconds=0)])
            )
        )
        self.err_invalid_request(self.post(self.req(now=-1)))
        self.err_invalid_request(self.post(self.req(now="now")))

    # ------------------------ 422：请求级头值 ------------------------

    def test_weak_etag_422(self) -> None:
        self.err(self.post(self.req(etag='W/"v3-strong"')), "weak_etag", "etag")

    def test_invalid_etag_422(self) -> None:
        self.err(self.post(self.req(etag="v3-strong")), "invalid_etag", "etag")

    def test_blank_uri_422(self) -> None:
        self.err(self.post(self.req(uri="   ")), "invalid_uri", "uri")

    def test_request_header_invalid_422(self) -> None:
        self.err(
            self.post(self.req(request_headers={"Accept Language": "x"})),
            "invalid_request_header",
            "request_headers.Accept Language",
        )
        self.err(
            self.post(self.req(request_headers={"Accept": "a\rb"})),
            "malformed_header",
            "request_headers.Accept",
        )

    # ------------------------ 422：字段级校验 ------------------------

    def test_missing_required_fields_422(self) -> None:
        self.err_invalid_request(self.post({}))
        self.err_invalid_request(self.post({"uri": URI}))

    def test_wrong_types_422(self) -> None:
        self.err_invalid_request(self.post(self.req(object_length=-1)))
        self.err_invalid_request(self.post(self.req(fetch_limit=0)))
        self.err_invalid_request(self.post(self.req(max_gap=-1)))
        self.err_invalid_request(self.post(self.req(ranges=[])))
        self.err_invalid_request(
            self.post(self.req(candidates=[self.cand(True, 0, 99)]))
        )
        self.err_invalid_request(
            self.post(self.req(candidates=[self.cand("", 0, 99)]))
        )

    def test_range_out_of_bounds_422(self) -> None:
        # 目标范围越界沿用 /plan 的 unsatisfiable 结构。
        resp = self.post(
            self.req(ranges=[{"type": "interval", "start": 0, "end": 256}])
        )
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertEqual(resp.json()["error"], "unsatisfiable")

    # --------------------- 与 /cache-write-decision 联动 ---------------------

    def test_roundtrip_with_cache_write_decision(self) -> None:
        # 写入侧判定得到的 cache_key/vary_headers/remaining_seconds 可直接
        # 作为候选块元数据被读取侧采用。
        headers = {"Accept-Encoding": "gzip"}
        write = self.client.post(
            "/cache-write-decision",
            json={
                "uri": URI,
                "start": 0,
                "end": 99,
                "etag": ETAG,
                "request_headers": headers,
                "cache_control": "s-maxage=120, max-age=60, public",
                "age": "20",
                "vary": "Accept-Encoding",
            },
        )
        self.assertEqual(write.status_code, 200, write.text)
        wd = write.json()
        self.assertTrue(wd["cacheable"])

        cand = {
            "cache_id": "cdn-a",
            "start": 0,
            "end": 99,
            "cache_key": wd["cache_key"],
            "vary_headers": wd["vary_headers"],
            "stored_at": NOW - 30,
            "remaining_seconds": wd["remaining_seconds"],
        }
        d = self.ok(self.post(self.req(request_headers=headers, candidates=[cand])))
        st = self.status_of(d, 0)
        self.assertTrue(st["adopted"])
        self.assertEqual(st["expires_at"], NOW - 30 + 100)


if __name__ == "__main__":
    unittest.main()
