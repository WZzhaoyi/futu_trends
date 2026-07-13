"""条件单引擎与网关单元测试

运行: python -m unittest tests.test_condition_engine -v

覆盖:
- 触发/一次性语义/状态持久化/重启跳过/配置修改重新激活
- 零价tick防护、终态订单归档
- 撤单重试上限
- 配置校验(变量名/信号名/操作符语义/网关引用/订阅fail-fast等)
- 雪花订单ID、QMT人工订单过滤、Futu美股时区
"""
import configparser
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import order_engine.condition as condition_module
from order_engine.condition import ConditionOrderEngine
from order_engine.core import BaseGateway, MainEngine
from order_engine.event_engine import EVENT_TICK, Event, EventEngine
from order_engine.gateway import FutuGateway, QmtGateway, SimGateway
from order_engine.models import Exchange, OrderData, Status, TickData

CHINA_TZ = ZoneInfo("Asia/Shanghai")
REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")

BASE_YAML = """
orders:
  - id: t1
    enabled: true
    description: "test"
    symbol: "00700"
    exchange: "HK"
    data_gateway: "FAKE"
    order_gateway: "SIM"
    conditions:
      logical_operator: "AND"
      items:
        - variable: "last_price"
          operator: ">"
          value: 400
    actions:
      - type: "limit"
        action: "buy"
        quantity: 100
        price: 405
"""


class FakeDataGateway(BaseGateway):
    """可订阅成功的假行情网关，tick由测试手动推送"""
    def connect(self, setting): pass
    def subscribe(self, req): return True
    def send_order(self, req): return ""
    def cancel_order(self, req): return None
    def close(self): pass


class FailOrderGateway(BaseGateway):
    """下单永远失败的订单网关，验证一次性语义"""
    def connect(self, setting): pass
    def subscribe(self, req): return False
    def send_order(self, req): return ""
    def cancel_order(self, req): return None
    def close(self): pass


class BlindDataGateway(BaseGateway):
    """订阅永远失败的行情网关，验证fail-fast"""
    def connect(self, setting): pass
    def subscribe(self, req): return False
    def send_order(self, req): return ""
    def cancel_order(self, req): return None
    def close(self): pass


class HoldOrderGateway(BaseGateway):
    """订单一直挂着不成交、撤单提交永远失败的网关，验证撤单重试上限"""
    seq = 0
    def connect(self, setting): pass
    def subscribe(self, req): return False
    def send_order(self, req):
        HoldOrderGateway.seq += 1
        order = OrderData(gateway_name=self.gateway_name, symbol=req.symbol,
                          exchange=req.exchange, orderid=f"H{HoldOrderGateway.seq}",
                          volume=req.volume, price=req.price, status=Status.NOTTRADED)
        self.on_order(order)
        return order.vt_orderid
    def cancel_order(self, req): return None
    def close(self): pass


