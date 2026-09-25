# -*- coding: utf-8 -*-
"""POST /secure-request 的鉴权与 nonce 防重放行为测试。

覆盖：
- 成功路径与原签名/时间窗/业务响应语义不变；
- 重放返回 409 nonce_replayed，且在防重放表已满时 409 仍优先于 503；
- 容量对进行中占位与窗口内已成功 nonce 统一计数；容量已满且无过期项时
  返回 503 nonce_capacity_exceeded（附 capacity），不驱逐未过期 nonce、
  不执行 payload；过期项在同一把锁内沿到期索引清理后可自动腾出名额；
- 到期索引只弹出已到期条目，不扫描未过期 nonce；索引中失败回滚/同名
  重占遗留的旧条目凭占位令牌识别并跳过，绝不误删新占位；
- payload/业务失败（422）释放请求自身占位，可用同一 nonce 重试；
- 释放占位时若该记录已过期并被并发请求重新占用，不能误删新记录；
- SECURE_REQUEST_NONCE_CAPACITY 默认 10000 且必须为正整数。

直接运行：python -m unittest discover -s tests
"""

from __future__ import annotations

import hashlib
import heapq
import hmac
import json
import os
import threading
import time
import unittest
import uuid

from app import main
from app.main import (
    SecureRequestConfig,
    _nonce_index,
    _nonce_lock,
    _nonce_seen,
    _release_nonce,
    _reserve_nonce,
    _sweep_nonces,
    app,
)

try:
    from starlette.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None  # type: ignore[assignment]

KEY_ID = "test-key"
SECRET = b"0123456789abcdef0123456789abcdef"


def canonical_json(obj: object) -> bytes:
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


PLAN_PAYLOAD = {
    "object_length": 256,
    "ranges": [{"type": "interval", "start": 0, "end": 99}],
    "max_gap": 8,
    "fetch_limit": 128,
}


