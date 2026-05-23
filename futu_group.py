import logging
import time

import futu as ft
import pandas as pd

logger = logging.getLogger(__name__)


def _valid_price(value) -> bool:
    try:
        return value is not None and float(value) > 0
    except (TypeError, ValueError):
        return False


def sync_futu_group(
    group_name: str,
    codes: list[str],
    host: str = '127.0.0.1',
    port: int = 11111,
    price_up_list: list[float] | None = None,
    price_down_list: list[float] | None = None,
    overwrite: bool = True,
    reminder_sleep_seconds: float = 3,
) -> bool:
    """
    同步标的到指定 futu group，可选设置向上/向下到价提醒。

    Args:
        group_name: futu 自选分组名称
        codes: futu 代码列表，如 HK.00700、SH.600519
        host: futuOpenD 地址
        port: futuOpenD 端口
        price_up_list: 向上到价提醒价格列表，与 codes 对齐
        price_down_list: 向下到价提醒价格列表，与 codes 对齐
        overwrite: True 时先清空分组，再写入 codes
        reminder_sleep_seconds: 每个标的设置提醒后的等待时间

    Returns:
        bool: 分组同步是否成功
    """
    if not group_name:
        logger.warning('没有futu group名称，跳过同步')
        return False

    if codes is None:
        codes = []

    records = []
    seen = set()
    price_up_list = [] if price_up_list is None else list(price_up_list)
    price_down_list = [] if price_down_list is None else list(price_down_list)

    for idx, code in enumerate(codes):
        if code is None:
            continue
        try:
            if pd.isna(code):
                continue
        except (TypeError, ValueError):
            pass
        code = str(code).strip()
        if not code or code in seen:
            continue
        seen.add(code)
        price_up = price_up_list[idx] if idx < len(price_up_list) else None
        price_down = price_down_list[idx] if idx < len(price_down_list) else None
        records.append((code, price_up, price_down))

    quote_ctx = ft.OpenQuoteContext(host=host, port=port)
    try:
        ret, data = quote_ctx.get_user_security(group_name)
        if ret != ft.RET_OK:
            logger.error('获取%s失败 %s', group_name, data)
            return False

        if overwrite and isinstance(data, pd.DataFrame) and 'code' in data.columns:
            old_code_list = list(data['code'])
            if old_code_list:
                ret_del, data_del = quote_ctx.modify_user_security(group_name, ft.ModifyUserSecurityOp.MOVE_OUT, old_code_list)
                if ret_del == ft.RET_OK:
                    logger.info('清空%s %s个标的', group_name, len(old_code_list))
                else:
                    logger.error('清空%s失败 %s', group_name, data_del)
                    return False

        if not records:
            logger.info('同步%s为空列表', group_name)
            return True

        sync_codes = [record[0] for record in records]
        ret_add, data_add = quote_ctx.modify_user_security(group_name, ft.ModifyUserSecurityOp.ADD, sync_codes)
        if ret_add != ft.RET_OK:
            logger.error('同步%s失败 %s', group_name, data_add)
            return False

        logger.info('同步%s成功 %s个标的', group_name, len(sync_codes))

        for code, price_up, price_down in records:
            if not (_valid_price(price_up) and _valid_price(price_down)):
                continue
            if float(price_up) == float(price_down):
                continue

            ret_del, data_del = quote_ctx.set_price_reminder(code=code, op=ft.SetPriceReminderOp.DEL_ALL)
            ret_up, data_up = quote_ctx.set_price_reminder(
                code=code,
                op=ft.SetPriceReminderOp.ADD,
                reminder_type=ft.PriceReminderType.PRICE_UP,
                reminder_freq=ft.PriceReminderFreq.ONCE,
                value=float(price_up),
            )
            ret_down, data_down = quote_ctx.set_price_reminder(
                code=code,
                op=ft.SetPriceReminderOp.ADD,
                reminder_type=ft.PriceReminderType.PRICE_DOWN,
                reminder_freq=ft.PriceReminderFreq.ONCE,
                value=float(price_down),
            )
            if ret_del == ft.RET_OK and ret_up == ft.RET_OK and ret_down == ft.RET_OK:
                logger.info('%s 价格提醒 [%s,%s]', code, price_down, price_up)
            else:
                logger.error('%s 价格提醒失败 %s %s %s', code, data_del, data_up, data_down)

            if reminder_sleep_seconds > 0:
                time.sleep(reminder_sleep_seconds)

        return True
    finally:
        quote_ctx.close()
