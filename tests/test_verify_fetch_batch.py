# -*- coding: utf-8 -*-
"""POST /verify-fetch-batch 的行为测试。

覆盖：
- 成功路径：206 + 正确 Content-Range/长度/强 ETag 时按块编号返回可直接
  用于 /assemble.contents 的数据与确认 ETag（块编号乱序也按升序输出）；
  expected_etag 一致/不一致；空 blocks 场景；
- 状态码：非 206（200/416/304 等）拒绝；
- Content-Range：格式非法、total 与对象总长不符、闭区间与规划块不符；
- 响应体：Base64 非法、解码字节数与声明长度不符；
- 块集合：缺块、重复响应、多余块、负数编号、回显 blocks 自身非法；
- ETag：缺失、弱标签（大小写 W/）、非法格式、块间不一致（不同版本混入）、
  expected_etag 为弱标签/非法语法/与批次不一致；
- 错误响应：统一 422、detail 携带 block_index 与原因，且绝不回显响应体
  （不含提交的 body_b64 明文）。

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

ETAG_V1 = '"v1-abcd"'
ETAG_V2 = '"v2-efgh"'
WEAK_ETAG = 'W/"v1-abcd"'


@unittest.skipIf(TestClient is None, "需要安装 starlette（随 fastapi 安装）")
class VerifyFetchBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    # ----------------------------- 构造工具 -----------------------------

    @staticmethod
    def block(index: int, start: int, end: int) -> dict:
        return {
            "index": index,
            "start": start,
            "end": end,
            "length": end - start + 1,
            "range_header": f"bytes={start}-{end}",
        }

    @staticmethod
    def response(
        index: int,
        data: bytes,
        *,
        start: int | None = None,
        end: int | None = None,
        total: int = 256,
        status: int = 206,
        etag: str | None = ETAG_V1,
        cr: str | None = None,
    ) -> dict:
        if cr is None:
            assert start is not None and end is not None
            cr = f"bytes {start}-{end}/{total}"
        return {
            "block_index": index,
            "status_code": status,
            "content_range": cr,
            "etag": etag,
            "body_b64": base64.b64encode(data).decode("ascii"),
        }

    def plan_blocks(self) -> list[dict]:
        # 与 README 示例一致的三个回源块：[0,39] [60,99] [240,255]
        return [self.block(0, 0, 39), self.block(1, 60, 99), self.block(2, 240, 255)]

    def object_bytes(self) -> bytes:
        return bytes((i % 256 for i in range(256)))

    def valid_request(self, **over) -> dict:
        obj = self.object_bytes()
        blocks = self.plan_blocks()
        responses = [
            self.response(0, obj[0:40], start=0, end=39),
            self.response(1, obj[60:100], start=60, end=99),
            self.response(2, obj[240:256], start=240, end=255),
        ]
        req = {"object_length": 256, "blocks": blocks, "responses": responses}
        req.update(over)
        return req

    def post(self, body: dict):
        return self.client.post("/verify-fetch-batch", json=body)

    def err(self, resp, code: str, block_index: int | None = None) -> dict:
        self.assertEqual(resp.status_code, 422, resp.text)
        payload = resp.json()
        self.assertEqual(payload["error"], code, payload)
        detail = payload["detail"][0]
        self.assertIn("message", detail)
        if block_index is not None:
            self.assertEqual(detail.get("block_index"), block_index, detail)
        return detail

    # ----------------------------- 成功路径 -----------------------------

    def test_success_returns_assemble_contents_and_etag(self) -> None:
        body = self.valid_request()
        resp = self.post(body)
        self.assertEqual(resp.status_code, 200, resp.text)
        result = resp.json()
        self.assertEqual(result["object_length"], 256)
        self.assertEqual(result["etag"], ETAG_V1)
        self.assertEqual([c["block_index"] for c in result["contents"]], [0, 1, 2])
        # 内容与提交的 body_b64 逐字节一致，可直接用于 /assemble。
        for content, src in zip(result["contents"], body["responses"]):
            self.assertEqual(content["data_b64"], src["body_b64"])
            raw = base64.b64decode(content["data_b64"])
            obj = self.object_bytes()
            idx = content["block_index"]
            start, end = {0: (0, 40), 1: (60, 100), 2: (240, 256)}[idx]
            self.assertEqual(raw, obj[start:end])

    def test_success_output_sorted_by_index_when_input_shuffled(self) -> None:
        body = self.valid_request()
        body["responses"] = body["responses"][::-1]  # 2,1,0 乱序提交
        resp = self.post(body)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            [c["block_index"] for c in resp.json()["contents"]], [0, 1, 2]
        )

    def test_success_non_contiguous_block_indexes(self) -> None:
        obj = self.object_bytes()
        body = {
            "object_length": 256,
            "blocks": [self.block(7, 0, 9), self.block(3, 246, 255)],
            "responses": [
                self.response(3, obj[246:256], start=246, end=255),
                self.response(7, obj[0:10], start=0, end=9),
            ],
        }
        resp = self.post(body)
        self.assertEqual(resp.status_code, 200, resp.text)
        result = resp.json()
        self.assertEqual([c["block_index"] for c in result["contents"]], [3, 7])

    def test_success_etag_with_ows_is_normalized(self) -> None:
        body = self.valid_request()
        for item in body["responses"]:
            item["etag"] = f' \t{ETAG_V1}\t '
        resp = self.post(body)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["etag"], ETAG_V1)

    def test_success_with_expected_etag(self) -> None:
        resp = self.post(self.valid_request(expected_etag=ETAG_V1))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["etag"], ETAG_V1)

    def test_success_expected_etag_with_ows(self) -> None:
        resp = self.post(self.valid_request(expected_etag=f' {ETAG_V1} '))
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_success_single_block_full_object_range(self) -> None:
        # 206 覆盖整个对象（闭区间恰好 [0,len-1]）也是合法分块响应。
        obj = self.object_bytes()
        body = {
            "object_length": 256,
            "blocks": [self.block(0, 0, 255)],
            "responses": [self.response(0, obj, start=0, end=255)],
        }
        resp = self.post(body)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            base64.b64decode(resp.json()["contents"][0]["data_b64"]), obj
        )

    def test_empty_blocks_success_with_null_etag(self) -> None:
        body = {"object_length": 256, "blocks": [], "responses": []}
        resp = self.post(body)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json(), {"object_length": 256, "contents": [], "etag": None})

    # ----------------------------- 状态码 -----------------------------

    def test_non_206_statuses_rejected(self) -> None:
        for status in (200, 204, 304, 404, 416, 500):
            with self.subTest(status=status):
                body = self.valid_request()
                body["responses"][0]["status_code"] = status
                resp = self.post(body)
                self.err(resp, "non_206_status", 0)

    # --------------------------- Content-Range ---------------------------

    def test_content_range_wrong_total(self) -> None:
        obj = self.object_bytes()
        body = {
            "object_length": 256,
            "blocks": [self.block(0, 0, 39)],
            "responses": [self.response(0, obj[0:40], start=0, end=39, total=255)],
        }
        self.err(self.post(body), "content_range_total_mismatch", 0)

    def test_content_range_wrong_interval(self) -> None:
        obj = self.object_bytes()
        body = {
            "object_length": 256,
            "blocks": [self.block(0, 0, 39)],
            "responses": [self.response(0, obj[0:40], start=0, end=38, total=256)],
        }
        self.err(self.post(body), "content_range_mismatch", 0)
        # 区间右移（同样 40 字节）也必须拒绝。
        body["responses"][0]["content_range"] = "bytes 1-40/256"
        self.err(self.post(body), "content_range_mismatch", 0)

    def test_content_range_malformed(self) -> None:
        obj = self.object_bytes()
        for bad in (
            "bytes 0-39/*",
            "bytes */256",
            "bytes 0-39",
            "0-39/256",
            "bytes=0-39/256",
            "bytes 39-0/256",
            "items 0-39/256",
            "bytes 0 - 39 / 256",
            "bytes 0-39/256\r\nX-Injected: 1",
        ):
            with self.subTest(bad=bad):
                body = {
                    "object_length": 256,
                    "blocks": [self.block(0, 0, 39)],
                    "responses": [self.response(0, obj[0:40], cr=bad)],
                }
                self.err(self.post(body), "invalid_content_range", 0)

    def test_content_range_ows_tolerated(self) -> None:
        # 单个前导/尾随 OWS（HTAB/SP）允许，与 HTTP 头解析惯例一致。
        body = self.valid_request()
        body["responses"][0]["content_range"] = "  bytes 0-39/256\t"
        resp = self.post(body)
        self.assertEqual(resp.status_code, 200, resp.text)

    # ----------------------------- 响应体 -----------------------------

    def test_body_too_short(self) -> None:
        body = self.valid_request()
        body["responses"][1]["body_b64"] = base64.b64encode(b"x" * 39).decode()
        self.err(self.post(body), "body_length_mismatch", 1)

    def test_body_too_long(self) -> None:
        body = self.valid_request()
        body["responses"][1]["body_b64"] = base64.b64encode(b"x" * 41).decode()
        self.err(self.post(body), "body_length_mismatch", 1)

    def test_body_invalid_base64(self) -> None:
        body = self.valid_request()
        body["responses"][2]["body_b64"] = "@@@not-base64@@@"
        self.err(self.post(body), "invalid_base64", 2)

    def test_body_urlsafe_alphabet_rejected(self) -> None:
        body = self.valid_request()
        # 块 2 的字节（0xF0-0xFF）标准字母表含 +//，urlsafe 变体必然出现
        # 标准字母表中不存在的 - 或 _，validate=True 必须拒绝。
        raw = base64.urlsafe_b64encode(self.object_bytes()[240:256]).decode()
        assert "-" in raw or "_" in raw
        body["responses"][2]["body_b64"] = raw
        self.err(self.post(body), "invalid_base64", 2)

    def test_body_length_off_by_one_despite_matching_b64_shape(self) -> None:
        # Content-Range 声称 [0,39]（40 字节），实际 39 字节；长度必须严格相等。
        obj = self.object_bytes()
        body = {
            "object_length": 256,
            "blocks": [self.block(0, 0, 39)],
            "responses": [
                {
                    "block_index": 0,
                    "status_code": 206,
                    "content_range": "bytes 0-39/256",
                    "etag": ETAG_V1,
                    "body_b64": base64.b64encode(obj[0:39]).decode(),
                }
            ],
        }
        self.err(self.post(body), "body_length_mismatch", 0)

    # ----------------------------- 缺块/重复/多余 -----------------------------

    def test_missing_block(self) -> None:
        body = self.valid_request()
        body["responses"] = body["responses"][:2]  # 缺块 2
        detail = self.err(self.post(body), "missing_blocks", 2)
        self.assertEqual(detail["missing"], [2])

    def test_missing_multiple_blocks_listed(self) -> None:
        body = {
            "object_length": 256,
            "blocks": [self.block(0, 0, 9), self.block(1, 10, 19), self.block(2, 20, 29)],
            "responses": [],
        }
        detail = self.err(self.post(body), "missing_blocks", 0)
        self.assertEqual(detail["missing"], [0, 1, 2])

    def test_duplicate_response_rejected(self) -> None:
        body = self.valid_request()
        body["responses"][2]["block_index"] = 0  # 第二份块 0，且缺块 2
        resp = self.post(body)
        # 按输入顺序先撞到重复（位置 2），重复优先于末尾的缺块检查。
        self.err(resp, "duplicate_response", 0)

    def test_unexpected_block_rejected(self) -> None:
        body = self.valid_request()
        obj = self.object_bytes()
        body["responses"].append(
            self.response(9, obj[250:256], start=250, end=255)
        )
        self.err(self.post(body), "unexpected_block", 9)

    def test_response_for_empty_blocks_is_unexpected(self) -> None:
        obj = self.object_bytes()
        body = {
            "object_length": 256,
            "blocks": [],
            "responses": [self.response(0, obj[0:40], start=0, end=39)],
        }
        self.err(self.post(body), "unexpected_block", 0)

    def test_negative_block_index_rejected(self) -> None:
        body = self.valid_request()
        body["responses"][0]["block_index"] = -1
        self.err(self.post(body), "invalid_block_index", -1)

    def test_blocks_duplicate_index_rejected(self) -> None:
        body = self.valid_request()
        body["blocks"][1]["index"] = 0
        resp = self.post(body)
        self.err(resp, "duplicate_block", 0)

    def test_blocks_invalid_interval_rejected(self) -> None:
        body = self.valid_request()
        body["blocks"][0]["end"] = -1
        self.err(self.post(body), "invalid_block", 0)

    def test_blocks_out_of_bounds_rejected(self) -> None:
        body = self.valid_request()
        body["blocks"][2]["end"] = 256  # 对象长度 256，末偏移必须 < 256
        self.err(self.post(body), "block_out_of_bounds", 2)

    def test_blocks_wrong_length_rejected(self) -> None:
        body = self.valid_request()
        body["blocks"][0]["length"] = 41
        self.err(self.post(body), "invalid_block_length", 0)

    # ------------------------------- ETag -------------------------------

    def test_missing_etag_header(self) -> None:
        body = self.valid_request()
        body["responses"][0]["etag"] = None
        self.err(self.post(body), "missing_etag", 0)

    def test_empty_etag_rejected(self) -> None:
        body = self.valid_request()
        body["responses"][1]["etag"] = "   "
        self.err(self.post(body), "missing_etag", 1)

    def test_weak_etag_rejected(self) -> None:
        body = self.valid_request()
        body["responses"][0]["etag"] = WEAK_ETAG
        self.err(self.post(body), "weak_etag", 0)

    def test_weak_etag_lowercase_prefix_rejected(self) -> None:
        body = self.valid_request()
        body["responses"][0]["etag"] = 'w/"v1-abcd"'
        self.err(self.post(body), "weak_etag", 0)

    def test_invalid_etag_syntax_rejected(self) -> None:
        for bad in ('v1-abcd', '"v1 abcd"', '"v1\nabcd"', '""x', '*', '""'):
            # 注意：空 opaque-tag "" 是语法合法的强 ETag（RFC 9110 允许），
            # 单独处理；其余均非法。
            if bad == '""':
                continue
            with self.subTest(bad=bad):
                body = self.valid_request()
                body["responses"][0]["etag"] = bad
                self.err(self.post(body), "invalid_etag", 0)

    def test_empty_opaque_tag_is_valid_strong_etag(self) -> None:
        body = self.valid_request()
        for item in body["responses"]:
            item["etag"] = '""'
        resp = self.post(body)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["etag"], '""')

    def test_etag_with_obs_text_accepted(self) -> None:
        # RFC 9110 etagc 允许 obs-text(0x80-0xFF)。
        body = self.valid_request()
        tag = '"v\xff\xfe"'
        for item in body["responses"]:
            item["etag"] = tag
        resp = self.post(body)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["etag"], tag)

    def test_etag_mismatch_between_blocks_rejected(self) -> None:
        body = self.valid_request()
        body["responses"][1]["etag"] = ETAG_V2
        detail = self.err(self.post(body), "etag_mismatch", 1)
        # 不得回显任何一方的 ETag 值（版本标签同样不在错误中泄露）。
        self.assertNotIn(ETAG_V2, str(detail))

    def test_expected_etag_mismatch_rejected(self) -> None:
        resp = self.post(self.valid_request(expected_etag=ETAG_V2))
        detail = self.err(resp, "expected_etag_mismatch")
        self.assertEqual(detail["loc"], "expected_etag")

    def test_expected_weak_etag_rejected(self) -> None:
        resp = self.post(self.valid_request(expected_etag=WEAK_ETAG))
        detail = self.err(resp, "weak_expected_etag")
        self.assertEqual(detail["loc"], "expected_etag")

    def test_expected_etag_invalid_syntax_rejected(self) -> None:
        resp = self.post(self.valid_request(expected_etag="not-a-tag"))
        detail = self.err(resp, "invalid_expected_etag")
        self.assertEqual(detail["loc"], "expected_etag")

    def test_expected_etag_unverifiable_with_empty_blocks(self) -> None:
        body = {
            "object_length": 256,
            "blocks": [],
            "responses": [],
            "expected_etag": ETAG_V1,
        }
        resp = self.post(body)
        detail = self.err(resp, "expected_etag_unverifiable")
        self.assertEqual(detail["loc"], "expected_etag")

    # ----------------------------- 错误响应 -----------------------------

    def test_errors_never_echo_body(self) -> None:
        body = self.valid_request()
        secret_b64 = body["responses"][0]["body_b64"]
        body["responses"][0]["status_code"] = 416
        resp = self.post(body)
        self.assertEqual(resp.status_code, 422)
        text = resp.text
        self.assertNotIn(secret_b64, text)

    def test_error_detail_carries_loc(self) -> None:
        body = self.valid_request()
        body["responses"][0]["etag"] = WEAK_ETAG
        detail = self.err(self.post(body), "weak_etag", 0)
        self.assertEqual(detail["loc"], "responses[0].etag")

    def test_error_detail_list_shape(self) -> None:
        body = self.valid_request()
        body["responses"] = []
        resp = self.post(body)
        self.assertEqual(resp.status_code, 422)
        payload = resp.json()
        self.assertIsInstance(payload["detail"], list)
        self.assertEqual(len(payload["detail"]), 1)

    # ----------------------------- 字段校验 -----------------------------

    def test_missing_required_fields_422(self) -> None:
        resp = self.client.post("/verify-fetch-batch", json={})
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_request")

    def test_wrong_types_422(self) -> None:
        body = self.valid_request()
        body["object_length"] = "not-an-int"
        resp = self.post(body)
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_request")

        body = self.valid_request()
        body["responses"][0]["status_code"] = "abc"  # 非数字字符串严格拒绝
        resp = self.post(body)
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_request")

        body = self.valid_request()
        body["responses"][0]["status_code"] = 999  # 超出 100..599
        resp = self.post(body)
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_request")

    def test_negative_object_length_422(self) -> None:
        body = self.valid_request(object_length=-1)
        resp = self.post(body)
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_request")

    # ----------------------------- 端到端对接 -----------------------------

    def test_verified_contents_flow_into_assemble(self) -> None:
        """verify 通过后的 contents 与 blocks/mappings 可直接驱动 /assemble。"""
        obj = self.object_bytes()
        plan_resp = self.client.post(
            "/plan",
            json={
                "object_length": 256,
                "ranges": [
                    {"type": "interval", "start": 0, "end": 99},
                    {"type": "suffix", "last_n": 16},
                ],
                "max_gap": 8,
                "fetch_limit": 128,
            },
        )
        self.assertEqual(plan_resp.status_code, 200, plan_resp.text)
        plan = plan_resp.json()

        responses = []
        for blk in plan["blocks"]:
            s, e = blk["start"], blk["end"]
            responses.append(
                {
                    "block_index": blk["index"],
                    "status_code": 206,
                    "content_range": f"bytes {s}-{e}/256",
                    "etag": ETAG_V1,
                    "body_b64": base64.b64encode(obj[s : e + 1]).decode(),
                }
            )
        verify_resp = self.client.post(
            "/verify-fetch-batch",
            json={
                "object_length": 256,
                "blocks": plan["blocks"],
                "responses": responses,
                "expected_etag": ETAG_V1,
            },
        )
        self.assertEqual(verify_resp.status_code, 200, verify_resp.text)
        verified = verify_resp.json()

        assemble_resp = self.client.post(
            "/assemble",
            json={
                "object_length": 256,
                "blocks": plan["blocks"],
                "mappings": plan["mappings"],
                "contents": verified["contents"],
            },
        )
        self.assertEqual(assemble_resp.status_code, 200, assemble_resp.text)
        ranges = assemble_resp.json()["ranges"]
        self.assertEqual(base64.b64decode(ranges[0]["data_b64"]), obj[0:100])
        self.assertEqual(base64.b64decode(ranges[1]["data_b64"]), obj[240:256])


if __name__ == "__main__":
    unittest.main()
