# -*- coding: utf-8 -*-
"""POST /cache-write-decision 的行为测试。

覆盖：
- 可缓存：max-age / s-maxage（含引号取值、大小写不敏感指令名）、s-maxage
  优先于 max-age、Age 扣减、剩余 1 秒边界、no-cache 不阻止写入、未知
  指令与空列表成员被忽略、"=" 周围 BWS 容忍；
- 拒绝：no-store（含带取值形式）、private（含限定字段形式）、Vary: *
  （含混入其他成员）、Authorization 且响应未显式允许共享、缺少
  s-maxage/max-age、扣除 Age 后过期；
- Authorization 豁免：public / must-revalidate / s-maxage 任一存在即可，
  字段名大小写不影响判定；
- 缓存键：包含 URI/区间/ETag/Vary 指定的请求头值；字段名大小写、Vary
  成员大小写与顺序、ETag 首尾 OWS 不影响结果；缺失的 Vary 头以 null
  参与；任一成分变化都改变键；键与文档承诺的规范化构造逐字节一致；
- 422：etag 缺失/弱标签/非法、Cache-Control 语法错误（未闭合引号、
  非法 token、max-age 非数字/缺取值、指令重复）、Age 非法、Vary 非法、
  请求头名/值非法、URI 空白/含控制字符、end < start、缺字段；
- 错误结构：统一 422、detail[0] 携带可定位 loc。

直接运行：python -m unittest discover -s tests
"""

from __future__ import annotations

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