class ConditionEngineTestCase(unittest.TestCase):
    """带引擎装配与临时目录的基类"""

    def setUp(self):
        self.workdir = tempfile.mkdtemp()
        self.yaml_path = os.path.join(self.workdir, "orders.yaml")
        self.state_path = os.path.join(self.workdir, "order_state.json")
        self.events_path = os.path.join(self.workdir, "order_events.jsonl")
        self._mains = []
        self.write_yaml(BASE_YAML)

    def tearDown(self):
        for main in self._mains:
            main.event_engine.stop()
        shutil.rmtree(self.workdir, ignore_errors=True)

    def write_yaml(self, text):
        with open(self.yaml_path, "w", encoding="utf-8") as f:
            f.write(text)

    def make_config(self):
        config = configparser.ConfigParser()
        config.add_section("CONFIG")
        config.set("CONFIG", "ORDER_CONFIG", self.yaml_path)
        config.set("CONFIG", "ORDER_STATE_FILE", self.state_path)
        config.set("CONFIG", "ORDER_EVENT_LOG", self.events_path)
        config.set("CONFIG", "ORDER_DATA_GATEWAY", "FAKE")
        config.set("CONFIG", "ORDER_TRADE_GATEWAY", "SIM")
        return config

    def build_engine(self):
        event_engine = EventEngine()
        main_engine = MainEngine(event_engine)
        self._mains.append(main_engine)
        main_engine.add_gateway(FakeDataGateway, "FAKE")
        main_engine.add_gateway(FailOrderGateway, "FAILGW")
        main_engine.add_gateway(BlindDataGateway, "BLIND")
        main_engine.add_gateway(HoldOrderGateway, "HOLD")
        main_engine.add_gateway(SimGateway, "SIM")
        main_engine.connect({}, "SIM")
        engine = ConditionOrderEngine(main_engine, event_engine)
        logs = []
        engine.load(self.make_config(), logs.append)
        return main_engine, engine, logs

    def push_tick(self, main_engine, price):
        tick = TickData(symbol="00700", exchange=Exchange.SEHK, gateway_name="FAKE",
                        datetime=datetime.now(CHINA_TZ), last_price=price)
        main_engine.event_engine.put(Event(EVENT_TICK, tick))

    def wait_until(self, cond, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            time.sleep(0.05)
        return cond()

    def read_state(self):
        with open(self.state_path, encoding="utf-8") as f:
            return json.load(f)


class TestTriggerLifecycle(ConditionEngineTestCase):

    def test_zero_price_tick_ignored(self):
        main, engine, _ = self.build_engine()
        self.push_tick(main, 0)
        time.sleep(0.3)
        self.assertIn("t1", engine.active_orders)

    def test_trigger_retire_persist_and_archive(self):
        main, engine, _ = self.build_engine()
        self.push_tick(main, 405.5)
        self.assertTrue(self.wait_until(lambda: "t1" not in engine.active_orders))
        state = self.read_state()
        self.assertIn("t1", state["triggered"])
        # SIM立即全成 → 终态归档: 内存释放、terminal_orders落盘
        self.assertTrue(self.wait_until(lambda: not engine.managed_orders))
        self.assertFalse(engine.order_snapshots)
        self.assertEqual(len(self.read_state()["terminal_orders"]), 1)
        # 审计日志有订单与成交记录
        with open(self.events_path, encoding="utf-8") as f:
            kinds = [json.loads(line)["kind"] for line in f]
        self.assertIn("order", kinds)
        self.assertIn("trade", kinds)

    def test_restart_skips_triggered_order(self):
        main, engine, _ = self.build_engine()
        self.push_tick(main, 405.5)
        self.assertTrue(self.wait_until(lambda: "t1" not in engine.active_orders))

        _, engine2, logs2 = self.build_engine()
        self.assertNotIn("t1", engine2.active_orders)
        self.assertTrue(self.wait_until(lambda: any("跳过装载" in m for m in logs2)))

    def test_edited_config_rearms(self):
        main, engine, _ = self.build_engine()
        self.push_tick(main, 405.5)
        self.assertTrue(self.wait_until(lambda: "t1" not in engine.active_orders))

        self.write_yaml(BASE_YAML.replace("value: 400", "value: 500"))
        _, engine2, _ = self.build_engine()
        self.assertIn("t1", engine2.active_orders)
        self.assertNotIn("t1", self.read_state()["triggered"])

    def test_one_shot_no_retry_on_failure(self):
        self.write_yaml(BASE_YAML.replace('order_gateway: "SIM"', 'order_gateway: "FAILGW"'))
        main, engine, logs = self.build_engine()
        self.push_tick(main, 405.5)
        self.push_tick(main, 406.0)
        self.assertTrue(self.wait_until(lambda: "t1" not in engine.active_orders))
        time.sleep(0.3)
        self.assertIn("t1", self.read_state()["triggered"])
        self.assertEqual(sum("下单失败" in m for m in logs), 1)


class TestCancelRetry(ConditionEngineTestCase):

    def setUp(self):
        super().setUp()
        self._orig_interval = condition_module.CANCEL_RETRY_INTERVAL
        condition_module.CANCEL_RETRY_INTERVAL = 0.05

    def tearDown(self):
        condition_module.CANCEL_RETRY_INTERVAL = self._orig_interval
        super().tearDown()

    def test_cancel_attempts_capped(self):
        self.write_yaml(BASE_YAML.replace('order_gateway: "SIM"', 'order_gateway: "HOLD"') + """
    cancel:
      enabled: true
      conditions:
        variable: "last_price"
        operator: ">"
        value: 0
""")
        main, engine, logs = self.build_engine()
        for i in range(6):
            self.push_tick(main, 405.5 + i)
            time.sleep(0.15)
        managed = list(engine.managed_orders.values())
        self.assertEqual(len(managed), 1)
        self.assertEqual(managed[0]["cancel_attempts"], condition_module.CANCEL_MAX_ATTEMPTS)
        self.assertEqual(sum("撤单提交失败" in m for m in logs), condition_module.CANCEL_MAX_ATTEMPTS)
        self.assertEqual(sum("请人工处理" in m for m in logs), 1)


class TestConfigValidation(ConditionEngineTestCase):

    INVALID_CASES = {
        # 静默装死类
        "unquoted_symbol": BASE_YAML.replace('symbol: "00700"', "symbol: 000700"),
        "variable_typo": BASE_YAML.replace('variable: "last_price"', 'variable: "last_pric"'),
        "unknown_gateway": BASE_YAML.replace('order_gateway: "SIM"', 'order_gateway: "TYPO"'),
        "blind_data_gateway": BASE_YAML.replace('data_gateway: "FAKE"', 'data_gateway: "BLIND"'),
        # 值合法性
        "bool_value": BASE_YAML.replace("value: 400", "value: true"),
        "zero_quantity": BASE_YAML.replace("quantity: 100", "quantity: 0"),
        "limit_without_price": BASE_YAML.replace("\n        price: 405", ""),
        # 操作符语义（对齐IBKR: 连续值无相等，字符串无方向）
        "price_equality": BASE_YAML.replace('operator: ">"', 'operator: "=="'),
        "limit_up_variable": BASE_YAML.replace('variable: "last_price"', 'variable: "limit_up"'),
    }
    LEAF_REPLACEMENTS = {
        "signal_typo": ('- variable: "signal.breakthrough"\n          operator: "contains"\n          value: "x"'),
        "signal_directional": ('- variable: "signal.breakout"\n          operator: ">"\n          value: "x"'),
        "datetime_equality": ('- variable: "datetime"\n          operator: "!="\n          value: "2026-07-13 10:00:00 +0800"'),
        "datetime_bad_format": ('- variable: "datetime"\n          operator: ">"\n          value: "2026/07/13 10:00:00"'),
    }
    BASE_LEAF = '- variable: "last_price"\n          operator: ">"\n          value: 400'

    def test_invalid_configs_rejected_at_load(self):
        cases = dict(self.INVALID_CASES)
        for name, leaf in self.LEAF_REPLACEMENTS.items():
            cases[name] = BASE_YAML.replace(self.BASE_LEAF, leaf)
        for name, yaml_text in cases.items():
            with self.subTest(case=name):
                self.write_yaml(yaml_text)
                with self.assertRaises(ValueError):
                    self.build_engine()

    def test_example_yaml_valid(self):
        import yaml as yaml_lib
        engine = ConditionOrderEngine.__new__(ConditionOrderEngine)
        path = os.path.join(REPO_ROOT, "orders.example.yaml")
        with open(path, encoding="utf-8") as f:
            for order in yaml_lib.safe_load(f)["orders"]:
                engine._validate_order_config(order)


class TestGateways(unittest.TestCase):

    def test_snowflake_order_ids(self):
        qmt = QmtGateway(EventEngine())
        ids = [qmt._get_order_id() for _ in range(5000)]
        self.assertTrue(all(i.isdigit() for i in ids))
        self.assertEqual(len(set(ids)), 5000)
        self.assertTrue(all(int(a) < int(b) for a, b in zip(ids, ids[1:])))

    def test_manual_orders_filtered(self):
        qmt = QmtGateway(EventEngine())
        emitted = []
        qmt.on_order = emitted.append

        def fake_order(remark, oid):
            return SimpleNamespace(order_remark=remark, stock_code="000001.SZ",
                                   order_status=50, price=10.0, order_volume=100,
                                   traded_volume=0, order_type=None,
                                   order_time=1780000000, order_id=oid)

        qmt._on_orders([fake_order("123456789", 1),   # 本引擎雪花remark → 处理
                        fake_order("", 2),            # 人工订单 → 过滤
                        fake_order("manual#1", 3)])   # 其他系统 → 过滤
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].orderid, "123456789")

    def test_manual_trades_filtered_and_deduped(self):
        qmt = QmtGateway(EventEngine())
        emitted = []
        qmt.on_trade = emitted.append

        def fake_trade(remark, tid):
            return SimpleNamespace(order_remark=remark, stock_code="000001.SZ",
                                   traded_id=tid, traded_price=10.0,
                                   traded_time=1780000000, traded_volume=100,
                                   order_type=None)

        qmt._on_trades([fake_trade("123456789", "T1"),
                        fake_trade("123456789", "T1"),   # 重复推送 → 去重
                        fake_trade("", "T2")])           # 人工成交 → 过滤
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].tradeid, "T1")

    def test_futu_tick_timezone_by_market(self):
        import pandas as pd
        futu = FutuGateway(EventEngine())
        captured = []
        futu.on_tick = captured.append
        row = {"open_price": 1.0, "high_price": 1.0, "low_price": 1.0,
               "prev_close_price": 1.0, "volume": 100}
        futu.process_quote(pd.DataFrame([
            {"code": "US.AAPL", "data_date": "2026-07-10",
             "data_time": "16:00:00.381", "last_price": 315.32, **row},
            {"code": "HK.00700", "data_date": "2026-07-13",
             "data_time": "11:23:35", "last_price": 460.8, **row},
        ]))
        us_offset = captured[0].datetime.utcoffset().total_seconds() / 3600
        hk_offset = captured[1].datetime.utcoffset().total_seconds() / 3600
        self.assertEqual(us_offset, -4)  # 美东夏令时
        self.assertEqual(hk_offset, 8)
        # 富途无真实涨跌停，不做估算
        self.assertEqual(captured[0].limit_up, 0)
        self.assertEqual(captured[0].limit_down, 0)


if __name__ == "__main__":
    unittest.main()
