# -*- coding: utf-8 -*-
"""POST /client-range-decision 的行为测试。

覆盖：
- 200 决策：无 Range（range_absent）；If-Range 不匹配（ETag 不同、
  弱 ETag、Last-Modified 晚于当前值）时忽略 Range 完整对象 200；
- 206 决策：单段闭区间（含 Content-Range/Content-Length）、开放终点
  bytes=N-、suffix bytes=-N、多段 multipart/byteranges；越界闭区间/
  开放终点收敛到对象尾端；suffix 在小对象上回落到整个对象；大范围
  超过 fetch_limit 时复用缓存感知规划拆块；cached_blocks 透传；
- 跳过不可满足段：起点 >= 总长的闭区间/开放终点被跳过但其余段仍
  206；全部不可满足（含空对象 suffix）给出 416 与
  Content-Range: bytes */总长；
- plan 与 /assemble 的衔接：206 的 plan 字段可直接回交 /assemble
  重组出与范围一致的字节；
- If-Range：强 ETag 全等匹配、弱标签即使取值相同也不强匹配；
  IMF-fixdate 同日/未来匹配、过去不匹配；kind 识别；
- 422 非法头值：Range 单位错误、反转区间、-0、空表达式、内部空白、
  控制字符、空字符串；If-Range 非法 ETag/日期（星期与日期不一致）；
  当前 ETag 弱标签/非法；Last-Modified 非法；etag 与 last_modified
  全缺；字段类型错误；错误 loc 可定位到 range_header /
  range_header.ranges[i] / if_range / etag / last_modified。

直接运行：python -m unittest discover -s tests
"""

from __future__ import annotations

import base64
import unittest

from app.main import app

try:
    from starlette.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None  # type: ignore[assignment]

OBJECT_LENGTH = 256
STRONG_ETAG = '"v3-strong"'
LAST_MODIFIED = "Wed, 21 Oct 2015 07:28:00 GMT"