@unittest.skipIf(TestClient is None, "需要安装 starlette（随 fastapi 安装）")
class SecureRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        # 进程级单例：测试中直接注入密钥与极小容量，并清空防重放表。
        self._orig_keys = main.secure_config.keys
        self._orig_capacity = main.secure_config.nonce_capacity
        self._orig_ttl = main.secure_config.nonce_ttl
        main.secure_config.keys = {KEY_ID: SECRET}
        main.secure_config.nonce_capacity = 4
        main.secure_config.nonce_ttl = 600
        with _nonce_lock:
            _nonce_seen.clear()
            _nonce_index.clear()

    def tearDown(self) -> None:
        main.secure_config.keys = self._orig_keys
        main.secure_config.nonce_capacity = self._orig_capacity
        main.secure_config.nonce_ttl = self._orig_ttl
        with _nonce_lock:
            _nonce_seen.clear()
            _nonce_index.clear()

    def _envelope(
        self,
        payload: dict,
        *,
        operation: str = "plan",
        nonce: str | None = None,
        timestamp: int | None = None,
        key_id: str = KEY_ID,
        secret: bytes = SECRET,
    ) -> dict:
        envelope = {
            "operation": operation,
            "payload": payload,
            "key_id": key_id,
            "timestamp": int(time.time()) if timestamp is None else timestamp,
            "nonce": uuid.uuid4().hex if nonce is None else nonce,
        }
        envelope["signature"] = hmac.new(
            secret, canonical_json(envelope), hashlib.sha256
        ).hexdigest()
        return envelope

    def _post(self, envelope: dict):
        return self.client.post(
            "/secure-request", json=envelope, headers={}
        )

    def _fill_table(self, count: int, *, ttl: float = 300.0) -> list[str]:
        """直接向防重放表写入 count 条记录并登记到期索引，返回占用的 nonce。"""
        nonces = [f"occupied-{i}-{uuid.uuid4().hex}" for i in range(count)]
        now = time.time()
        with _nonce_lock:
            for nonce in nonces:
                key = (KEY_ID, nonce)
                token = uuid.uuid4().int
                _nonce_seen[key] = (now + ttl, token)
                heapq.heappush(_nonce_index, (now + ttl, key, token))
        return nonces

    # ------------------------------- 基本语义 -------------------------------

    def test_success_then_replay_409(self) -> None:
        envelope = self._envelope(dict(PLAN_PAYLOAD), nonce="once-1")
        resp1 = self._post(envelope)
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp1.json()["object_length"], 256)

        resp2 = self._post(envelope)
        self.assertEqual(resp2.status_code, 409)
        self.assertEqual(resp2.json()["error"], "nonce_replayed")
        self.assertEqual(resp2.json()["key_id"], KEY_ID)

    def test_bad_signature_rejected_without_occupying_nonce(self) -> None:
        envelope = self._envelope(dict(PLAN_PAYLOAD), nonce="bad-sig-1")
        envelope["signature"] = "0" * 64
        resp = self._post(envelope)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"], "invalid_signature")
        with _nonce_lock:
            self.assertNotIn((KEY_ID, "bad-sig-1"), _nonce_seen)

    # ----------------------------- 容量与 503 ------------------------------

    def test_capacity_full_returns_503_and_does_not_execute(self) -> None:
        self._fill_table(main.secure_config.nonce_capacity)
        self.assertEqual(len(_nonce_seen), 4)

        executed = threading.Event()

        def _boom(_req):  # pragma: no cover - 容量满时绝不能被调用
            executed.set()
            raise AssertionError("容量已满时不得执行 payload")

        orig_plan = main.plan
        main.plan = _boom
        try:
            resp = self._post(self._envelope(dict(PLAN_PAYLOAD)))
        finally:
            main.plan = orig_plan
        self.assertEqual(resp.status_code, 503)
        body = resp.json()
        self.assertEqual(body["error"], "nonce_capacity_exceeded")
        self.assertEqual(body["capacity"], 4)
        self.assertFalse(executed.is_set())
        # 不得驱逐任何未过期 nonce
        self.assertEqual(len(_nonce_seen), 4)

    def test_replay_409_takes_precedence_over_capacity_503(self) -> None:
        nonces = self._fill_table(main.secure_config.nonce_capacity)
        # 表已满，但其中一条 nonce 被重放：必须返回 409 而不是 503。
        resp = self._post(self._envelope(dict(PLAN_PAYLOAD), nonce=nonces[0]))
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"], "nonce_replayed")

    def test_expired_entries_swept_free_capacity(self) -> None:
        # 写满容量，但全部已过期；下一条合法请求应在同锁内清理后成功。
        self._fill_table(main.secure_config.nonce_capacity, ttl=-1.0)
        self.assertEqual(len(_nonce_seen), 4)

        resp = self._post(self._envelope(dict(PLAN_PAYLOAD), nonce="fresh-1"))
        self.assertEqual(resp.status_code, 200)
        with _nonce_lock:
            self.assertIn((KEY_ID, "fresh-1"), _nonce_seen)
            # 旧记录全部被清理，只剩本次成功的一条
            self.assertEqual(len(_nonce_seen), 1)

    def test_in_flight_placeholder_counts_toward_capacity(self) -> None:
        # 预置 1 条已成功记录，容量为 4；请求 A 在执行 plan 期间持有 1 个
        # 占位，因此其余 2 个名额被直接写入占满后，第 4 个新 nonce 应 503。
        self._fill_table(2)
        entered = threading.Event()
        gate = threading.Event()
        orig_plan = main.plan

        def _blocking_plan(req):
            entered.set()
            self.assertTrue(gate.wait(3.0), "测试同步事件超时")
            return orig_plan(req)

        main.plan = _blocking_plan
        result: dict[str, object] = {}

        def _call_a() -> None:
            result["resp"] = self._post(
                self._envelope(dict(PLAN_PAYLOAD), nonce="in-flight-A")
            )

        try:
            thread = threading.Thread(target=_call_a)
            thread.start()
            self.assertTrue(entered.wait(3.0), "请求 A 未进入执行阶段")
            # A 此时持有占位：连同预置 2 条共 3 条，再加 1 条即满。
            with _nonce_lock:
                self.assertEqual(len(_nonce_seen), 3)
            self._fill_table(1)

            blocked = self._post(self._envelope(dict(PLAN_PAYLOAD), nonce="late-X"))
            self.assertEqual(blocked.status_code, 503)
            self.assertEqual(blocked.json()["error"], "nonce_capacity_exceeded")

            gate.set()
            thread.join(3.0)
        finally:
            main.plan = orig_plan
            gate.set()

        self.assertEqual(result["resp"].status_code, 200)  # type: ignore[attr-defined]
        # A 成功后占位转为正式占用，表仍满（4 条均未过期），late-X 仍 503。
        self.assertEqual(
            self._post(self._envelope(dict(PLAN_PAYLOAD), nonce="late-X")).status_code,
            503,
        )
        # 一条旧记录到期后被下一次请求在同一把锁内经到期索引清理，腾出名额；
        # 此前被 503 拒绝的 nonce 可重试成功。
        with _nonce_lock:
            for key, (_expiry, token) in list(_nonce_seen.items()):
                if key != (KEY_ID, "in-flight-A"):
                    expired_at = time.time() - 1
                    _nonce_seen[key] = (expired_at, token)  # 置为已过期
                    heapq.heappush(_nonce_index, (expired_at, key, token))
                    break
        retry = self._post(self._envelope(dict(PLAN_PAYLOAD), nonce="late-X"))
        self.assertEqual(retry.status_code, 200)

    # --------------------------- 失败回滚占位 -----------------------------

    def test_invalid_payload_releases_placeholder(self) -> None:
        bad_payload = dict(PLAN_PAYLOAD, object_length=-1)
        envelope = self._envelope(bad_payload, nonce="retry-bad-payload")
        resp1 = self._post(envelope)
        self.assertEqual(resp1.status_code, 422)
        # 占位已释放：同一信封再来一次仍是 422（payload 错误），而非 409。
        resp2 = self._post(envelope)
        self.assertEqual(resp2.status_code, 422)

        # 客户端修正 payload 后用同一 nonce + 新签名重试，应当成功。
        fixed = self._envelope(dict(PLAN_PAYLOAD), nonce="retry-bad-payload")
        resp3 = self._post(fixed)
        self.assertEqual(resp3.status_code, 200)
        # 成功后再重放则 409。
        self.assertEqual(self._post(fixed).status_code, 409)

    def test_business_failure_releases_placeholder(self) -> None:
        bad_business = dict(PLAN_PAYLOAD, object_length=10)  # end 越界
        envelope = self._envelope(bad_business, nonce="retry-business")
        resp1 = self._post(envelope)
        self.assertEqual(resp1.status_code, 422)
        self.assertEqual(resp1.json()["error"], "unsatisfiable")
        resp2 = self._post(envelope)
        self.assertEqual(resp2.status_code, 422)
        self.assertEqual(resp2.json()["error"], "unsatisfiable")

    # ------------------- 释放占位不得误删并发重占记录 -----------------------

    def test_release_keeps_record_reoccupied_after_expiry(self) -> None:
        now = 10_000.0
        main.secure_config.nonce_ttl = 600
        status, old = _reserve_nonce((KEY_ID, "raced"), now)
        self.assertEqual(status, "reserved")
        self.assertIsNotNone(old)

        # 旧请求尚未结束，时间推进到其占位过期；并发请求清理后重新占用。
        later = now + 601
        status, new = _reserve_nonce((KEY_ID, "raced"), later)
        self.assertEqual(status, "reserved")
        self.assertNotEqual(old, new)

        # 旧请求此刻才失败并回滚自己的占位：新记录必须保留。
        with _nonce_lock:
            _release_nonce((KEY_ID, "raced"), old)  # type: ignore[arg-type]
            self.assertEqual(_nonce_seen.get((KEY_ID, "raced")), new)

        # 新占位到期后由其自己的索引条目清理；期间遗留的旧条目早已弹出。
        with _nonce_lock:
            _sweep_nonces(later + 601)
            self.assertNotIn((KEY_ID, "raced"), _nonce_seen)

    def test_release_keeps_record_with_same_expiry_but_new_token(self) -> None:
        now = 20_000.0
        status, old = _reserve_nonce((KEY_ID, "raced2"), now)
        self.assertEqual(status, "reserved")
        # 极端情形：到期时间相同，仅占位令牌不同（记录被重新占用）。
        with _nonce_lock:
            new_token = old[1] ^ 0xDEADBEEF  # type: ignore[index]
            _nonce_seen[(KEY_ID, "raced2")] = (old[0], new_token)
            # 重新占用会像 _reserve_nonce 一样登记携带新令牌的索引条目。
            heapq.heappush(_nonce_index, (old[0], (KEY_ID, "raced2"), new_token))
            _release_nonce((KEY_ID, "raced2"), old)  # type: ignore[arg-type]
            self.assertIn((KEY_ID, "raced2"), _nonce_seen)
            # 到期清理时旧令牌条目被跳过，新令牌条目删除新占位——新占位
            # 确实已到期，不存在“遗留条目删除未到期新占位”的情况。
            _sweep_nonces(old[0])  # type: ignore[index]
            self.assertNotIn((KEY_ID, "raced2"), _nonce_seen)

    # --------------------- 到期索引的推进与遗留条目 -------------------------

    def test_index_entry_registered_with_placeholder_token(self) -> None:
        now = 30_000.0
        status, reservation = _reserve_nonce((KEY_ID, "indexed"), now)
        self.assertEqual(status, "reserved")
        expiry, token = reservation  # type: ignore[misc]
        with _nonce_lock:
            self.assertEqual(_nonce_index[0], (expiry, (KEY_ID, "indexed"), token))
            self.assertEqual(_nonce_seen[(KEY_ID, "indexed")], (expiry, token))
            # 堆按到期时间有序：堆顶是最早到期的条目。
            self.assertEqual(
                [entry[0] for entry in _nonce_index],
                sorted(entry[0] for entry in _nonce_index),
            )

    def test_sweep_advances_only_over_expired_entries(self) -> None:
        # 混合到期时间填充：2 条已过期、1 条刚好到期（相等即过期）、1 条未过期。
        now = 40_000.0
        ttl = main.secure_config.nonce_ttl
        specs = [
            ("expired-a", now - ttl - 10),  # 已过期
            ("expired-b", now - ttl),       # 刚好到期
            ("fresh-a", now - ttl + 1),     # 还差 1 秒
            ("fresh-b", now - ttl + 300),   # 远未到期
        ]
        for nonce, reserved_at in specs:
            status, _r = _reserve_nonce((KEY_ID, nonce), reserved_at)
            self.assertEqual(status, "reserved")
        self.assertEqual(len(_nonce_seen), 4)

        with _nonce_lock:
            heap_before = len(_nonce_index)
            _sweep_nonces(now)
            # 只弹出了 2 条到期索引项，未过期的 2 条索引项原样保留。
            self.assertEqual(len(_nonce_index), heap_before - 2)
            self.assertNotIn((KEY_ID, "expired-a"), _nonce_seen)
            self.assertNotIn((KEY_ID, "expired-b"), _nonce_seen)
            self.assertIn((KEY_ID, "fresh-a"), _nonce_seen)
            self.assertIn((KEY_ID, "fresh-b"), _nonce_seen)

        # 时间再推进 1 秒：fresh-a 到期后被下一次清理，fresh-b 仍保留。
        with _nonce_lock:
            _sweep_nonces(now + 1)
            self.assertNotIn((KEY_ID, "fresh-a"), _nonce_seen)
            self.assertIn((KEY_ID, "fresh-b"), _nonce_seen)
            self.assertEqual(len(_nonce_seen), 1)

    def test_stale_entry_from_released_placeholder_is_skipped(self) -> None:
        # 失败请求回滚占位后，其索引条目成为遗留项：到期弹出时记录已不存在，
        # 必须跳过而不是报错，更不能影响其他 nonce 的预留与清理。
        now = 50_000.0
        ttl = main.secure_config.nonce_ttl
        status, reservation = _reserve_nonce((KEY_ID, "rolled-back"), now)
        self.assertEqual(status, "reserved")
        with _nonce_lock:
            _release_nonce((KEY_ID, "rolled-back"), reservation)  # type: ignore[arg-type]
        self.assertNotIn((KEY_ID, "rolled-back"), _nonce_seen)

        # 推进到遗留条目到期之后再预留新 nonce：清理弹出遗留项并跳过。
        later = now + ttl + 1
        status, _r = _reserve_nonce((KEY_ID, "next"), later)
        self.assertEqual(status, "reserved")
        with _nonce_lock:
            self.assertNotIn((KEY_ID, "rolled-back"), _nonce_seen)
            self.assertIn((KEY_ID, "next"), _nonce_seen)
            # 遗留条目已被弹出，索引中只剩新占位自己的条目。
            self.assertEqual(
                [entry[1] for entry in _nonce_index], [(KEY_ID, "next")]
            )

    def test_stale_entry_after_reoccupation_never_deletes_new_placeholder(self) -> None:
        # 旧占位过期、同名 nonce 重新占用：旧索引条目到期弹出时凭令牌识别为
        # 遗留项并跳过，新占位在其自身到期前必须始终保留（含容量计数）。
        now = 60_000.0
        ttl = main.secure_config.nonce_ttl
        status, old = _reserve_nonce((KEY_ID, "same-name"), now)
        self.assertEqual(status, "reserved")
        later = now + ttl + 1
        status, new = _reserve_nonce((KEY_ID, "same-name"), later)
        self.assertEqual(status, "reserved")
        with _nonce_lock:
            # 第二次预留的清理已经弹出旧条目；表中只有携带新令牌的新占位。
            self.assertEqual(_nonce_seen[(KEY_ID, "same-name")], new)
            self.assertEqual(
                [entry for entry in _nonce_index if entry[1] == (KEY_ID, "same-name")],
                [(new[0], (KEY_ID, "same-name"), new[1])],  # type: ignore[index]
            )
            # 新占位尚未到期：再清理一次也不得动它。
            _sweep_nonces(later)
            self.assertEqual(_nonce_seen.get((KEY_ID, "same-name")), new)


