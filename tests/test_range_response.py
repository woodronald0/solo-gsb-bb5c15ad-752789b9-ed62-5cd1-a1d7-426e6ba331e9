# -*- coding: utf-8 -*-
"""POST /range-response 的行为测试。

覆盖：
- 成功路径：单段直接输出范围字节（Content-Type/Content-Length/
  Content-Range），多段按下标顺序生成 multipart/byteranges，每段附
  Content-Type 与 Content-Range；输入乱序时按 index 排序；suffix 与
  interval 混用；Content-Length 始终等于最终二进制正文长度；boundary
  合法、随机且不与正文冲突；生成的 multipart 可被 /multipart-parse
  严格解析回同样的字节；
- 下标校验：负数、重复、未恰好覆盖 0..N-1（缺号/越号）；
- 区间校验：end < start、越界（end >= object_length、负 start）；
- 长度校验：length 与区间不一致、Base64 解码字节数与声明长度不符；
- Base64：非法填充、URL-safe 字母表拒绝；
- content_type：空值、缺斜杠、CR/LF 注入、非 ASCII、非法参数等拒绝；
  合法参数（含 charset、quoted-string）原样保留；
- 字段校验：缺字段、错误类型返回 422 invalid_request；
- 错误响应不回显 data_b64。

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
MEDIA_TYPE = "application/octet-stream"


@unittest.skipIf(TestClient is None, "需要安装 starlette（随 fastapi 安装）")
class RangeResponseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.obj = bytes((i % 256 for i in range(OBJECT_LENGTH)))

    # ----------------------------- 构造工具 -----------------------------

    def rng(self, index: int, start: int, end: int, data: bytes | None = None) -> dict:
        if data is None:
            data = self.obj[start : end + 1]
        return {
            "index": index,
            "type": "interval",
            "start": start,
            "end": end,
            "length": end - start + 1,
            "data_b64": base64.b64encode(data).decode("ascii"),
        }

    def request(self, ranges: list[dict], *, content_type: str = MEDIA_TYPE,
                object_length: int = OBJECT_LENGTH) -> dict:
        return {
            "object_length": object_length,
            "ranges": ranges,
            "content_type": content_type,
        }

    def post(self, body: dict):
        return self.client.post("/range-response", json=body)

    def err(self, resp, code: str, range_index: int | None = None) -> dict:
        self.assertEqual(resp.status_code, 422, resp.text)
        payload = resp.json()
        self.assertEqual(payload["error"], code, payload)
        detail = payload["detail"][0]
        self.assertIn("message", detail)
        if range_index is not None:
            self.assertEqual(detail.get("range_index"), range_index, detail)
        return detail

    # ----------------------------- 单段成功 -----------------------------

    def test_single_range_success(self) -> None:
        resp = self.post(self.request([self.rng(0, 10, 19)]))
        self.assertEqual(resp.status_code, 200, resp.text)
        out = resp.json()
        self.assertEqual(out["status_code"], 206)
        self.assertEqual(out["media_type"], "single")
        self.assertIsNone(out["boundary"])
        self.assertEqual(
            out["headers"],
            {
                "Content-Type": MEDIA_TYPE,
                "Content-Length": "10",
                "Content-Range": "bytes 10-19/256",
            },
        )
        self.assertEqual(out["content_length"], 10)
        self.assertEqual(base64.b64decode(out["body_b64"]), self.obj[10:20])

    def test_single_range_full_object(self) -> None:
        resp = self.post(self.request([self.rng(0, 0, 255)]))
        self.assertEqual(resp.status_code, 200, resp.text)
        out = resp.json()
        self.assertEqual(out["media_type"], "single")
        self.assertEqual(out["content_length"], 256)
        self.assertEqual(base64.b64decode(out["body_b64"]), self.obj)
        self.assertEqual(out["headers"]["Content-Range"], "bytes 0-255/256")

    def test_single_range_suffix_style_interval(self) -> None:
        # /assemble 输出的 suffix 范围 type=suffix，但区间已规范化。
        body = self.request([
            {
                "index": 0,
                "type": "suffix",
                "start": 240,
                "end": 255,
                "length": 16,
                "data_b64": base64.b64encode(self.obj[240:256]).decode(),
            }
        ])
        resp = self.post(body)
        self.assertEqual(resp.status_code, 200, resp.text)
        out = resp.json()
        self.assertEqual(base64.b64decode(out["body_b64"]), self.obj[240:256])
        self.assertEqual(out["headers"]["Content-Range"], "bytes 240-255/256")

    def test_single_range_binary_content_verbatim(self) -> None:
        data = bytes(range(256))
        resp = self.post(self.request([self.rng(0, 0, 255, data)]))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(base64.b64decode(resp.json()["body_b64"]), data)

    # ----------------------------- 多段成功 -----------------------------

    def test_multi_range_success(self) -> None:
        resp = self.post(self.request([self.rng(0, 0, 9), self.rng(1, 250, 255)]))
        self.assertEqual(resp.status_code, 200, resp.text)
        out = resp.json()
        self.assertEqual(out["status_code"], 206)
        self.assertEqual(out["media_type"], "multipart/byteranges")
        boundary = out["boundary"]
        self.assertIsNotNone(boundary)
        self.assertEqual(
            out["headers"]["Content-Type"],
            f"multipart/byteranges; boundary={boundary}",
        )
        body = base64.b64decode(out["body_b64"])
        self.assertEqual(len(body), out["content_length"])
        self.assertEqual(out["headers"]["Content-Length"], str(len(body)))
        # 严格 CRLF 分帧：开边界 / 段头 / 结束边界都在。
        self.assertTrue(body.startswith(f"--{boundary}\r\n".encode()))
        self.assertTrue(body.endswith(f"--{boundary}--".encode()))
        self.assertIn(b"Content-Type: application/octet-stream\r\n", body)
        self.assertIn(b"Content-Range: bytes 0-9/256\r\n\r\n", body)
        self.assertIn(b"Content-Range: bytes 250-255/256\r\n\r\n", body)
        # 正文按 index 顺序：第一页是 0..9 的字节。
        self.assertIn(self.obj[0:10], body)
        self.assertIn(self.obj[250:256], body)

    def test_multi_range_ordered_by_index_not_input_order(self) -> None:
        # 输入乱序（1 在前），输出必须按 index 0,1 排列。
        resp = self.post(self.request([self.rng(1, 250, 255), self.rng(0, 0, 9)]))
        self.assertEqual(resp.status_code, 200, resp.text)
        out = resp.json()
        body = base64.b64decode(out["body_b64"])
        boundary = out["boundary"]
        pos0 = body.find(b"Content-Range: bytes 0-9/256")
        pos1 = body.find(b"Content-Range: bytes 250-255/256")
        self.assertGreaterEqual(pos0, 0)
        self.assertGreater(pos1, pos0)
        self.assertTrue(body.startswith(f"--{boundary}\r\n".encode()))

    def test_multi_range_three_parts(self) -> None:
        resp = self.post(self.request([
            self.rng(0, 0, 4), self.rng(1, 100, 104), self.rng(2, 251, 255)
        ]))
        self.assertEqual(resp.status_code, 200, resp.text)
        body = base64.b64decode(resp.json()["body_b64"])
        for cr in (b"bytes 0-4/256", b"bytes 100-104/256", b"bytes 251-255/256"):
            self.assertIn(cr, body)
        self.assertEqual(body.count(b"Content-Type: "), 3)

    def test_boundary_is_valid_bchars_and_not_in_payload_conflict(self) -> None:
        import re
        boundary_re = re.compile(r"^[0-9A-Za-z'()+_,-./:=?]{1,70}$")
        out = self.post(
            self.request([self.rng(0, 0, 9), self.rng(1, 240, 255)])
        ).json()
        self.assertTrue(boundary_re.fullmatch(out["boundary"]))
        body = base64.b64decode(out["body_b64"])
        # 边界出现次数 = 段数 + 1（开边界一次、段间一次，末段为结束）。
        delim = b"--" + out["boundary"].encode()
        self.assertEqual(body.count(delim), 3)

    def test_boundary_safe_against_payload_containing_prefixes(self) -> None:
        # 正文里刻意放入 boundary 前缀与 -- 序列：随机 boundary 仍不得与
        # 任何正文分隔行冲突，生成结果必须能被严格解析器解析。
        evil = b"--range-response-\r\n--\r\nContent-Range: bytes 0-3/256\r\n\r\nX"
        payload1 = evil + self.obj[10:20]  # 长度 = len(evil) + 10
        r1 = self.rng(0, 0, 9)
        r2 = {
            "index": 1,
            "type": "interval",
            "start": 10,
            "end": 10 + len(payload1) - 1,
            "length": len(payload1),
            "data_b64": base64.b64encode(payload1).decode(),
        }
        assert r2["end"] < OBJECT_LENGTH
        out = self.post(self.request([r1, r2])).json()
        parsed = self.client.post("/multipart-parse", json={
            "object_length": OBJECT_LENGTH,
            "ranges": [
                {"type": "interval", "start": 0, "end": 9},
                {"type": "interval", "start": 10, "end": r2["end"]},
            ],
            "boundary": out["boundary"],
            "body_b64": out["body_b64"],
        })
        self.assertEqual(parsed.status_code, 200, parsed.text)
        parts = parsed.json()["ranges"]
        self.assertEqual(base64.b64decode(parts[1]["data_b64"]), payload1)

    def test_boundary_randomized_between_calls(self) -> None:
        req = self.request([self.rng(0, 0, 9), self.rng(1, 250, 255)])
        b1 = self.post(req).json()["boundary"]
        b2 = self.post(req).json()["boundary"]
        self.assertNotEqual(b1, b2)

    def test_content_length_matches_final_binary_body(self) -> None:
        # 多段时 Content-Length 是整个 multipart 分帧正文（含头与边界），
        # 而不是范围字节之和。
        out = self.post(
            self.request([self.rng(0, 0, 9), self.rng(1, 250, 255)])
        ).json()
        raw = base64.b64decode(out["body_b64"])
        self.assertEqual(out["content_length"], len(raw))
        self.assertGreater(len(raw), 10 + 6)

    # ------------------ 与 /multipart-parse 的往返一致性 ------------------

    def test_generated_multipart_roundtrips_through_parser(self) -> None:
        gen = self.post(
            self.request([self.rng(0, 0, 99), self.rng(1, 240, 255)],
                         content_type="video/mp4")
        ).json()
        parsed = self.client.post("/multipart-parse", json={
            "object_length": OBJECT_LENGTH,
            "ranges": [
                {"type": "interval", "start": 0, "end": 99},
                {"type": "interval", "start": 240, "end": 255},
            ],
            "boundary": gen["boundary"],
            "expected_content_type": "video/mp4",
            "body_b64": gen["body_b64"],
        })
        self.assertEqual(parsed.status_code, 200, parsed.text)
        parts = parsed.json()["ranges"]
        self.assertEqual(base64.b64decode(parts[0]["data_b64"]), self.obj[0:100])
        self.assertEqual(base64.b64decode(parts[1]["data_b64"]), self.obj[240:256])

    # ----------------------------- 下标校验 -----------------------------

    def test_negative_index_rejected(self) -> None:
        self.err(self.post(self.request([self.rng(-1, 0, 9)])),
                 "invalid_range_index", -1)

    def test_duplicate_index_rejected(self) -> None:
        self.err(
            self.post(self.request([self.rng(0, 0, 9), self.rng(0, 10, 19)])),
            "duplicate_range_index", 0,
        )

    def test_indexes_must_cover_zero_to_n_minus_one_missing(self) -> None:
        # 两段但下标为 0,2：缺少 1，存在多余下标 2。
        detail = self.err(
            self.post(self.request([self.rng(0, 0, 9), self.rng(2, 10, 19)])),
            "invalid_range_index",
        )
        self.assertEqual(detail["loc"], "ranges")

    def test_indexes_start_at_one_rejected(self) -> None:
        detail = self.err(
            self.post(self.request([self.rng(1, 0, 9)])),
            "invalid_range_index",
        )
        self.assertEqual(detail["loc"], "ranges")

    # ----------------------------- 区间校验 -----------------------------

    def test_end_before_start_rejected(self) -> None:
        bad = self.rng(0, 0, 9)
        bad["start"], bad["end"], bad["length"] = 20, 10, 1
        detail = self.err(self.post(self.request([bad])), "invalid_range", 0)
        self.assertEqual(detail["loc"], "ranges[0]")

    def test_range_end_out_of_bounds_rejected(self) -> None:
        self.err(
            self.post(self.request([self.rng(0, 250, 256)])),
            "range_out_of_bounds", 0,
        )

    def test_range_start_negative_rejected(self) -> None:
        # AssembledRange.start 无字段级下界，端点语义校验统一拒绝。
        bad = self.rng(0, 0, 9)
        bad["start"] = -1
        bad["length"] = 10
        self.err(self.post(self.request([bad])), "range_out_of_bounds", 0)

    # ----------------------------- 长度校验 -----------------------------

    def test_declared_length_mismatch_interval(self) -> None:
        bad = self.rng(0, 0, 9)
        bad["length"] = 9  # 区间 10 字节
        self.err(self.post(self.request([bad])), "range_length_mismatch", 0)

    def test_decoded_bytes_shorter_than_length(self) -> None:
        bad = self.rng(0, 0, 9, b"x" * 9)  # b64 只有 9 字节，声明 10
        self.err(self.post(self.request([bad])), "body_length_mismatch", 0)

    def test_decoded_bytes_longer_than_length(self) -> None:
        bad = self.rng(0, 0, 9, b"x" * 11)
        self.err(self.post(self.request([bad])), "body_length_mismatch", 0)

    # ----------------------------- Base64 -----------------------------

    def test_invalid_base64_rejected(self) -> None:
        bad = self.rng(0, 0, 9)
        bad["data_b64"] = "@@@not-base64@@@"
        self.err(self.post(self.request([bad])), "invalid_base64", 0)

    def test_base64_missing_padding_rejected(self) -> None:
        bad = self.rng(0, 0, 9)
        raw = bad["data_b64"].rstrip("=")
        bad["data_b64"] = raw
        self.err(self.post(self.request([bad])), "invalid_base64", 0)

    def test_urlsafe_alphabet_rejected(self) -> None:
        bad = self.rng(0, 240, 255)
        raw = base64.urlsafe_b64encode(self.obj[240:256]).decode()
        assert "-" in raw or "_" in raw
        bad["data_b64"] = raw
        self.err(self.post(self.request([bad])), "invalid_base64", 0)

    # --------------------------- content_type ---------------------------

    def test_content_type_with_parameters_preserved(self) -> None:
        ct = "text/plain; charset=utf-8"
        out = self.post(self.request([self.rng(0, 0, 9)], content_type=ct)).json()
        self.assertEqual(out["headers"]["Content-Type"], ct)

    def test_content_type_quoted_parameter_preserved(self) -> None:
        ct = 'text/plain; charset="utf-8"'
        out = self.post(self.request([self.rng(0, 0, 9)], content_type=ct)).json()
        self.assertEqual(out["headers"]["Content-Type"], ct)

    def test_content_type_multipart_each_part_uses_it(self) -> None:
        ct = "video/mp4; codecs=avc1"
        out = self.post(
            self.request([self.rng(0, 0, 9), self.rng(1, 10, 19)], content_type=ct)
        ).json()
        body = base64.b64decode(out["body_b64"])
        self.assertEqual(body.count(f"Content-Type: {ct}\r\n".encode()), 2)

    def test_content_type_empty_rejected(self) -> None:
        resp = self.post(self.request([self.rng(0, 0, 9)], content_type="   "))
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["detail"][0]["loc"], "content_type")

    def test_content_type_missing_slash_rejected(self) -> None:
        resp = self.post(self.request([self.rng(0, 0, 9)], content_type="octet"))
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_content_type")

    def test_content_type_crlf_injection_rejected(self) -> None:
        for evil in (
            "application/octet-stream\r\nX-Injected: 1",
            "application/octet-stream\nX-Injected: 1",
            "a/b; x=\"y\r\nZ: 1\"",
        ):
            with self.subTest(evil=evil):
                resp = self.post(self.request([self.rng(0, 0, 9)], content_type=evil))
                self.assertEqual(resp.status_code, 422)
                self.assertEqual(resp.json()["error"], "invalid_content_type")

    def test_content_type_non_ascii_rejected(self) -> None:
        resp = self.post(
            self.request([self.rng(0, 0, 9)], content_type="文本/纯文本")
        )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_content_type")

    def test_content_type_bad_parameter_rejected(self) -> None:
        for bad in ("a/b; badparam", 'a/b; x="unclosed', "a/b; ; x=1"):
            with self.subTest(bad=bad):
                resp = self.post(self.request([self.rng(0, 0, 9)], content_type=bad))
                self.assertEqual(resp.status_code, 422)
                self.assertEqual(resp.json()["error"], "invalid_content_type")

    # ----------------------------- 字段校验 -----------------------------

    def test_missing_required_fields_422(self) -> None:
        resp = self.client.post("/range-response", json={})
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_request")

    def test_empty_ranges_422(self) -> None:
        resp = self.post(
            {"object_length": 256, "ranges": [], "content_type": "a/b"}
        )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_request")

    def test_wrong_types_422(self) -> None:
        good = self.request([self.rng(0, 0, 9)])
        for patch in (
            {"object_length": "not-an-int"},
            {"ranges": "not-a-list"},
            {"content_type": 123},
        ):
            body = dict(good)
            body.update(patch)
            with self.subTest(patch=patch):
                resp = self.post(body)
                self.assertEqual(resp.status_code, 422)
                self.assertEqual(resp.json()["error"], "invalid_request")

    def test_negative_object_length_422(self) -> None:
        resp = self.post(self.request([self.rng(0, 0, 9)], object_length=-1))
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_request")

    # ----------------------------- 错误响应 -----------------------------

    def test_error_never_echoes_data_b64(self) -> None:
        secret = base64.b64encode(b"super-secret-payload!!").decode()
        bad = self.rng(0, 0, 99)
        bad["data_b64"] = secret
        resp = self.post(self.request([bad]))
        self.assertEqual(resp.status_code, 422)
        self.assertNotIn(secret, resp.text)

    def test_error_detail_shape(self) -> None:
        bad = self.rng(0, 0, 9)
        bad["length"] = 9
        resp = self.post(self.request([bad]))
        payload = resp.json()
        self.assertIsInstance(payload["detail"], list)
        self.assertEqual(len(payload["detail"]), 1)
        self.assertEqual(payload["detail"][0]["loc"], "ranges[0].length")

    # ----------------------------- 端到端对接 -----------------------------

    def test_full_plan_assemble_range_response_flow(self) -> None:
        """/plan → 模拟回源 → /assemble → /range-response 全链路。"""
        plan = self.client.post("/plan", json={
            "object_length": OBJECT_LENGTH,
            "ranges": [
                {"type": "interval", "start": 0, "end": 99},
                {"type": "suffix", "last_n": 16},
            ],
            "max_gap": 8,
            "fetch_limit": 128,
        }).json()
        contents = []
        for blk in plan["blocks"]:
            s, e = blk["start"], blk["end"]
            contents.append({
                "block_index": blk["index"],
                "data_b64": base64.b64encode(self.obj[s : e + 1]).decode(),
            })
        asm = self.client.post("/assemble", json={
            "object_length": OBJECT_LENGTH,
            "blocks": plan["blocks"],
            "mappings": plan["mappings"],
            "contents": contents,
        })
        self.assertEqual(asm.status_code, 200, asm.text)

        out = self.post({
            "object_length": OBJECT_LENGTH,
            "ranges": asm.json()["ranges"],
            "content_type": "application/octet-stream",
        })
        self.assertEqual(out.status_code, 200, out.text)
        result = out.json()
        self.assertEqual(result["media_type"], "multipart/byteranges")
        raw = base64.b64decode(result["body_b64"])
        self.assertEqual(len(raw), result["content_length"])
        # multipart 正文内含两段的完整字节。
        self.assertIn(self.obj[0:100], raw)
        self.assertIn(self.obj[240:256], raw)


if __name__ == "__main__":
    unittest.main()
