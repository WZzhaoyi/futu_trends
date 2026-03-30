"""条件单系统入口脚本"""
import time
import threading
from datetime import datetime

from ft_config import get_config
from notification_engine import NotificationEngine
from order_engine import ConditionOrderEngine, MainEngine, EventEngine, FutuGateway, QmtGateway


def main():
    config = get_config()

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    futu_setting = {
        "host": config.get("CONFIG", "FUTU_HOST"),
        "port": int(config.get("CONFIG", "FUTU_PORT")),
    }
    main_engine.add_gateway(FutuGateway, "FUTU")
    main_engine.connect(futu_setting, "FUTU")

    qmt_setting = {
        "path": config.get("CONFIG", "QMT_PATH"),
        "session_id": int(time.time()),
        "account_id": config.get("CONFIG", "QMT_ACCOUNT_ID"),
    }
    main_engine.add_gateway(QmtGateway, "QMT")
    main_engine.connect(qmt_setting, "QMT")

    # 远程通知
    notification_engine = NotificationEngine(config)
    def notify_calc(msg: str):
        print(f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} {msg}')
        notification_engine.send_telegram_message(msg)
        notification_engine.send_email(msg, msg)

    # 添加条件单配置
    condition_engine: ConditionOrderEngine = main_engine.add_engine(ConditionOrderEngine)
    condition_engine.load(config, notify_calc)

    # 阻塞主线程，等待事件处理
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        main_engine.close()


if __name__ == "__main__":
    main()
