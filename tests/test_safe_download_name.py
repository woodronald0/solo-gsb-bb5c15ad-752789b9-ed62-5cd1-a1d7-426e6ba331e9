# -*- coding: utf-8 -*-
"""POST /safe-download-name 的行为测试。

覆盖：
- RFC 5987 filename* 解码（UTF-8 / ISO-8859-1、百分号编码、language-tag、
  优先级）；
- filename* 无效或缺失时回退到 filename（token / quoted-string /
  quoted-pair / 原始 UTF-8 与 ISO-8859-1 字节）；
- 路径前缀剥离（POSIX、Windows、混合分隔符）、控制字符清除、尾部点/空格
  规范化与空名 / "." / ".." 等危险名称拒绝；
- 头部结构错误（折行、未闭合引号、参数名非法、非法 token 等）与字段
  校验缺失均返回 HTTP 422。

直接运行：python -m unittest discover -s tests
（仅依赖标准库 unittest；HTTP 层通过 starlette TestClient 发起真实 ASGI 请求。）
"""

from __future__ import annotations

import json
import unittest

from app.main import app

try:
    from starlette.testclient import TestClient
except ImportError:  # pragma: no cover - 测试依赖缺失时给出明确提示
    TestClient = None  # type: ignore[assignment]


@unittest.skipIf(TestClient is None, "需要安装 starlette（随 fastapi 安装）")
class SafeDownloadNameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def post(self, header: str):
        return self.client.post(
            "/safe-download-name", json={"content_disposition": header}
        )

    def assert_name(self, header: str, filename: str, source: str) -> None:
        resp = self.post(header)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json(), {"filename": filename, "source": source})

    def assert_422(self, header: str) -> None:
        resp = self.post(header)
        self.assertEqual(resp.status_code, 422, (header, resp.text))

    # ------------------------- RFC 5987 filename* -------------------------

    def test_extended_utf8_percent_decoding(self) -> None:
        self.assert_name(
            "attachment; filename*=utf-8''%E4%B8%AD%E6%96%87.tar.gz",
            "中文.tar.gz",
            "filename*",
        )

    def test_extended_iso88591_decoding(self) -> None:
        self.assert_name(
            "attachment; filename*=iso-8859-1''caf%E9.txt",
            "café.txt",
            "filename*",
        )

    def test_extended_charset_case_insensitive(self) -> None:
        self.assert_name(
            "attachment; filename*=UTF-8'en'%E2%82%AC%20rates",
            "€ rates",
            "filename*",
        )

    def test_extended_with_language_tag(self) -> None:
        self.assert_name(
            "attachment; filename*=utf-8'zh-CN'%E4%B8%AD.txt",
            "中.txt",
            "filename*",
        )

    def test_extended_ascii_no_encoding_needed(self) -> None:
        self.assert_name(
            "attachment; filename*=utf-8''report-2026.bin",
            "report-2026.bin",
            "filename*",
        )

    def test_extended_takes_precedence_over_filename(self) -> None:
        # 无论 filename* 出现在 filename 之前还是之后，都优先采用。
        self.assert_name(
            "attachment; filename=old.txt; filename*=utf-8''new.txt",
            "new.txt",
            "filename*",
        )
        self.assert_name(
            "attachment; filename*=utf-8''new.txt; filename=old.txt",
            "new.txt",
            "filename*",
        )

    def test_extended_chinese_real_world_value(self) -> None:
        self.assert_name(
            "attachment; filename*=utf-8''%E9%A6%96%E9%80%89.bin",
            "首选.bin",
            "filename*",
        )

    # ------------------------- 回退到 filename -------------------------

    def test_fallback_quoted_string(self) -> None:
        self.assert_name(
            'attachment; filename="plain.zip"', "plain.zip", "filename"
        )

    def test_fallback_token_value(self) -> None:
        self.assert_name(
            "attachment; filename=token-name.dat", "token-name.dat", "filename"
        )

    def test_fallback_when_filename_star_missing(self) -> None:
        self.assert_name(
            'attachment; size=42; filename="only-one.bin"',
            "only-one.bin",
            "filename",
        )

    def test_fallback_invalid_percent_sequence(self) -> None:
        self.assert_name(
            "attachment; filename=ok.dat; filename*=utf-8''%zzbad",
            "ok.dat",
            "filename",
        )

    def test_fallback_truncated_percent_sequence(self) -> None:
        self.assert_name(
            "attachment; filename=ok.dat; filename*=utf-8''a%E4",
            "ok.dat",
            "filename",
        )

    def test_fallback_unsupported_charset(self) -> None:
        self.assert_name(
            "attachment; filename=ok.dat; filename*=utf-16''x",
            "ok.dat",
            "filename",
        )

    def test_fallback_bad_language_tag(self) -> None:
        self.assert_name(
            "attachment; filename=ok.dat; filename*=utf-8'!!!'x",
            "ok.dat",
            "filename",
        )

    def test_fallback_invalid_utf8_byte_sequence(self) -> None:
        self.assert_name(
            "attachment; filename=ok.dat; filename*=utf-8''%ff%ff%ff",
            "ok.dat",
            "filename",
        )

    def test_fallback_extended_cleans_to_empty(self) -> None:
        # filename* 合法但清理后为空（仅编码出 ".."），回退到 filename。
        self.assert_name(
            "attachment; filename=ok.dat; filename*=utf-8''%2E%2E%2F",
            "ok.dat",
            "filename",
        )

    def test_fallback_wrong_quote_count(self) -> None:
        self.assert_name(
            "attachment; filename=ok.dat; filename*=utf-8'only-one-quote",
            "ok.dat",
            "filename",
        )

    def test_legacy_quoted_pair_escape(self) -> None:
        self.assert_name('attachment; filename="quo\\"te.txt"', 'quo"te.txt', "filename")

    def test_legacy_raw_utf8_bytes_in_quoted_string(self) -> None:
        # 上游未用 filename*，直接把 UTF-8 字节放进 quoted-string。
        self.assert_name('attachment; filename="报价单.pdf"', "报价单.pdf", "filename")

    def test_legacy_iso88591_bytes_in_quoted_string(self) -> None:
        self.assert_name('attachment; filename="café.txt"', "café.txt", "filename")

    def test_first_filename_wins_when_duplicated(self) -> None:
        self.assert_name(
            "attachment; filename=first.txt; filename=second.txt",
            "first.txt",
            "filename",
        )

    def test_first_extended_wins_when_duplicated(self) -> None:
        self.assert_name(
            "attachment; filename*=utf-8''first.bin; filename*=utf-8''second.bin",
            "first.bin",
            "filename*",
        )

    def test_invalid_first_extended_does_not_use_later_extended(self) -> None:
        # 首个 filename* 虽非法，但会“占位”；按 RFC 重复参数语义不再采用
        # 后一个 filename*，而是回退到 filename。
        self.assert_name(
            "attachment; filename=fb.txt; "
            "filename*=utf-16''bad; filename*=utf-8''good.bin",
            "fb.txt",
            "filename",
        )

    def test_parameter_names_case_insensitive(self) -> None:
        self.assert_name('attachment; FILENAME="x.txt"', "x.txt", "filename")
        self.assert_name(
            "attachment; FILENAME*=utf-8''y.txt", "y.txt", "filename*"
        )

    def test_disposition_type_case_insensitive(self) -> None:
        self.assert_name('INLINE; filename="x.bin"', "x.bin", "filename")

    def test_extension_parameters_are_tolerated(self) -> None:
        self.assert_name(
            'attachment; size=42; filename="x.bin"; creation-date="Wed, 2026"',
            "x.bin",
            "filename",
        )

    # ------------------------- 路径清理 -------------------------

    def test_strip_unix_path_prefix(self) -> None:
        self.assert_name('attachment; filename="../../etc/passwd"', "passwd", "filename")
        self.assert_name('attachment; filename="/var/log/syslog"', "syslog", "filename")

    def test_strip_windows_path_prefix(self) -> None:
        # RFC quoted-string 中的反斜杠必须成对转义（"\\" -> 字面 \）。
        self.assert_name(
            'attachment; filename="C:\\\\Windows\\\\System32\\\\drivers\\\\etc\\\\hosts"',
            "hosts",
            "filename",
        )

    def test_strip_mixed_separators(self) -> None:
        self.assert_name(
            'attachment; filename="a/b\\\\c/d.txt"', "d.txt", "filename"
        )

    def test_extended_encoded_path_prefix(self) -> None:
        # filename* 中百分号编码出的 "../" 同样必须被剥离。
        self.assert_name(
            "attachment; filename*=utf-8''..%2F..%2Fevil.sh",
            "evil.sh",
            "filename*",
        )

    def test_control_chars_removed_from_filename(self) -> None:
        resp = self.client.post(
            "/safe-download-name",
            content=json.dumps(
                {"content_disposition": 'attachment; filename="a\x00b\x07.txt"'}
            ),
            headers={"content-type": "application/json"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["filename"], "ab.txt")

    def test_del_char_removed(self) -> None:
        resp = self.client.post(
            "/safe-download-name",
            content=json.dumps(
                {"content_disposition": 'attachment; filename="a\x7fb.txt"'}
            ),
            headers={"content-type": "application/json"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["filename"], "ab.txt")

    def test_extended_percent_encoded_control_chars_removed(self) -> None:
        self.assert_name(
            "attachment; filename*=utf-8''a%00b%01.txt",
            "ab.txt",
            "filename*",
        )

    def test_trailing_dots_and_spaces_trimmed(self) -> None:
        self.assert_name(
            'attachment; filename="report.pdf...   "', "report.pdf", "filename"
        )
        self.assert_name(
            'attachment; filename="a   ...   "', "a", "filename"
        )

    def test_leading_dot_filename_is_kept(self) -> None:
        # ".bashrc" 是合法的隐藏文件名，不应被误伤。
        self.assert_name('attachment; filename=".bashrc"', ".bashrc", "filename")

    def test_result_is_single_filename_without_separator(self) -> None:
        resp = self.post('attachment; filename="a/b\\\\c.txt"')
        self.assertEqual(resp.status_code, 200, resp.text)
        name = resp.json()["filename"]
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)
        self.assertEqual(name, "c.txt")

    # ------------------------- 422：危险空名 -------------------------

    def test_422_empty_quoted_filename(self) -> None:
        resp = self.post('attachment; filename=""')
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "no_safe_filename")

    def test_422_only_whitespace(self) -> None:
        self.assert_422('attachment; filename="   "')

    def test_422_dot_names(self) -> None:
        for value in (".", "..", "..."):
            self.assert_422(f'attachment; filename="{value}"')

    def test_422_path_only_segments(self) -> None:
        self.assert_422('attachment; filename="/"')
        self.assert_422('attachment; filename="../../"')
        # quoted-string 中字面反斜杠需以 "\\" 表示："\\" 即单个 \。
        self.assert_422('attachment; filename="\\\\"')

    def test_422_no_filename_parameter(self) -> None:
        resp = self.post("attachment")
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "no_safe_filename")
        resp = self.post("attachment; size=42")
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "no_safe_filename")

    def test_422_extended_cleans_to_dotdot_without_fallback(self) -> None:
        resp = self.post("attachment; filename*=utf-8''%2E%2E")
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "no_safe_filename")

    # ------------------------- 422：头值结构错误 -------------------------

    def test_422_crlf_in_header(self) -> None:
        self.assert_422('attachment; filename="x.txt"\r\nX-Injected: 1')
        self.assert_422('attachment; filename="x.txt"\n')

    def test_422_unterminated_quoted_string(self) -> None:
        self.assert_422('attachment; filename="abc.txt')

    def test_422_bad_quoted_pair(self) -> None:
        # quoted-pair 只允许转义可见 ASCII。
        self.assert_422('attachment; filename="a\\\x01b"')

    def test_422_missing_disposition_type(self) -> None:
        self.assert_422('; filename="x.txt"')
        self.assert_422('filename="x.txt"')

    def test_422_parameter_without_equals(self) -> None:
        self.assert_422("attachment; badname")

    def test_422_parameter_with_empty_name(self) -> None:
        self.assert_422("attachment; =novaluename")

    def test_422_invalid_token_value(self) -> None:
        self.assert_422("attachment; filename=a b")

    def test_422_trailing_semicolon(self) -> None:
        self.assert_422("attachment;")

    def test_422_bad_separator_between_params(self) -> None:
        self.assert_422('attachment filename="x.txt"')

    def test_422_filename_star_empty_value(self) -> None:
        self.assert_422("attachment; filename*=")

    # ------------------------- 422：请求字段校验 -------------------------

    def test_422_missing_field(self) -> None:
        resp = self.client.post("/safe-download-name", json={})
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_request")

    def test_422_empty_field(self) -> None:
        resp = self.client.post(
            "/safe-download-name", json={"content_disposition": ""}
        )
        self.assertEqual(resp.status_code, 422)

    def test_422_wrong_field_type(self) -> None:
        resp = self.client.post(
            "/safe-download-name", json={"content_disposition": 123}
        )
        self.assertEqual(resp.status_code, 422)

    def test_422_value_too_long(self) -> None:
        resp = self.client.post(
            "/safe-download-name", json={"content_disposition": "a" * 9000}
        )
        self.assertEqual(resp.status_code, 422)


if __name__ == "__main__":
    unittest.main()
