"""条件单系统前台入口，支持 ``python -m order_engine --config <path>``。"""
import os
import queue
import time
import threading
from datetime import datetime

import yaml

from ft_config import get_config
from notification_engine import NotificationEngine
from order_engine import ConditionOrderEngine, MainEngine, EventEngine, FutuGateway, QmtGateway, SimGateway

GATEWAY_NAMES = {"FUTU", "QMT", "SIM"}
NOTIFY_THROTTLE_SECONDS = 60
NOTIFY_QUEUE_MAXSIZE = 1000


class AsyncNotifier:
    """异步通知：独立线程发送，避免阻塞事件引擎；相同消息在节流窗口内只发一次。"""

    def __init__(self, notification_engine: NotificationEngine,
                 throttle_seconds: int = NOTIFY_THROTTLE_SECONDS,
                 maxsize: int = NOTIFY_QUEUE_MAXSIZE):
        self.engine = notification_engine
        self.throttle_seconds = throttle_seconds
        self.queue = queue.Queue(maxsize=maxsize)
        self._last_sent = {}
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def notify(self, msg: str):
        print(f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} {msg}')
        now = time.time()
        last = self._last_sent.get(msg)
        if last is not None and now - last < self.throttle_seconds:
            return
        self._last_sent[msg] = now
        if len(self._last_sent) > 2000:
            cutoff = now - self.throttle_seconds
            self._last_sent = {m: t for m, t in self._last_sent.items() if t >= cutoff}
        try:
            self.queue.put_nowait(msg)
        except queue.Full:
            print(f"通知队列已满，丢弃: {msg}")

    def _run(self):
        while True:
            msg = self.queue.get()
            for send in (
                self.engine.send_webhook,
                self.engine.send_telegram_message,
                lambda m: self.engine.send_email(m, m),
            ):
                try:
                    send(msg)
                except Exception as e:
                    print(f"通知发送失败: {e}")


def collect_order_gateway_names(config, default_data_gateway: str, default_order_gateway: str) -> set:
    """从条件单yaml收集启用订单引用的网关，保证per-order覆盖的网关也会被注册。"""
    names = set()
    order_config_path = config.get("CONFIG", "ORDER_CONFIG", fallback=None)
    if not order_config_path or not os.path.exists(order_config_path):
        return names
    with open(order_config_path, 'r', encoding='utf-8') as f:
        yaml_config = yaml.safe_load(f) or {}
    orders = yaml_config.get('orders', [])
    if not isinstance(orders, list):
        return names
    for order in orders:
        if not isinstance(order, dict) or not order.get('enabled', False):
            continue
        names.add(str(order.get('data_gateway', default_data_gateway)).upper())
        names.add(str(order.get('order_gateway', default_order_gateway)).upper())
        for action in order.get('actions') or []:
            if isinstance(action, dict) and 'order_gateway' in action:
                names.add(str(action['order_gateway']).upper())
    return names


def main():
    config = get_config()

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    data_gateway_name = config.get("CONFIG", "ORDER_DATA_GATEWAY", fallback="FUTU").strip().upper()
    order_gateway_name = config.get("CONFIG", "ORDER_TRADE_GATEWAY", fallback="QMT").strip().upper()
    gateway_names = {data_gateway_name, order_gateway_name}
    gateway_names |= collect_order_gateway_names(config, data_gateway_name, order_gateway_name)
    unknown_gateways = gateway_names - GATEWAY_NAMES
    if unknown_gateways:
        raise ValueError(f"不支持的网关: {', '.join(sorted(unknown_gateways))}")

    if "FUTU" in gateway_names:
        futu_setting = {
            "host": config.get("CONFIG", "FUTU_HOST"),
            "port": int(config.get("CONFIG", "FUTU_PORT")),
        }
        main_engine.add_gateway(FutuGateway, "FUTU")
        main_engine.connect(futu_setting, "FUTU")

    if "QMT" in gateway_names:
        qmt_setting = {
            "path": config.get("CONFIG", "QMT_PATH"),
            "session_id": int(time.time()),
            "account_id": config.get("CONFIG", "QMT_ACCOUNT_ID"),
        }
        main_engine.add_gateway(QmtGateway, "QMT")
        main_engine.connect(qmt_setting, "QMT")

    if "SIM" in gateway_names:
        main_engine.add_gateway(SimGateway, "SIM")
        main_engine.connect({}, "SIM")

    # 远程通知：异步发送 + 重复消息节流，避免阻塞事件引擎
    notifier = AsyncNotifier(NotificationEngine(config))

    # 添加条件单配置
    condition_engine: ConditionOrderEngine = main_engine.add_engine(ConditionOrderEngine)
    condition_engine.load(config, notifier.notify)

    # 阻塞主线程，等待事件处理
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        main_engine.close()


if __name__ == "__main__":
    main()