@unittest.skipIf(TestClient is None, "需要安装 starlette（随 fastapi 安装）")
class CacheWriteDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    # ----------------------------- 构造工具 -----------------------------

    def req(self, **over) -> dict:
        body = {
            "uri": URI,
            "start": 0,
            "end": 99,
            "etag": ETAG,
            "request_headers": {},
            "cache_control": "max-age=300",
        }
        body.update(over)
        return body

    def post(self, body: dict):
        return self.client.post("/cache-write-decision", json=body)

    def ok(self, resp) -> dict:
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def cacheable(self, resp) -> dict:
        d = self.ok(resp)
        self.assertTrue(d["cacheable"], d)
        self.assertEqual(d["reason"], "cacheable")
        self.assertIsInstance(d["remaining_seconds"], int)
        self.assertGreaterEqual(d["remaining_seconds"], 1)
        self.assertIsInstance(d["cache_key"], str)
        self.assertEqual(len(d["cache_key"]), 64)
        int(d["cache_key"], 16)  # 十六进制
        return d

    def rejected(self, resp, reason: str) -> dict:
        d = self.ok(resp)
        self.assertFalse(d["cacheable"], d)
        self.assertEqual(d["reason"], reason)
        self.assertIsNone(d["remaining_seconds"])
        self.assertIsNone(d["cache_key"])
        return d

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
        # pydantic 字段级校验失败的统一结构（loc 为路径列表）。
        self.assertEqual(resp.status_code, 422, resp.text)
        payload = resp.json()
        self.assertEqual(payload["error"], "invalid_request", payload)
        self.assertIsInstance(payload["detail"], list)
        self.assertTrue(payload["detail"])
        return payload

    # --------------------------- 可缓存路径 ---------------------------

    def test_cacheable_with_max_age(self) -> None:
        d = self.cacheable(self.post(self.req()))
        self.assertEqual(d["remaining_seconds"], 300)
        self.assertEqual(d["vary_headers"], [])

    def test_age_deducted(self) -> None:
        d = self.cacheable(self.post(self.req(age="120")))
        self.assertEqual(d["remaining_seconds"], 180)

    def test_age_absent_defaults_zero(self) -> None:
        d = self.cacheable(self.post(self.req(age=None)))
        self.assertEqual(d["remaining_seconds"], 300)

    def test_s_maxage_preferred_over_max_age(self) -> None:
        d = self.cacheable(
            self.post(self.req(cache_control="max-age=300, s-maxage=60"))
        )
        self.assertEqual(d["remaining_seconds"], 60)

    def test_s_maxage_alone_provides_freshness(self) -> None:
        d = self.cacheable(self.post(self.req(cache_control="s-maxage=120")))
        self.assertEqual(d["remaining_seconds"], 120)

    def test_quoted_delta_seconds(self) -> None:
        d = self.cacheable(self.post(self.req(cache_control='max-age="300"')))
        self.assertEqual(d["remaining_seconds"], 300)

    def test_quoted_comma_does_not_split_directives(self) -> None:
        # 引号内的逗号不分隔指令：private="Set-Cookie, X-Other" 是一条指令。
        self.rejected(
            self.post(
                self.req(cache_control='private="Set-Cookie, X-Other", max-age=60')
            ),
            "private",
        )

    def test_quoted_pair_escape(self) -> None:
        # quoted-pair 转义在未知扩展指令中合法存在且被忽略。
        d = self.cacheable(
            self.post(self.req(cache_control='max-age=60, ext="a\\"b"'))
        )
        self.assertEqual(d["remaining_seconds"], 60)

    def test_directive_names_case_insensitive(self) -> None:
        d = self.cacheable(self.post(self.req(cache_control="MAX-AGE=300")))
        self.assertEqual(d["remaining_seconds"], 300)
        self.rejected(self.post(self.req(cache_control="No-Store")), "no_store")
        self.rejected(self.post(self.req(cache_control="PRIVATE")), "private")

    def test_ows_and_bws_tolerated(self) -> None:
        d = self.cacheable(
            self.post(self.req(cache_control="  max-age = 300 ,  public "))
        )
        self.assertEqual(d["remaining_seconds"], 300)

    def test_empty_list_members_ignored(self) -> None:
        d = self.cacheable(
            self.post(self.req(cache_control=",max-age=300,, public,"))
        )
        self.assertEqual(d["remaining_seconds"], 300)

    def test_unknown_directives_ignored(self) -> None:
        d = self.cacheable(
            self.post(
                self.req(cache_control='max-age=300, foo=bar, baz, ext="a,b"')
            )
        )
        self.assertEqual(d["remaining_seconds"], 300)

    def test_no_cache_does_not_block_write(self) -> None:
        # no-cache 只要求使用时再验证，不禁止写入共享缓存。
        d = self.cacheable(
            self.post(self.req(cache_control="no-cache, max-age=300"))
        )
        self.assertEqual(d["remaining_seconds"], 300)

    def test_remaining_one_second_still_cacheable(self) -> None:
        d = self.cacheable(
            self.post(self.req(cache_control="max-age=101", age="100"))
        )
        self.assertEqual(d["remaining_seconds"], 1)

    # ----------------------------- 拒绝路径 -----------------------------

    def test_no_store_rejected(self) -> None:
        self.rejected(self.post(self.req(cache_control="no-store")), "no_store")
        # 带取值的形式同样拒绝。
        self.rejected(
            self.post(self.req(cache_control='no-store="x", max-age=300')),
            "no_store",
        )

    def test_private_rejected(self) -> None:
        self.rejected(self.post(self.req(cache_control="private")), "private")
        # 限定字段的 private 对共享缓存同样拒绝。
        self.rejected(
            self.post(self.req(cache_control='private="Set-Cookie", max-age=300')),
            "private",
        )

    def test_no_store_checked_before_other_reasons(self) -> None:
        # 同时命中多条拒绝规则时按 no-store -> private -> vary_star ->
        # authorization 的顺序报告。
        d = self.rejected(
            self.post(
                self.req(
                    cache_control="no-store, private",
                    vary="*",
                    request_headers={"Authorization": "Bearer t"},
                )
            ),
            "no_store",
        )
        self.assertEqual(d["vary_headers"], [])

    def test_vary_star_rejected(self) -> None:
        d = self.rejected(self.post(self.req(vary="*")), "vary_star")
        self.assertEqual(d["vary_headers"], [])
        # 混入其他成员时 * 仍然生效。
        self.rejected(self.post(self.req(vary="Accept-Encoding, *")), "vary_star")
        self.rejected(self.post(self.req(vary="  *  ")), "vary_star")

    def test_authorization_without_allowance_rejected(self) -> None:
        d = self.rejected(
            self.post(
                self.req(request_headers={"Authorization": "Bearer token"})
            ),
            "authorization_not_shareable",
        )
        self.assertEqual(d["vary_headers"], [])

    def test_authorization_header_name_case_insensitive(self) -> None:
        self.rejected(
            self.post(self.req(request_headers={"AUTHORIZATION": "Bearer t"})),
            "authorization_not_shareable",
        )
        self.rejected(
            self.post(self.req(request_headers={"aUtHoRiZaTiOn": "Basic x"})),
            "authorization_not_shareable",
        )

    def test_authorization_allowed_by_public(self) -> None:
        d = self.cacheable(
            self.post(
                self.req(
                    cache_control="max-age=300, public",
                    request_headers={"Authorization": "Bearer t"},
                )
            )
        )
        self.assertEqual(d["remaining_seconds"], 300)

    def test_authorization_allowed_by_must_revalidate(self) -> None:
        self.cacheable(
            self.post(
                self.req(
                    cache_control="max-age=300, must-revalidate",
                    request_headers={"Authorization": "Bearer t"},
                )
            )
        )

    def test_authorization_allowed_by_s_maxage(self) -> None:
        d = self.cacheable(
            self.post(
                self.req(
                    cache_control="s-maxage=100",
                    request_headers={"Authorization": "Bearer t"},
                )
            )
        )
        self.assertEqual(d["remaining_seconds"], 100)

    def test_no_explicit_freshness_rejected(self) -> None:
        self.rejected(
            self.post(self.req(cache_control="public")), "no_explicit_freshness"
        )
        self.rejected(
            self.post(self.req(cache_control=None)), "no_explicit_freshness"
        )

    def test_expired_rejected(self) -> None:
        self.rejected(
            self.post(self.req(cache_control="max-age=100", age="100")),
            "expired",
        )
        self.rejected(
            self.post(self.req(cache_control="max-age=100", age="150")),
            "expired",
        )
        # max-age=0 立即过期。
        self.rejected(
            self.post(self.req(cache_control="max-age=0")), "expired"
        )

    # ----------------------------- 缓存键 -----------------------------

    def test_cache_key_matches_documented_construction(self) -> None:
        body = self.req(
            vary="Accept-Encoding",
            request_headers={"Accept-Encoding": "gzip"},
        )
        d = self.cacheable(self.post(body))
        material = {
            "uri": URI,
            "start": 0,
            "end": 99,
            "etag": ETAG,
            "vary": [["accept-encoding", "gzip"]],
        }
        expected = hashlib.sha256(
            json.dumps(
                material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(d["cache_key"], expected)

    def test_cache_key_stable_across_calls(self) -> None:
        body = self.req(vary="Accept", request_headers={"Accept": "text/html"})
        d1 = self.cacheable(self.post(body))
        d2 = self.cacheable(self.post(body))
        self.assertEqual(d1["cache_key"], d2["cache_key"])

    def test_cache_key_ignores_header_name_case(self) -> None:
        body1 = self.req(
            vary="Accept-Encoding", request_headers={"Accept-Encoding": "gzip"}
        )
        body2 = self.req(
            vary="ACCEPT-ENCODING", request_headers={"accept-encoding": "gzip"}
        )
        body3 = self.req(
            vary="accept-encoding", request_headers={"ACCEPT-ENCODING": "gzip"}
        )
        keys = {
            self.cacheable(self.post(b))["cache_key"] for b in (body1, body2, body3)
        }
        self.assertEqual(len(keys), 1)

    def test_cache_key_ignores_vary_member_order_and_case(self) -> None:
        headers = {"Accept": "text/html", "Accept-Encoding": "gzip"}
        body1 = self.req(
            vary="Accept-Encoding, Accept", request_headers=headers
        )
        body2 = self.req(
            vary="accept, ACCEPT-ENCODING", request_headers=headers
        )
        d1 = self.cacheable(self.post(body1))
        d2 = self.cacheable(self.post(body2))
        self.assertEqual(d1["cache_key"], d2["cache_key"])
        self.assertEqual(d1["vary_headers"], ["accept", "accept-encoding"])

    def test_cache_key_ignores_etag_ows(self) -> None:
        body1 = self.req(etag=ETAG)
        body2 = self.req(etag=f"  {ETAG}\t")
        d1 = self.cacheable(self.post(body1))
        d2 = self.cacheable(self.post(body2))
        self.assertEqual(d1["cache_key"], d2["cache_key"])

    def test_duplicate_header_names_combined_case_insensitively(self) -> None:
        # 大小写不同的同名头按出现顺序以 ", " 合并，与直接写合并值等价。
        body1 = self.req(
            vary="Accept",
            request_headers={"Accept": "text/html", "accept": "application/json"},
        )
        body2 = self.req(
            vary="Accept",
            request_headers={"accept": "text/html, application/json"},
        )
        d1 = self.cacheable(self.post(body1))
        d2 = self.cacheable(self.post(body2))
        self.assertEqual(d1["cache_key"], d2["cache_key"])

    def test_cache_key_changes_with_vary_value(self) -> None:
        body1 = self.req(
            vary="Accept-Encoding", request_headers={"Accept-Encoding": "gzip"}
        )
        body2 = self.req(
            vary="Accept-Encoding", request_headers={"Accept-Encoding": "br"}
        )
        k1 = self.cacheable(self.post(body1))["cache_key"]
        k2 = self.cacheable(self.post(body2))["cache_key"]
        self.assertNotEqual(k1, k2)

    def test_cache_key_missing_vary_header_is_null(self) -> None:
        # Vary 指定的头在请求中缺失时以 null 参与；与“存在但为空值”不同键。
        body_missing = self.req(vary="Accept-Encoding", request_headers={})
        body_empty = self.req(
            vary="Accept-Encoding", request_headers={"Accept-Encoding": ""}
        )
        d_missing = self.cacheable(self.post(body_missing))
        d_empty = self.cacheable(self.post(body_empty))
        self.assertNotEqual(d_missing["cache_key"], d_empty["cache_key"])
        # 两次都缺失时键一致。
        self.assertEqual(
            d_missing["cache_key"],
            self.cacheable(self.post(body_missing))["cache_key"],
        )

    def test_cache_key_changes_with_uri_range_etag(self) -> None:
        base = self.req()
        variants = [
            self.req(uri="https://objects.example.com/bucket/other.bin"),
            self.req(start=100, end=199),
            self.req(end=100),
            self.req(etag='"v4-other"'),
        ]
        base_key = self.cacheable(self.post(base))["cache_key"]
        for body in variants:
            self.assertNotEqual(
                base_key, self.cacheable(self.post(body))["cache_key"], body
            )

    def test_vary_headers_echoed_lowercase_sorted(self) -> None:
        d = self.cacheable(
            self.post(self.req(vary="Accept-Encoding, Accept, accept-encoding"))
        )
        self.assertEqual(d["vary_headers"], ["accept", "accept-encoding"])

    def test_unlisted_request_headers_do_not_affect_key(self) -> None:
        body1 = self.req(request_headers={"User-Agent": "a"})
        body2 = self.req(request_headers={"User-Agent": "b", "X-Trace": "t"})
        k1 = self.cacheable(self.post(body1))["cache_key"]
        k2 = self.cacheable(self.post(body2))["cache_key"]
        self.assertEqual(k1, k2)

    # ----------------------------- 422：ETag -----------------------------

    def test_weak_etag_422(self) -> None:
        self.err(self.post(self.req(etag='W/"v3-strong"')), "weak_etag", "etag")
        self.err(self.post(self.req(etag='w/"v3-strong"')), "weak_etag", "etag")

    def test_invalid_etag_422(self) -> None:
        self.err(self.post(self.req(etag="v3-strong")), "invalid_etag", "etag")
        self.err(self.post(self.req(etag='"unclosed')), "invalid_etag", "etag")

    def test_blank_etag_422(self) -> None:
        self.err(self.post(self.req(etag="   ")), "missing_etag", "etag")

    # ----------------------- 422：Cache-Control -----------------------

    def test_cache_control_unclosed_quote_422(self) -> None:
        self.err(
            self.post(self.req(cache_control='private="abc')),
            "invalid_cache_control",
            "cache_control",
        )

    def test_cache_control_bad_token_422(self) -> None:
        self.err(
            self.post(self.req(cache_control="max-age=6 0")),
            "invalid_cache_control",
            "cache_control",
        )
        self.err(
            self.post(self.req(cache_control="max age=60")),
            "invalid_cache_control",
            "cache_control",
        )

    def test_cache_control_max_age_value_invalid_422(self) -> None:
        for bad in ("max-age=abc", "max-age=-5", "max-age=1.5", "max-age=",
                    "max-age", 'max-age="6 0"', "max-age=60x"):
            self.err(
                self.post(self.req(cache_control=bad)),
                "invalid_cache_control",
                "cache_control",
            )

    def test_cache_control_s_maxage_value_invalid_422(self) -> None:
        self.err(
            self.post(self.req(cache_control="s-maxage=soon")),
            "invalid_cache_control",
            "cache_control",
        )

    def test_cache_control_duplicate_freshness_422(self) -> None:
        self.err(
            self.post(self.req(cache_control="max-age=60, max-age=70")),
            "duplicate_cache_directive",
            "cache_control",
        )
        self.err(
            self.post(self.req(cache_control="s-maxage=60, S-MAXAGE=70")),
            "duplicate_cache_directive",
            "cache_control",
        )
        # max-age 与 s-maxage 同时出现不算重复（s-maxage 优先）。
        self.cacheable(
            self.post(self.req(cache_control="max-age=60, s-maxage=70"))
        )

    def test_cache_control_blank_422(self) -> None:
        self.err(
            self.post(self.req(cache_control="")),
            "invalid_cache_control",
            "cache_control",
        )
        self.err(
            self.post(self.req(cache_control="   ")),
            "invalid_cache_control",
            "cache_control",
        )
        self.err(
            self.post(self.req(cache_control=" , ,")),
            "invalid_cache_control",
            "cache_control",
        )

    def test_cache_control_crlf_422(self) -> None:
        self.err(
            self.post(self.req(cache_control="max-age=60\r\nno-store")),
            "malformed_header",
            "cache_control",
        )

    # ----------------------------- 422：Age -----------------------------

    def test_age_invalid_422(self) -> None:
        for bad in ("abc", "-5", "1.5", '"60"', "+60", "6 0"):
            self.err(self.post(self.req(age=bad)), "invalid_age", "age")

    def test_age_blank_422(self) -> None:
        self.err(self.post(self.req(age="  ")), "invalid_age", "age")

    def test_age_ows_allowed(self) -> None:
        d = self.cacheable(self.post(self.req(age=" 100 ")))
        self.assertEqual(d["remaining_seconds"], 200)

    # ----------------------------- 422：Vary -----------------------------

    def test_vary_invalid_422(self) -> None:
        self.err(self.post(self.req(vary="  ")), "invalid_vary", "vary")
        self.err(self.post(self.req(vary="Accept Language")), "invalid_vary", "vary")
        self.err(self.post(self.req(vary='"Accept"')), "invalid_vary", "vary")
        self.err(self.post(self.req(vary=",,")), "invalid_vary", "vary")

    def test_vary_crlf_422(self) -> None:
        self.err(
            self.post(self.req(vary="Accept\r\nX-Evil: 1")),
            "malformed_header",
            "vary",
        )

    # -------------------------- 422：请求头/URI --------------------------

    def test_request_header_name_invalid_422(self) -> None:
        self.err(
            self.post(self.req(request_headers={"Accept Language": "x"})),
            "invalid_request_header",
            "request_headers.Accept Language",
        )
        self.err(
            self.post(self.req(request_headers={"": "x"})),
            "invalid_request_header",
            "request_headers.",
        )

    def test_request_header_value_control_chars_422(self) -> None:
        self.err(
            self.post(self.req(request_headers={"Accept": "a\rb"})),
            "malformed_header",
            "request_headers.Accept",
        )

    def test_uri_invalid_422(self) -> None:
        self.err(self.post(self.req(uri="   ")), "invalid_uri", "uri")
        self.err(
            self.post(self.req(uri="https://x.example/\ninject")),
            "invalid_uri",
            "uri",
        )

    # ------------------------ 422：字段级校验 ------------------------

    def test_end_before_start_422(self) -> None:
        self.err_invalid_request(self.post(self.req(start=10, end=5)))

    def test_missing_required_fields_422(self) -> None:
        self.err_invalid_request(self.post({}))
        self.err_invalid_request(self.post({"uri": URI}))

    def test_negative_offsets_422(self) -> None:
        self.err_invalid_request(self.post(self.req(start=-1)))
        self.err_invalid_request(self.post(self.req(end=-1)))

    def test_wrong_types_422(self) -> None:
        self.err_invalid_request(self.post(self.req(start=1.5)))
        self.err_invalid_request(self.post(self.req(age=100)))
        self.err_invalid_request(
            self.post(self.req(request_headers={"Accept": 1}))
        )
        self.err_invalid_request(self.post(self.req(request_headers=["Accept"])))

    # --------------------- 完整链路（与 verify 配合） ---------------------

    def test_typical_pipeline_after_verify(self) -> None:
        # 模拟 /verify-fetch-batch 验收后的逐块判定：键随块区间变化。
        keys = set()
        for start, end in ((0, 39), (60, 99), (240, 255)):
            d = self.cacheable(
                self.post(
                    self.req(
                        start=start,
                        end=end,
                        cache_control="s-maxage=120, max-age=60, public",
                        age="20",
                        vary="Accept-Encoding",
                        request_headers={"Accept-Encoding": "gzip"},
                    )
                )
            )
            self.assertEqual(d["remaining_seconds"], 100)
            keys.add(d["cache_key"])
        self.assertEqual(len(keys), 3)


if __name__ == "__main__":
    unittest.main()