class NonceCapacityConfigTests(unittest.TestCase):
    """SECURE_REQUEST_NONCE_CAPACITY 的默认值与启动期校验。"""

    ENV_VARS = (
        "SECURE_REQUEST_KEYS",
        "SECURE_REQUEST_TIMESTAMP_SKEW",
        "SECURE_REQUEST_NONCE_TTL",
        "SECURE_REQUEST_NONCE_CAPACITY",
    )

    def setUp(self) -> None:
        self._saved = {k: os.environ.pop(k, None) for k in self.ENV_VARS}

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_default_capacity_is_10000(self) -> None:
        cfg = SecureRequestConfig()
        self.assertEqual(cfg.nonce_capacity, 10000)

    def test_custom_capacity_is_parsed(self) -> None:
        os.environ["SECURE_REQUEST_NONCE_CAPACITY"] = "123"
        self.assertEqual(SecureRequestConfig().nonce_capacity, 123)

    def test_capacity_must_be_positive_integer(self) -> None:
        for bad in ("0", "-1"):
            os.environ["SECURE_REQUEST_NONCE_CAPACITY"] = bad
            with self.subTest(bad=bad):
                with self.assertRaises(RuntimeError):
                    SecureRequestConfig()
        os.environ["SECURE_REQUEST_NONCE_CAPACITY"] = "not-an-int"
        with self.assertRaises(ValueError):
            SecureRequestConfig()


if __name__ == "__main__":
    unittest.main()