@unittest.skipIf(TestClient is None, "需要安装 starlette（随 fastapi 安装）")
class ClientRangeDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.obj = bytes((i % 256 for i in range(OBJECT_LENGTH)))

    # ----------------------------- 构造工具 -----------------------------

    def post(self, body: dict):
        return self.client.post("/client-range-decision", json=body)

    def req(self, range_header, **over) -> dict:
        body = {
            "object_length": OBJECT_LENGTH,
            "etag": STRONG_ETAG,
            "range_header": range_header,
            "max_gap": 8,
            "fetch_limit": 128,
        }
        body.update(over)
        return body

    def err422(self, resp, *, loc: str | None = None, code: str | None = None):
        self.assertEqual(resp.status_code, 422, resp.text)
        payload = resp.json()
        detail = payload["detail"][0]
        self.assertIn("message", detail)
        if loc is not None:
            self.assertEqual(detail["loc"], loc, payload)
        if code is not None:
            self.assertEqual(payload["error"], code, payload)
        return payload

    # ----------------------------- 200：无 Range -----------------------------

    def test_no_range_header_full_object_200(self) -> None:
        d = self.post(self.req(None)).json()
        self.assertEqual(d["status_code"], 200)
        self.assertEqual(d["reason"], "range_absent")
        self.assertEqual(d["headers"], {"Content-Length": str(OBJECT_LENGTH)})
        self.assertEqual(d["content_length"], OBJECT_LENGTH)
        self.assertIsNone(d["media_type"])
        self.assertIsNone(d["plan"])
        self.assertEqual(d["segments"], [])
        self.assertTrue(d["if_range_matched"])

    def test_range_key_omitted_equals_null(self) -> None:
        body = {
            "object_length": OBJECT_LENGTH,
            "etag": STRONG_ETAG,
            "max_gap": 8,
            "fetch_limit": 128,
        }
        d = self.post(body).json()
        self.assertEqual(d["status_code"], 200)
        self.assertEqual(d["reason"], "range_absent")

    def test_blank_range_header_is_422_not_absent(self) -> None:
        for raw in ("", "   ", "\t"):
            with self.subTest(raw=raw):
                resp = self.post(self.req(raw))
                self.err422(resp, loc="range_header", code="invalid_range_header")

    # ----------------------------- 206：单段 -----------------------------

    def test_single_closed_interval_206(self) -> None:
        d = self.post(self.req("bytes=10-19")).json()
        self.assertEqual(d["status_code"], 206)
        self.assertEqual(d["reason"], "partial")
        self.assertEqual(d["media_type"], "single")
        self.assertEqual(
            d["headers"],
            {
                "Content-Length": "10",
                "Content-Range": "bytes 10-19/256",
            },
        )
        self.assertEqual(d["content_length"], 10)
        self.assertEqual(d["satisfied_count"], 1)
        self.assertEqual(d["skipped_count"], 0)
        seg = d["segments"][0]
        self.assertEqual(seg["kind"], "closed")
        self.assertEqual((seg["start"], seg["end"]), (10, 19))
        self.assertEqual(seg["plan_range_index"], 0)
        self.assertEqual(seg["reason"], "satisfied")

    def test_open_ended_range_206(self) -> None:
        d = self.post(self.req("bytes=240-")).json()
        self.assertEqual(d["status_code"], 206)
        self.assertEqual(d["segments"][0]["kind"], "open")
        self.assertEqual((d["segments"][0]["start"], d["segments"][0]["end"]), (240, 255))
        self.assertEqual(d["headers"]["Content-Range"], "bytes 240-255/256")
        self.assertEqual(d["content_length"], 16)

    def test_suffix_range_206(self) -> None:
        d = self.post(self.req("bytes=-16")).json()
        self.assertEqual(d["status_code"], 206)
        seg = d["segments"][0]
        self.assertEqual(seg["kind"], "suffix")
        self.assertEqual(seg["suffix_length"], 16)
        self.assertEqual((seg["start"], seg["end"]), (240, 255))
        self.assertEqual(d["headers"]["Content-Range"], "bytes 240-255/256")

    def test_suffix_larger_than_object_covers_all(self) -> None:
        d = self.post(self.req("bytes=-1000")).json()
        self.assertEqual(d["status_code"], 206)
        seg = d["segments"][0]
        self.assertEqual((seg["start"], seg["end"]), (0, 255))
        self.assertEqual(d["headers"]["Content-Range"], "bytes 0-255/256")
        self.assertEqual(d["content_length"], 256)

    def test_closed_range_clamped_to_object_end(self) -> None:
        # 末字节位置超过对象尾端：按 HTTP 语义收敛，仍可满足。
        d = self.post(self.req("bytes=250-9999")).json()
        self.assertEqual(d["status_code"], 206)
        seg = d["segments"][0]
        self.assertEqual((seg["requested_start"], seg["requested_end"]), (250, 9999))
        self.assertEqual((seg["start"], seg["end"]), (250, 255))
        self.assertEqual(d["headers"]["Content-Range"], "bytes 250-255/256")

    def test_bytes_unit_case_insensitive_and_ows_allowed(self) -> None:
        # RFC 允许 '=' 之后与成员周围的 OWS；'bytes' 与 '=' 之间不得有空白。
        for raw in ("BYTES=0-9", "bytes= 0-9", "  bytes=0-9  ",
                    "bytes=0-9 , 100-109", "bytes=0-9,,100-109",
                    "bytes= 0-9 , 100-109 "):
            with self.subTest(raw=raw):
                resp = self.post(self.req(raw))
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(resp.json()["status_code"], 206, raw)

    def test_range_full_object_via_open_ended(self) -> None:
        d = self.post(self.req("bytes=0-")).json()
        self.assertEqual(d["status_code"], 206)
        self.assertEqual(d["headers"]["Content-Range"], "bytes 0-255/256")
        self.assertEqual(d["content_length"], 256)

    # ----------------------------- 206：多段 -----------------------------

    def test_multiple_ranges_206_multipart(self) -> None:
        d = self.post(self.req("bytes=0-9, 240-255, -16")).json()
        self.assertEqual(d["status_code"], 206)
        self.assertEqual(d["media_type"], "multipart/byteranges")
        self.assertEqual(d["satisfied_count"], 3)
        self.assertEqual(d["skipped_count"], 0)
        # 多段不臆造 Content-Length（取决于后续 multipart 分帧）。
        self.assertEqual(d["headers"], {})
        self.assertEqual(d["content_length"], 10 + 16 + 16)
        self.assertEqual([s["plan_range_index"] for s in d["segments"]], [0, 1, 2])
        self.assertEqual(len(d["plan"]["mappings"]), 3)
        # 后两段区间相同，规划块可以合并复用。
        self.assertTrue(len(d["plan"]["blocks"]) <= 3)

    def test_mixed_kind_segment_fields(self) -> None:
        d = self.post(self.req("bytes=0-9, 100-, -8, 50-60")).json()
        kinds = [(s["kind"], s["requested_start"], s["requested_end"],
                  s["suffix_length"]) for s in d["segments"]]
        self.assertEqual(
            kinds,
            [("closed", 0, 9, None), ("open", 100, None, None),
             ("suffix", None, None, 8), ("closed", 50, 60, None)],
        )

    def test_large_range_split_by_fetch_limit(self) -> None:
        # 单段闭区间 1000 字节、fetch_limit=300：缓存感知规划拆 4 块。
        d = self.post(self.req("bytes=0-999",
                               object_length=1000, fetch_limit=300)).json()
        self.assertEqual(d["status_code"], 206)
        self.assertEqual(
            [b["range_header"] for b in d["plan"]["blocks"]],
            ["bytes=0-299", "bytes=300-599", "bytes=600-899", "bytes=900-999"],
        )
        # mapping 用 4 个连续 fetch 切片覆盖整段。
        slices = d["plan"]["mappings"][0]["slices"]
        self.assertEqual([s["block_index"] for s in slices], [0, 1, 2, 3])

    def test_cached_blocks_passthrough_to_plan(self) -> None:
        body = self.req("bytes=0-99", cached_blocks=[
            {"cache_id": "cdn-a", "start": 40, "end": 59}
        ])
        d = self.post(body).json()
        self.assertEqual(d["status_code"], 206)
        # 回源块避开缓存区间；缓存块原样出现在 plan 中。
        self.assertEqual(
            [(b["start"], b["end"]) for b in d["plan"]["blocks"]],
            [(0, 39), (60, 99)],
        )
        self.assertEqual(d["plan"]["cached_blocks"], [
            {"cache_id": "cdn-a", "start": 40, "end": 59}
        ])

    # --------------------- 206 plan 与 /assemble 衔接 ---------------------

    def test_plan_can_drive_assemble_roundtrip(self) -> None:
        d = self.post(self.req("bytes=0-99, 240-255")).json()
        self.assertEqual(d["status_code"], 206)
        plan = d["plan"]
        contents = []
        for blk in plan["blocks"]:
            s, e = blk["start"], blk["end"]
            contents.append({
                "block_index": blk["index"],
                "data_b64": base64.b64encode(self.obj[s : e + 1]).decode("ascii"),
            })
        asm = self.client.post("/assemble", json={
            "object_length": OBJECT_LENGTH,
            "blocks": plan["blocks"],
            "mappings": plan["mappings"],
            "contents": contents,
            "cached_blocks": plan["cached_blocks"],
        })
        self.assertEqual(asm.status_code, 200, asm.text)
        ranges = asm.json()["ranges"]
        self.assertEqual(base64.b64decode(ranges[0]["data_b64"]), self.obj[0:100])
        self.assertEqual(base64.b64decode(ranges[1]["data_b64"]), self.obj[240:256])

    def test_skipped_segment_plan_contains_only_satisfied(self) -> None:
        d = self.post(self.req("bytes=0-9, 1000-2000, 100-109")).json()
        self.assertEqual(d["status_code"], 206)
        self.assertEqual(d["satisfied_count"], 2)
        self.assertEqual(d["skipped_count"], 1)
        skipped = d["segments"][1]
        self.assertFalse(skipped["satisfiable"])
        self.assertIsNone(skipped["plan_range_index"])
        self.assertEqual(skipped["reason"], "skipped_start_past_end")
        # plan 只有两段；其余段的 plan_range_index 仍按可满足段编号。
        self.assertEqual(
            [s["plan_range_index"] for s in d["segments"] if s["satisfiable"]],
            [0, 1],
        )
        self.assertEqual(len(d["plan"]["mappings"]), 2)

    # ----------------------------- 416 -----------------------------

    def test_all_unsatisfiable_closed_416(self) -> None:
        d = self.post(self.req("bytes=300-400, 256-257")).json()
        self.assertEqual(d["status_code"], 416)
        self.assertEqual(d["reason"], "unsatisfiable")
        self.assertEqual(d["headers"], {"Content-Range": "bytes */256"})
        self.assertIsNone(d["plan"])
        self.assertIsNone(d["content_length"])
        self.assertEqual(d["satisfied_count"], 0)
        self.assertEqual(d["skipped_count"], 2)
        for seg in d["segments"]:
            self.assertEqual(seg["reason"], "skipped_start_past_end")

    def test_open_ended_start_at_length_416(self) -> None:
        d = self.post(self.req("bytes=256-")).json()
        self.assertEqual(d["status_code"], 416)
        self.assertEqual(d["headers"], {"Content-Range": "bytes */256"})

    def test_suffix_on_empty_object_416(self) -> None:
        d = self.post(self.req("bytes=-10", object_length=0)).json()
        self.assertEqual(d["status_code"], 416)
        self.assertEqual(d["headers"], {"Content-Range": "bytes */0"})
        self.assertEqual(d["segments"][0]["reason"],
                         "skipped_empty_object_suffix")

    # ----------------------------- If-Range -----------------------------

    def test_if_range_strong_etag_match_206(self) -> None:
        d = self.post(self.req("bytes=0-9", if_range=STRONG_ETAG)).json()
        self.assertEqual(d["status_code"], 206)
        self.assertTrue(d["if_range_matched"])
        self.assertEqual(d["if_range_kind"], "etag")

    def test_if_range_etag_mismatch_ignores_range(self) -> None:
        d = self.post(self.req("bytes=0-9", if_range='"old-version"')).json()
        self.assertEqual(d["status_code"], 200)
        self.assertEqual(d["reason"], "if_range_mismatch")
        self.assertFalse(d["if_range_matched"])
        self.assertEqual(d["segments"], [])
        self.assertIsNone(d["plan"])
        self.assertEqual(d["content_length"], OBJECT_LENGTH)

    def test_if_range_weak_etag_never_matches(self) -> None:
        d = self.post(self.req("bytes=0-9", if_range='W/"v3-strong"')).json()
        self.assertEqual(d["status_code"], 200)
        self.assertFalse(d["if_range_matched"])
        self.assertEqual(d["reason"], "if_range_mismatch")

    def test_if_range_lowercase_w_prefix_also_weak(self) -> None:
        d = self.post(self.req("bytes=0-9", if_range='w/"v3-strong"')).json()
        self.assertEqual(d["status_code"], 200)
        self.assertFalse(d["if_range_matched"])

    def test_if_range_date_match_same_instant(self) -> None:
        d = self.post(self.req(
            "bytes=0-9", etag=None, last_modified=LAST_MODIFIED,
            if_range=LAST_MODIFIED,
        )).json()
        self.assertEqual(d["status_code"], 206)
        self.assertEqual(d["if_range_kind"], "date")
        self.assertTrue(d["if_range_matched"])

    def test_if_range_date_match_future_instant(self) -> None:
        d = self.post(self.req(
            "bytes=0-9", etag=None, last_modified=LAST_MODIFIED,
            if_range="Thu, 22 Oct 2015 07:28:00 GMT",
        )).json()
        self.assertEqual(d["status_code"], 206)

    def test_if_range_date_past_ignores_range(self) -> None:
        d = self.post(self.req(
            "bytes=0-9", etag=None, last_modified=LAST_MODIFIED,
            if_range="Tue, 20 Oct 2015 07:28:00 GMT",
        )).json()
        self.assertEqual(d["status_code"], 200)
        self.assertEqual(d["reason"], "if_range_mismatch")

    def test_if_range_date_missing_last_modified_means_mismatch(self) -> None:
        # 只有 ETag 验证器，却用日期形式 If-Range：没有可比较的
        # Last-Modified，按不匹配处理（忽略 Range 回完整对象）。
        d = self.post(self.req(
            "bytes=0-9", etag=STRONG_ETAG,
            if_range=LAST_MODIFIED,
        )).json()
        self.assertEqual(d["status_code"], 200)
        self.assertFalse(d["if_range_matched"])

    def test_if_range_etag_without_current_etag_means_mismatch(self) -> None:
        d = self.post(self.req(
            "bytes=0-9", etag=None, last_modified=LAST_MODIFIED,
            if_range=STRONG_ETAG,
        )).json()
        self.assertEqual(d["status_code"], 200)
        self.assertFalse(d["if_range_matched"])

    def test_if_range_without_range_ignored_but_syntax_checked(self) -> None:
        # 无 Range 时即便携带合法 If-Range，仍是 range_absent 200。
        d = self.post(self.req(None, if_range=STRONG_ETAG)).json()
        self.assertEqual(d["status_code"], 200)
        self.assertEqual(d["reason"], "range_absent")
        self.assertIsNone(d["if_range_kind"])
        # 但非法 If-Range 头值依然是可定位的 422。
        resp = self.post(self.req(None, if_range="not-an-etag-or-date"))
        self.err422(resp, loc="if_range")

    # ----------------------------- 422：Range 头 -----------------------------

    def test_invalid_range_unit(self) -> None:
        # 单位名错误、'=' 缺失或前面有空白都按单位级语法错误定位。
        for raw in ("items=0-9", "bytes 0-9", "byte=0-9", "bytes0-9"):
            with self.subTest(raw=raw):
                resp = self.post(self.req(raw))
                self.err422(resp, loc="range_header")
        # ';' 与非数字字符落在成员上，定位到具体 range 下标。
        resp = self.post(self.req("bytes=0-9; foo"))
        self.err422(resp, loc="range_header.ranges[0]")

    def test_reversed_interval_is_syntax_error(self) -> None:
        resp = self.post(self.req("bytes=9-0"))
        self.err422(resp, loc="range_header.ranges[0]",
                   code="invalid_range_spec")

    def test_suffix_zero_is_syntax_error(self) -> None:
        resp = self.post(self.req("bytes=-0"))
        self.err422(resp, loc="range_header.ranges[0]",
                   code="invalid_range_spec")

    def test_internal_whitespace_rejected(self) -> None:
        # '=' 之后的前导 OWS 合法（在成员拆分后被 strip）；数字表达式
        # 内部的空白一律拒绝。
        for raw in ("bytes=1- 2", "bytes=1 -2", "bytes=- 10"):
            with self.subTest(raw=raw):
                resp = self.post(self.req(raw))
                self.assertEqual(resp.status_code, 422, resp.text)
                self.assertTrue(
                    resp.json()["detail"][0]["loc"].startswith("range_header")
                )

    def test_empty_and_garbage_expressions(self) -> None:
        for raw in ("bytes=", "bytes=,", "bytes=abc", "bytes=--",
                    "bytes=1-2-3", "bytes=1.5-2", "bytes=+1-2"):
            with self.subTest(raw=raw):
                resp = self.post(self.req(raw))
                self.err422(resp, code=None)
                self.assertTrue(
                    resp.json()["detail"][0]["loc"].startswith("range_header")
                )

    def test_control_characters_rejected(self) -> None:
        for raw in ("bytes=0-9\r\nX: 1", "bytes=0-9\n", "bytes=\x000-9"):
            with self.subTest(raw=raw):
                resp = self.post(self.req(raw))
                self.err422(resp, code="malformed_header")

    def test_error_loc_points_to_range_index(self) -> None:
        resp = self.post(self.req("bytes=0-9, 5-1"))
        detail = self.err422(resp)["detail"][0]
        self.assertEqual(detail["loc"], "range_header.ranges[1]")
        self.assertEqual(detail["range_index"], 1)

    # --------------------------- 422：当前验证器 ---------------------------

    def test_current_weak_etag_rejected(self) -> None:
        resp = self.post(self.req("bytes=0-9", etag='W/"v3"'))
        self.err422(resp, loc="etag", code="weak_current_etag")

    def test_current_invalid_etag_rejected(self) -> None:
        for v in ('"un closed', "v3", '"bad etag"'):
            with self.subTest(v=v):
                resp = self.post(self.req("bytes=0-9", etag=v))
                self.err422(resp, loc="etag")
        # 显式空字符串同样是非法头值（区别于省略字段）。
        resp = self.post(self.req("bytes=0-9", etag=""))
        self.err422(resp, loc="etag", code="missing_etag")
        resp = self.post(self.req("bytes=0-9", etag="   "))
        self.err422(resp, loc="etag", code="missing_etag")

    def test_current_invalid_last_modified_rejected(self) -> None:
        for v in ("Wed, 21 Oct 2015 07:28:00 UTC",
                  "Tuesday, 21-Oct-15 07:28:00 GMT",
                  "Tue, 21 Oct 2015 07:28:00 GMT",  # 星期与日期不一致
                  "Wed, 32 Oct 2015 07:28:00 GMT",
                  "Wed, 21 Oct 2015 25:00:00 GMT"):
            with self.subTest(v=v):
                resp = self.post(self.req(
                    "bytes=0-9", etag=None, last_modified=v))
                self.err422(resp, loc="last_modified")

    def test_neither_validator_provided(self) -> None:
        body = {
            "object_length": OBJECT_LENGTH,
            "range_header": "bytes=0-9",
            "max_gap": 8,
            "fetch_limit": 128,
        }
        resp = self.post(body)
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_request")

    def test_blank_validators_rejected_with_loc(self) -> None:
        # etag/last_modified 显式空白字符串按非法头值定位（区别于字段缺失）。
        resp = self.post(self.req("bytes=0-9", etag="   "))
        self.err422(resp, loc="etag")
        resp = self.post(self.req(
            "bytes=0-9", etag=None, last_modified="\t"))
        self.err422(resp, loc="last_modified")

    # ----------------------------- 422：If-Range -----------------------------

    def test_invalid_if_range_etag(self) -> None:
        resp = self.post(self.req("bytes=0-9", if_range='"broken etag"'))
        self.err422(resp, loc="if_range", code="invalid_if_range_etag")

    def test_invalid_if_range_date(self) -> None:
        resp = self.post(self.req("bytes=0-9", if_range="21 Oct 2015 07:28"))
        self.err422(resp, loc="if_range", code="invalid_if_range_date")

    def test_blank_if_range_is_422(self) -> None:
        resp = self.post(self.req("bytes=0-9", if_range="  "))
        self.err422(resp, loc="if_range", code="invalid_if_range")

    # ----------------------------- 字段校验 -----------------------------

    def test_missing_required_fields_422(self) -> None:
        resp = self.client.post("/client-range-decision", json={})
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_request")

    def test_wrong_types_422(self) -> None:
        good = self.req("bytes=0-9")
        for patch in (
            {"object_length": "big"},
            {"max_gap": -1},
            {"fetch_limit": 0},
            {"cached_blocks": "no"},
            {"etag": 123},
        ):
            body = dict(good)
            body.update(patch)
            with self.subTest(patch=patch):
                resp = self.post(body)
                self.assertEqual(resp.status_code, 422, resp.text)
                self.assertEqual(resp.json()["error"], "invalid_request")

    def test_duplicate_cache_id_422(self) -> None:
        body = self.req("bytes=0-99", cached_blocks=[
            {"cache_id": "a", "start": 0, "end": 9},
            {"cache_id": "a", "start": 20, "end": 29},
        ])
        resp = self.post(body)
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_request")

    # ----------------------------- 端到端对接 -----------------------------

    def test_decision_to_assemble_to_range_response_flow(self) -> None:
        """决策 → /assemble → /range-response 全链路（多段 multipart）。"""
        d = self.post(self.req("bytes=0-9, 100-104, -6")).json()
        self.assertEqual(d["status_code"], 206)
        plan = d["plan"]
        contents = [
            {
                "block_index": b["index"],
                "data_b64": base64.b64encode(
                    self.obj[b["start"] : b["end"] + 1]
                ).decode("ascii"),
            }
            for b in plan["blocks"]
        ]
        asm = self.client.post("/assemble", json={
            "object_length": OBJECT_LENGTH,
            "blocks": plan["blocks"],
            "mappings": plan["mappings"],
            "contents": contents,
        })
        self.assertEqual(asm.status_code, 200, asm.text)

        out = self.client.post("/range-response", json={
            "object_length": OBJECT_LENGTH,
            "ranges": asm.json()["ranges"],
            "content_type": "application/octet-stream",
        })
        self.assertEqual(out.status_code, 200, out.text)
        result = out.json()
        self.assertEqual(result["media_type"], "multipart/byteranges")
        raw = base64.b64decode(result["body_b64"])
        self.assertEqual(len(raw), result["content_length"])
        self.assertIn(self.obj[0:10], raw)
        self.assertIn(self.obj[100:105], raw)
        self.assertIn(self.obj[250:256], raw)


if __name__ == "__main__":
    unittest.main()
