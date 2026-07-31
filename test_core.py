#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核心纯函数单元测试：_load_dotenv / compute / detect_anomaly / save_history。

运行：
    .venv/Scripts/python.exe test_core.py
"""
import json
import os
import tempfile
import time
import unittest

import dorm_elec_auto as m


class TestLoadDotenv(unittest.TestCase):
    """测试 .env 文件解析逻辑。"""

    def test_basic_kv(self):
        d = m._load_dotenv(self._make_env("KEY=val\nFOO=bar"))
        self.assertEqual(d["KEY"], "val")
        self.assertEqual(d["FOO"], "bar")

    def test_double_quoted_value(self):
        d = m._load_dotenv(self._make_env("KEY=\"hello world\""))
        self.assertEqual(d["KEY"], "hello world")

    def test_single_quoted_value(self):
        d = m._load_dotenv(self._make_env("KEY='hello world'"))
        self.assertEqual(d["KEY"], "hello world")

    def test_value_with_equals(self):
        """值中包含 = 号应正确解析（split("=", 1) 的正确行为）。"""
        d = m._load_dotenv(self._make_env("URL=https://example.com?a=1&b=2"))
        self.assertEqual(d["URL"], "https://example.com?a=1&b=2")

    def test_value_with_inner_quotes(self):
        """值包含引号字符但首尾不是配对引号，应保留原样。"""
        d = m._load_dotenv(self._make_env("KEY=val\"ue"))
        self.assertEqual(d["KEY"], "val\"ue")

    def test_empty_and_comments(self):
        d = m._load_dotenv(self._make_env("\n# comment\n\nA=1\n"))
        self.assertEqual(d, {"A": "1"})

    def test_missing_file(self):
        d = m._load_dotenv("/nonexistent/path/.env")
        self.assertEqual(d, {})

    @staticmethod
    def _make_env(content):
        fd, path = tempfile.mkstemp(suffix=".env")
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        return path


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

    def test_old_data_excluded_from_baseline(self):
        """旧数据（>30天）不应参与基线计算，避免假期零耗电污染基线。"""
        now = int(time.time())
        hist = []
        # 40 天前~31 天前：几乎不耗电（模拟假期）
        bal = 100.0
        for i in range(10):
            t = now - (40 - i) * 86400
            hist.append({"t": t, "b": bal})
            bal -= 0.001  # 几乎不耗电
        # 近 30 天内：正常缓慢耗电
        for i in range(20):
            t = now - (30 - i) * 3600
            hist.append({"t": t, "b": bal})
            bal -= 0.1  # 正常耗电
        # 最后一段：正常范围内，不应因旧数据的零基线而被误报
        hist.append({"t": now, "b": bal - 0.15})
        is_anom, _ = m.detect_anomaly(hist)
        self.assertFalse(is_anom, "正常用电不应因旧数据（假期零耗电）污染基线而被误判为异常")


class TestSaveHistory(unittest.TestCase):
    def test_atomic_roundtrip(self):
        d = tempfile.mkdtemp()
        orig_store = m.STORE
        try:
            m.STORE = os.path.join(d, "dorm_balance.json")
            payload = {"history": [{"t": 1, "b": 9.9}], "marker": "atomic-ok"}
            m.save_history(payload)
            # 文件应完整可解析，且内容与写入一致（无半截 JSON）
            with open(m.STORE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            self.assertEqual(loaded["marker"], "atomic-ok")
            self.assertEqual(loaded["history"][0]["b"], 9.9)
        finally:
            m.STORE = orig_store
            for fn in os.listdir(d):
                os.remove(os.path.join(d, fn))
            os.rmdir(d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
