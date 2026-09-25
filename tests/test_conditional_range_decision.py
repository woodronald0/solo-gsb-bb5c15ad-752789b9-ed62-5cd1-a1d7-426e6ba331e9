# -*- coding: utf-8 -*-
"""POST /conditional-range-decision 的行为测试。

覆盖：
- 304：If-None-Match 为 "*"；标签列表强/弱比较命中（W/ 前缀双向匹配）；
  多标签中任一命中；仅 If-Modified-Since 且当前修改时间早于/等于条件
  时刻；304 回传当前 ETag/Last-Modified 且不产生 plan，也不解析 Range；
- 未命中复用 /client-range-decision：标签不匹配时 206/416/200 决策不
  变；弱标签列表对强当前 ETag 按弱比较命中；If-None-Match 存在时
  If-Modified-Since 被完全忽略（含非法值不报 422）；IMS 晚于当前修改
  时间未命中；没有当前 Last-Modified 时 IMS 未命中；
- 206 的 plan 仍可提交 /assemble；
- 422：If-None-Match 空字符串/空白/裸文本/尾随逗号/空成员/残缺引号/
  控制字符；If-Modified-Since 非法日期（含错误星期）；etag/
  last_modified 当前验证器非法仍按原 loc 报错；字段错误结构一致；
- condition 评估明细字段（matched_by / kind / tags / evaluated）。

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
FUTURE = "Thu, 22 Oct 2015 07:28:00 GMT"
PAST = "Tue, 20 Oct 2015 07:28:00 GMT"


@unittest.skipIf(TestClient is None, "需要安装 starlette（随 fastapi 安装）")
class ConditionalRangeDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.obj = bytes((i % 256 for i in range(OBJECT_LENGTH)))

    # ----------------------------- 构造工具 -----------------------------

    def post(self, body: dict):
        return self.client.post("/conditional-range-decision", json=body)

    def req(self, **over) -> dict:
        body = {
            "object_length": OBJECT_LENGTH,
            "etag": STRONG_ETAG,
            "range_header": "bytes=0-99",
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

    # --------------------------- 304：If-None-Match "*" ---------------------------

    def test_star_matches_even_with_range(self) -> None:
        d = self.post(self.req(if_none_match="*")).json()
        self.assertEqual(d["status_code"], 304)
        self.assertEqual(d["reason"], "not_modified")
        self.assertIsNone(d["decision"])
        self.assertEqual(d["headers"], {"ETag": STRONG_ETAG})
        cond = d["condition"]
        self.assertTrue(cond["matched"])
        self.assertEqual(cond["matched_by"], "if_none_match")
        self.assertEqual(cond["if_none_match_kind"], "*")
        self.assertIsNone(cond["if_none_match_tags"])
        self.assertFalse(cond["if_modified_since_evaluated"])

    def test_star_ows_allowed(self) -> None:
        d = self.post(self.req(if_none_match="  *  ")).json()
        self.assertEqual(d["status_code"], 304)

    def test_star_ignores_range_and_if_range(self) -> None:
        # 即使 Range/If-Range 非法，条件命中也短路为 304（不解析 Range）。
        d = self.post(
            self.req(range_header="bytes=garbage", if_range="also-garbage",
                     if_none_match="*")
        ).json()
        self.assertEqual(d["status_code"], 304)
        self.assertIsNone(d["decision"])

    # ---------------------- 304：标签列表弱比较 ----------------------

    def test_strong_tag_exact_match(self) -> None:
        d = self.post(self.req(if_none_match=STRONG_ETAG)).json()
        self.assertEqual(d["status_code"], 304)
        cond = d["condition"]
        self.assertEqual(cond["matched_by"], "if_none_match")
        self.assertEqual(cond["if_none_match_kind"], "list")
        self.assertEqual(cond["if_none_match_tags"], [STRONG_ETAG])

    def test_weak_client_tag_matches_strong_current(self) -> None:
        # 弱比较：客户端 W/"v3-strong" 与服务端强 "v3-strong" 命中。
        d = self.post(self.req(if_none_match='W/"v3-strong"')).json()
        self.assertEqual(d["status_code"], 304)
        self.assertEqual(
            d["condition"]["if_none_match_tags"], ['W/"v3-strong"']
        )

    def test_lowercase_w_prefix_normalized_and_matches(self) -> None:
        d = self.post(self.req(if_none_match='w/"v3-strong"')).json()
        self.assertEqual(d["status_code"], 304)
        self.assertEqual(
            d["condition"]["if_none_match_tags"], ['W/"v3-strong"']
        )

    def test_any_tag_in_list_matches(self) -> None:
        d = self.post(
            self.req(if_none_match='"old", W/"v3-strong", "other"')
        ).json()
        self.assertEqual(d["status_code"], 304)
        self.assertEqual(
            d["condition"]["if_none_match_tags"],
            ['"old"', 'W/"v3-strong"', '"other"'],
        )

    def test_tag_list_ows_and_duplicate_commas_not_allowed(self) -> None:
        # 成员周围 OWS 允许；空成员（重复逗号）拒绝。
        d = self.post(self.req(if_none_match=' "old" , "v3-strong" ')).json()
        self.assertEqual(d["status_code"], 304)

    def test_etag_match_returns_both_validators(self) -> None:
        d = self.post(
            self.req(last_modified=LAST_MODIFIED, if_none_match=STRONG_ETAG)
        ).json()
        self.assertEqual(d["status_code"], 304)
        self.assertEqual(
            d["headers"],
            {"ETag": STRONG_ETAG, "Last-Modified": LAST_MODIFIED},
        )

    def test_star_without_current_etag_still_matches(self) -> None:
        d = self.post(
            self.req(etag=None, last_modified=LAST_MODIFIED, if_none_match="*")
        ).json()
        self.assertEqual(d["status_code"], 304)
        self.assertEqual(d["headers"], {"Last-Modified": LAST_MODIFIED})

    # ---------------------- 304：If-Modified-Since ----------------------

    def test_ims_equal_instant_is_not_modified(self) -> None:
        d = self.post(
            self.req(etag=None, last_modified=LAST_MODIFIED,
                     if_modified_since=LAST_MODIFIED)
        ).json()
        self.assertEqual(d["status_code"], 304)
        cond = d["condition"]
        self.assertEqual(cond["matched_by"], "if_modified_since")
        self.assertFalse(cond["if_none_match_present"])
        self.assertTrue(cond["if_modified_since_evaluated"])

    def test_ims_future_is_not_modified(self) -> None:
        d = self.post(
            self.req(etag=None, last_modified=LAST_MODIFIED,
                     if_modified_since=FUTURE)
        ).json()
        self.assertEqual(d["status_code"], 304)

    # --------------------- 未命中：复用范围决策 ---------------------

    def test_inm_tag_mismatch_falls_through_to_206(self) -> None:
        d = self.req(if_none_match='"stale-version"')
        out = self.post(d).json()
        self.assertEqual(out["status_code"], 206)
        self.assertEqual(out["reason"], "partial")
        self.assertFalse(out["condition"]["matched"])
        self.assertIsNone(out["condition"]["matched_by"])
        decision = out["decision"]
        self.assertIsNotNone(decision)
        self.assertEqual(decision["status_code"], 206)
        self.assertEqual(decision["media_type"], "single")
        self.assertIsNotNone(decision["plan"])

    def test_inm_mismatch_with_unsatisfiable_range_gives_416(self) -> None:
        out = self.post(
            self.req(range_header="bytes=1000-2000", if_none_match='"v2"')
        ).json()
        self.assertEqual(out["status_code"], 416)
        self.assertEqual(out["reason"], "unsatisfiable")
        self.assertEqual(out["headers"], {"Content-Range": "bytes */256"})
        self.assertIsNone(out["decision"]["plan"])

    def test_inm_mismatch_without_range_gives_200(self) -> None:
        body = self.req(range_header=None, if_none_match='"v2"')
        del body["range_header"]
        out = self.post(body).json()
        self.assertEqual(out["status_code"], 200)
        self.assertEqual(out["reason"], "range_absent")

    def test_inm_list_no_match_without_current_etag(self) -> None:
        # 只有 Last-Modified：标签列表无法比较，按未命中处理（继续 IMS 缺席
        # 的正常范围决策）。
        out = self.post(
            self.req(etag=None, last_modified=LAST_MODIFIED,
                     if_none_match='"v3-strong"')
        ).json()
        self.assertEqual(out["status_code"], 206)
        self.assertFalse(out["condition"]["matched"])

    def test_ims_past_means_modified_206(self) -> None:
        out = self.post(
            self.req(etag=None, last_modified=LAST_MODIFIED,
                     if_modified_since=PAST)
        ).json()
        self.assertEqual(out["status_code"], 206)
        self.assertFalse(out["condition"]["matched"])
        self.assertTrue(out["condition"]["if_modified_since_evaluated"])

    def test_ims_without_current_last_modified_never_matches(self) -> None:
        out = self.post(
            self.req(etag=STRONG_ETAG, if_modified_since=FUTURE)
        ).json()
        self.assertEqual(out["status_code"], 206)
        self.assertFalse(out["condition"]["matched"])

    def test_inm_present_ignores_ims_completely(self) -> None:
        # If-None-Match 未命中 + 一个语法上非法的 If-Modified-Since：
        # RFC 9110 规定 IMS 被忽略，不得返回 422。
        out = self.post(
            self.req(
                etag=STRONG_ETAG,
                if_none_match='"stale"',
                if_modified_since="not-a-date",
            )
        ).json()
        self.assertEqual(out["status_code"], 206)
        self.assertFalse(out["condition"]["if_modified_since_evaluated"])
        self.assertEqual(out["condition"]["matched_by"], None)

    def test_no_conditions_normal_decision(self) -> None:
        out = self.post(self.req()).json()
        self.assertEqual(out["status_code"], 206)
        cond = out["condition"]
        self.assertFalse(cond["condition_present"])
        self.assertFalse(cond["matched"])
        self.assertFalse(cond["if_none_match_present"])
        self.assertFalse(cond["if_modified_since_evaluated"])
        self.assertEqual(out["decision"]["status_code"], 206)

    # ---------------------- 206 plan 衔接 /assemble ----------------------

    def test_miss_plan_can_drive_assemble(self) -> None:
        out = self.post(
            self.req(range_header="bytes=0-99, 240-255",
                     if_none_match='"stale"')
        ).json()
        self.assertEqual(out["status_code"], 206)
        plan = out["decision"]["plan"]
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
            "cached_blocks": plan["cached_blocks"],
        })
        self.assertEqual(asm.status_code, 200, asm.text)
        ranges = asm.json()["ranges"]
        self.assertEqual(base64.b64decode(ranges[0]["data_b64"]), self.obj[0:100])
        self.assertEqual(base64.b64decode(ranges[1]["data_b64"]), self.obj[240:256])

    # ----------------------------- 422：If-None-Match -----------------------------

    def test_blank_if_none_match_is_422(self) -> None:
        # 空字符串/纯空格为空值；纯 tab 属于控制字符（malformed_header），
        # 与其他头值解析器的行为一致。
        for raw in ("", "   "):
            with self.subTest(raw=raw):
                self.err422(
                    self.post(self.req(if_none_match=raw)),
                    loc="if_none_match",
                    code="invalid_if_none_match",
                )
        self.err422(
            self.post(self.req(if_none_match="\t")),
            loc="if_none_match",
            code="malformed_header",
        )

    def test_malformed_if_none_match_list(self) -> None:
        for raw in (
            "v1",                    # 裸文本
            '"a",',                  # 尾随逗号
            '"a",, "b"',             # 空成员
            '"a" "b"',               # 缺逗号
            '"un closed"',           # 标签内含空格
            'W/"a',                  # 残缺引号
            '*, "a"',                # * 出现在列表中
            '"a", garbage',          # 尾随裸文本
        ):
            with self.subTest(raw=raw):
                self.err422(
                    self.post(self.req(if_none_match=raw)),
                    loc="if_none_match",
                )

    def test_if_none_match_control_chars_rejected(self) -> None:
        self.err422(
            self.post(self.req(if_none_match='"a"\r\nX: 1')),
            loc="if_none_match",
            code="malformed_header",
        )

    # --------------------------- 422：If-Modified-Since ---------------------------

    def test_invalid_if_modified_since(self) -> None:
        for raw in (
            "21 Oct 2015 07:28:00",
            "Tue, 21 Oct 2015 07:28:00 GMT",   # 星期与日期不一致
            "Wed, 32 Oct 2015 07:28:00 GMT",
        ):
            with self.subTest(raw=raw):
                self.err422(
                    self.post(
                        self.req(etag=None, last_modified=LAST_MODIFIED,
                                 if_modified_since=raw)
                    ),
                    loc="if_modified_since",
                )

    def test_blank_if_modified_since_is_422(self) -> None:
        self.err422(
            self.post(
                self.req(etag=None, last_modified=LAST_MODIFIED,
                         if_modified_since="  ")
            ),
            loc="if_modified_since",
        )

    # --------------------------- 422：其余字段沿用 ---------------------------

    def test_current_weak_etag_still_rejected(self) -> None:
        self.err422(
            self.post(self.req(etag='W/"x"', if_none_match="*")),
            loc="etag",
            code="weak_current_etag",
        )

    def test_invalid_last_modified_still_rejected(self) -> None:
        self.err422(
            self.post(
                self.req(etag=None, last_modified="bad-date",
                         if_modified_since=FUTURE)
            ),
            loc="last_modified",
        )

    def test_neither_validator_provided(self) -> None:
        body = {
            "object_length": OBJECT_LENGTH,
            "range_header": "bytes=0-9",
            "max_gap": 8,
            "fetch_limit": 128,
            "if_none_match": "*",
        }
        resp = self.post(body)
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_request")

    def test_missing_required_fields_422(self) -> None:
        resp = self.client.post("/conditional-range-decision", json={})
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_request")

    def test_condition_hit_skips_if_range_validation(self) -> None:
        # 命中短路：不评估 If-Range/Range 的合法性。
        out = self.post(
            self.req(if_none_match="*", if_range="nonsense",
                     range_header="also-nonsense")
        ).json()
        self.assertEqual(out["status_code"], 304)


if __name__ == "__main__":
    unittest.main()
