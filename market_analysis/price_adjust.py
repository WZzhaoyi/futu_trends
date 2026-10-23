"""富途前复权 → 等比复权（分红再投资），供回测计算收益使用。

富途 A 股与场内 ETF 的前复权是加法复权：每个除权日 e 之前的价格依次做 p → p·A_e + B_e
（按除权日从早到晚），现金分红 A=1、B=−每份分红，拆合 A=比例、B=0（`get_rehab` 的
forward_adj_factorA/B）。高分红品种越早的价格被压得越低，涨跌幅被放大；美股 ETF 的
B 恒为 0，等比与前复权相同。

等比复权：先逆变换还原原始价，再对每个事件乘以因子 A + B / 除权前一日原始收盘。
"""
from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

Event = tuple[str, float, float]          # (除权日 YYYY-MM-DD, A, B)


def rehab_events(context: Any, symbol: str, as_of: date | None = None) -> list[Event]:
    """读取 Futu 复权因子，只保留除权日不晚于 as_of（默认今天）的事件，按日期升序。"""
    from futu import RET_OK

    ret, frame = context.get_rehab(symbol)
    if ret != RET_OK:
        raise RuntimeError(f'{symbol} 复权因子获取失败: {frame}')
    cutoff = (as_of or date.today()).isoformat()
    events = []
    for ex_date, a, b in zip(frame.get('ex_div_date', []), frame.get('forward_adj_factorA', []),
                             frame.get('forward_adj_factorB', [])):
        day = str(ex_date)[:10]
        a = 1.0 if pd.isna(a) else float(a)
        b = 0.0 if pd.isna(b) else float(b)
        if day <= cutoff and (a != 1.0 or b != 0.0):
            events.append((day, a, b))
    return validate_events(symbol, events)


def validate_events(symbol: str, events: Iterable[Sequence]) -> list[Event]:
    out = [(str(d)[:10], float(a), float(b)) for d, a, b in events]
    days = [d for d, _, _ in out]
    if days != sorted(days) or len(set(days)) != len(days):
        raise ValueError(f'{symbol} 复权事件须按除权日升序且不重复')
    if any(not np.isfinite([a, b]).all() or a <= 0 for _, a, b in out):
        raise ValueError(f'{symbol} 复权因子非法')
    return out


def ratio_adjust(times: Sequence, columns: dict[str, Sequence[float]], events: Sequence[Event],
                 symbol: str = '') -> dict[str, np.ndarray]:
    """把同一时间序列的前复权价格列（须含 close）转为等比复权。times 为时间戳，升序。"""
    index = pd.DatetimeIndex(pd.to_datetime(times))
    if not index.is_monotonic_increasing:
        raise ValueError(f'{symbol} 时间须升序')
    day = index.normalize()
    raw = {k: np.asarray(v, dtype=float).copy() for k, v in columns.items()}
    for ex_date, a, b in reversed(events):                 # 逆变换：先撤销最晚的事件
        before = day < pd.Timestamp(ex_date)
        for values in raw.values():
            values[before] = (values[before] - b) / a
    if any((v <= 0).any() for v in raw.values()):
        raise ValueError(f'{symbol} 还原原始价出现非正值，复权事件与行情不一致')
    closes = pd.Series(raw['close'], index=index).groupby(day).last()
    factor = np.ones(len(index))
    for ex_date, a, b in events:
        ex = pd.Timestamp(ex_date)
        prior = closes[closes.index < ex]
        if prior.empty:
            continue                                       # 数据都在除权日之后，不受影响
        factor *= np.where(day < ex, a + b / prior.iloc[-1], 1.0)
    return {k: v * factor for k, v in raw.items()}


def ratio_adjust_rows(rows: list[dict], events: Sequence[Event], symbol: str = '',
                      fields=('open', 'high', 'low', 'close')) -> list[dict]:
    """K 线字典列表（time_key + OHLC）转等比复权，返回新列表。"""
    if not rows:
        return []
    adjusted = ratio_adjust([r['time_key'] for r in rows], {f: [r[f] for r in rows] for f in fields},
                            events, symbol)
    return [dict(r, **{f: float(adjusted[f][i]) for f in fields}) for i, r in enumerate(rows)]


def ratio_adjust_frame(frame: pd.DataFrame, events: Sequence[Event], symbol: str = '',
                       fields=('Open', 'High', 'Low', 'Close')) -> pd.DataFrame:
    """DatetimeIndex 的 OHLC 表转等比复权（列名默认首字母大写），返回新表。"""
    if frame.empty:
        return frame.copy()
    renamed = {f: f.lower() for f in fields}
    adjusted = ratio_adjust(frame.index, {renamed[f]: frame[f].to_numpy() for f in fields}, events, symbol)
    out = frame.copy()
    for f in fields:
        out[f] = adjusted[renamed[f]]
    return out
