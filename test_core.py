#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核心纯函数单元测试：jwt_exp_info / compute / detect_anomaly。

运行：
    .venv/Scripts/python.exe test_core.py
"""
import base64
import json
import time
import unittest

import dorm_elec_auto as m


def _make_jwt(exp, iat=None, include_exp=True):
    if iat is None:
        iat = exp - 30 * 86400
    payload = {"iat": iat}
    if include_exp:
        payload["exp"] = exp
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    raw = raw.rstrip("=")
    return f"header.{raw}.sig"


class TestJwtExpInfo(unittest.TestCase):
    def test_expired(self):
        exp = int(time.time()) - 100
        e, d, ex, p = m.jwt_exp_info(_make_jwt(exp))
        self.assertTrue(p)
        self.assertTrue(ex)
        self.assertIsNotNone(e)

    def test_valid(self):
        exp = int(time.time()) + 86400
        e, d, ex, p = m.jwt_exp_info(_make_jwt(exp))
        self.assertTrue(p)
        self.assertFalse(ex)
        self.assertGreater(d, 0)

    def test_non_jwt(self):
        self.assertEqual(m.jwt_exp_info("not-a-jwt"), (None, None, False, False))

    def test_ms_exp(self):
        exp = (int(time.time()) + 86400) * 1000
        e, d, ex, p = m.jwt_exp_info(_make_jwt(exp))
        self.assertTrue(p)
        self.assertFalse(ex)
        self.assertGreater(d, 0)

    def test_no_exp(self):
        e, d, ex, p = m.jwt_exp_info(_make_jwt(0, include_exp=False))
        self.assertEqual(e, None)
        self.assertTrue(p)


class TestCompute(unittest.TestCase):
    def test_usage_counted(self):
        # 用较细的时间间隔，避免「按段结束时间判定近24h」的边界把前一天的段也计入
        now = int(time.time())
        hist = [{"t": now - 3 * 86400, "b": 10.0},   # 3 天前
                {"t": now - 2 * 86400, "b": 8.0},    # 2 天前（耗 2，结束点不在近24h）
                {"t": now - 6 * 3600, "b": 5.0},     # 6h 前（耗 3，在近24h）
                {"t": now, "b": 3.0}]                # 现在（耗 2，在近24h）
        _, last24, avg = m.compute(hist, 3.0)
        self.assertAlmostEqual(last24, 5.0, places=2)   # 近24h 总共用了 3+2 = 5
        self.assertGreater(avg, 0)

    def test_recharge_excluded(self):
        # 余额只增不减（纯充值），不应计入用电
        now = int(time.time())
        hist = [{"t": now - 2 * 86400, "b": 10.0},
                {"t": now - 86400, "b": 20.0},
                {"t": now, "b": 30.0}]
        _, last24, avg = m.compute(hist, 30.0)
        self.assertEqual(last24, 0.0)
        self.assertEqual(avg, 0.0)


class TestDetectAnomaly(unittest.TestCase):
    def test_insufficient_samples(self):
        hist = [{"t": int(time.time()), "b": 10.0}]
        self.assertEqual(m.detect_anomaly(hist), (False, {}))

    def test_normal_no_spike(self):
        now = int(time.time())
        hist = []
        bal = 100.0
        for i in range(1, 30):
            hist.append({"t": now - (30 - i) * 3600, "b": bal})
            bal -= 0.1   # 正常缓慢耗电
        self.assertFalse(m.detect_anomaly(hist)[0])

    def test_spike_triggers(self):
        now = int(time.time())
        hist = []
        bal = 100.0
        for i in range(1, 40):
            hist.append({"t": now - (40 - i) * 3600, "b": bal})
            bal -= 0.1   # 正常缓慢耗电
        hist.append({"t": now + 3600, "b": bal - 5.0})   # 再往后 1h 突增 5 元
        is_anom, info = m.detect_anomaly(hist)
        self.assertTrue(is_anom)
        self.assertIn("last_usage", info)


if __name__ == "__main__":
    unittest.main(verbosity=2)
