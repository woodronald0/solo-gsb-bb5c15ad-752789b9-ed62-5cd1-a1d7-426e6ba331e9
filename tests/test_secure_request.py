# -*- coding: utf-8 -*-
"""POST /secure-request 的鉴权与 nonce 防重放行为测试。

覆盖：
- 成功路径与原签名/时间窗/业务响应语义不变；
- 重放返回 409 nonce_replayed，且在防重放表已满时 409 仍优先于 503；
- 容量对进行中占位与窗口内已成功 nonce 统一计数；容量已满且无过期项时
  返回 503 nonce_capacity_exceeded（附 capacity），不驱逐未过期 nonce、
  不执行 payload；过期项在同一把锁内清理后可自动腾出名额；
- 过期清理由按到期时间推进的索引驱动：单次预留只从索引头部弹出已到期
  条目，不扫描未过期 nonce；
- payload/业务失败（422）释放请求自身占位，可用同一 nonce 重试；
- 释放占位时若该记录已过期并被并发请求重新占用，不能误删新记录；
- 索引条目携带占位令牌：占位释放或同名 nonce 重新占用后，遗留的索引
  条目弹出时不得删除主表中的新占位；
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
    _nonce_expiry_index,
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
        # 进程级单例：测试中直接注入密钥与极小容量，并清空防重放主表与到期索引。
        self._orig_keys = main.secure_config.keys
        self._orig_capacity = main.secure_config.nonce_capacity
        self._orig_ttl = main.secure_config.nonce_ttl
        main.secure_config.keys = {KEY_ID: SECRET}
        main.secure_config.nonce_capacity = 4
        main.secure_config.nonce_ttl = 600
        with _nonce_lock:
            _nonce_seen.clear()
            _nonce_expiry_index.clear()

    def tearDown(self) -> None:
        main.secure_config.keys = self._orig_keys
        main.secure_config.nonce_capacity = self._orig_capacity
        main.secure_config.nonce_ttl = self._orig_ttl
        with _nonce_lock:
            _nonce_seen.clear()
            _nonce_expiry_index.clear()

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
        """写入 count 条到期时间约为 now+ttl 的记录，返回占用的 nonce。

        通过正式预留接口（按过去时刻预留）写入，保证主表与到期索引
        同步维护，与真实请求路径一致。
        """
        nonces = [f"occupied-{i}-{uuid.uuid4().hex}" for i in range(count)]
        now = time.time()
        with _nonce_lock:
            for nonce in nonces:
                status, _ = _reserve_nonce(
                    (KEY_ID, nonce), now + ttl - main.secure_config.nonce_ttl
                )
                self.assertEqual(status, "reserved")
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
        # 一条旧记录到期后被下一次请求在同一把锁内清理，腾出名额；
        # 此前被 503 拒绝的 nonce 可重试成功。
        with _nonce_lock:
            for key in list(_nonce_seen):
                if key != (KEY_ID, "in-flight-A"):
                    _expiry, token = _nonce_seen[key]
                    past = time.time() - 1
                    # 主表与到期索引同步修改（旧索引条目成为遗留项，
                    # 弹出时会因记录不匹配被丢弃）
                    _nonce_seen[key] = (past, token)
                    heapq.heappush(_nonce_expiry_index, (past, token, key))
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

    def test_release_keeps_record_with_same_expiry_but_new_token(self) -> None:
        now = 20_000.0
        status, old = _reserve_nonce((KEY_ID, "raced2"), now)
        self.assertEqual(status, "reserved")
        # 极端情形：到期时间相同，仅占位令牌不同（记录被重新占用）。
        with _nonce_lock:
            _nonce_seen[(KEY_ID, "raced2")] = (old[0], old[1] ^ 0xDEADBEEF)  # type: ignore[index]
            _release_nonce((KEY_ID, "raced2"), old)  # type: ignore[arg-type]
            self.assertIn((KEY_ID, "raced2"), _nonce_seen)

    # ------------------- 到期索引：只处理到期项、遗留项不误删 -------------------

    def test_sweep_processes_only_expired_entries(self) -> None:
        # 2 条已过期 + 2 条未过期：清理只弹出到期项，未过期记录与其索引
        # 条目都保持不动（无需扫描全表）。
        expired = self._fill_table(2, ttl=-1.0)
        fresh = self._fill_table(2, ttl=300.0)
        with _nonce_lock:
            self.assertEqual(len(_nonce_expiry_index), 4)
            _sweep_nonces(time.time())
            self.assertEqual(len(_nonce_seen), 2)
            for nonce in fresh:
                self.assertIn((KEY_ID, nonce), _nonce_seen)
            for nonce in expired:
                self.assertNotIn((KEY_ID, nonce), _nonce_seen)
            # 索引中只剩下两条未过期记录的条目
            self.assertEqual(len(_nonce_expiry_index), 2)
            self.assertTrue(
                all(expiry > time.time() for expiry, _t, _k in _nonce_expiry_index)
            )

    def test_sweep_stale_index_entry_keeps_reoccupied_record(self) -> None:
        ttl = main.secure_config.nonce_ttl
        t0 = 30_000.0
        status, old = _reserve_nonce((KEY_ID, "idx-raced"), t0)
        self.assertEqual(status, "reserved")
        # 失败请求释放占位：主表删除，索引条目遗留。
        with _nonce_lock:
            _release_nonce((KEY_ID, "idx-raced"), old)  # type: ignore[arg-type]
            self.assertNotIn((KEY_ID, "idx-raced"), _nonce_seen)
        # 旧索引条目尚未到期时，同名 nonce 被并发请求重新占用。
        t1 = t0 + ttl - 1
        status, new = _reserve_nonce((KEY_ID, "idx-raced"), t1)
        self.assertEqual(status, "reserved")
        self.assertNotEqual(old, new)
        # 旧索引条目到期弹出：令牌与主表记录不符，必须直接丢弃，
        # 不得删除新占位。
        with _nonce_lock:
            _sweep_nonces(t0 + ttl + 0.5)  # 早于新记录的到期时间 t1+ttl
            self.assertEqual(_nonce_seen.get((KEY_ID, "idx-raced")), new)

    def test_sweep_discards_index_entry_of_released_placeholder(self) -> None:
        ttl = main.secure_config.nonce_ttl
        t0 = 40_000.0
        status, reservation = _reserve_nonce((KEY_ID, "idx-released"), t0)
        self.assertEqual(status, "reserved")
        with _nonce_lock:
            _release_nonce((KEY_ID, "idx-released"), reservation)  # type: ignore[arg-type]
            # 释放只删主表：索引条目仍在，留待到期时惰性丢弃。
            self.assertEqual(len(_nonce_expiry_index), 1)
            _sweep_nonces(t0 + ttl + 1)
            self.assertEqual(len(_nonce_expiry_index), 0)
            self.assertNotIn((KEY_ID, "idx-released"), _nonce_seen)


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
