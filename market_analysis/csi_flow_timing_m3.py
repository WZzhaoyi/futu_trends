#!/usr/bin/env python3
"""中证流通择时（M3）：分层持仓策略

三层共用 M（动量启动）入场，退出按时间尺度由短到长：
  层 M1：动量转弱（z30、z60 跌到当月退出阈值）；层 X：价格破位（D ≤ −1 或 P ≤ 0.25）；
  层 HS：X 退出但趋势态成立时继续持有（H），另有峰值回撤 ATR 止损（S）。
目标仓位为三层持仓的加权和。权重为参数：预设 m3=(0.5,0.25,0.25)；m1=(1,0,0) 即 M1；m2=(0,0,1) 即 M2。

backtest: 回测，可选 ETF 面板生成执行账本；
fetch-bars: 只读拉取指数与四只 ETF 的 15 分钟历史；
live: 订阅 Futu 15 分钟 K 线，逐根输出目标仓位变化并通知。
"""
from __future__ import annotations

import argparse
import json
import math
import queue
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import csi_flow_timing as m1  # z30/z60 特征、月度阈值校准与维护
from live_runtime import EventNotifier, graceful_stop_event, managed_futu_context, runtime_file_lock, write_json_atomic

ROOT = Path(__file__).resolve().parents[1]
STRATEGY = 'm3'
VERSION = 'M3-layered-m1exit-dp-trendhold-atrvalve-etf-leader-causal'
PRESETS = {'m3': (0.5, 0.25, 0.25), 'm1': (1.0, 0.0, 0.0), 'm2': (0.0, 0.0, 1.0)}
SYMBOL = "SH.000902"
STRENGTH_SYMBOLS = ("SH.510300", "SH.510500", "SH.510050", "SH.512100")

TRAIL_K = 1.5                 # 峰值收盘回撤止损倍数（日 ATR(20)）
ENTRY_CLOCKS = ("10:30", "14:00")
EXIT_CLOCKS = ("10:00", "10:30", "14:00")
STRENGTH_N = 9
COST_BPS = 5.0
LAG_BARS = 1
MARKET_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

# 动量阈值取自 csi_flow_timing 按月发布的 threshold_YYYY-MM.json。加载时逐个校验发布口径等于
# M_THRESHOLD_SOURCE，拒绝其他策略或口径的发布；校准口径变化时须同时升级本策略版本。
M_THRESHOLD_SOURCE = "M1-LF-held-downside-exact-grid-strength-t1-defer-v2"
DEFAULT_THRESHOLDS = ROOT / 'data/csi_flow_thresholds'   # 回测默认目录；在线运行使用运行目录
INCOMPLETE_WINDOW_TOLERANCE_DAYS = 7   # 训练窗末日距月末容许的最大缺口（周末/假期）
BACKTEST_START = '2021-01-04'           # 默认评价起点：阈值训练窗需此前 9 个月 15 分钟行情（快照自 2020-01）


@dataclass(frozen=True)
class Layer:
    name: str
    exit_column: str      # build_signals 中的退出列
    trail_k: float        # 峰值回撤止损倍数；0 表示不止损


LAYERS = (Layer('M1', 'm1_exit', 0.0), Layer('X', 'regular_exit', 0.0), Layer('HS', 'exit_trend_hold', TRAIL_K))
REASONS = {'M': '动量启动', 'M1': '动量转弱', 'X': '价格破位', 'H': '趋势结束', 'S': '止损'}


# ============================ 数据 ============================
TIMES = ['09:45', '10:00', '10:15', '10:30', '10:45', '11:00', '11:15', '11:30',
         '13:15', '13:30', '13:45', '14:00', '14:15', '14:30', '14:45', '15:00']


def frame_from_rows(rows, require_turnover=True) -> pd.DataFrame:
    bars = pd.DataFrame(rows)
    if bars.empty:
        raise ValueError('没有行情')
    bars.index = pd.DatetimeIndex(pd.to_datetime(bars.time_key), name='time')
    if (bars.index.tz is not None or bars.index.hasnans or not bars.index.is_unique
            or not bars.index.is_monotonic_increasing):
        raise ValueError('需要已排序、无重复的交易所本地时间戳')
    fields = ['open', 'high', 'low', 'close'] + (['turnover'] if require_turnover else [])
    bars[fields] = bars[fields].apply(pd.to_numeric, errors='raise')
    if not np.isfinite(bars[fields]).all().all() or (bars[fields] <= 0).any().any():
        raise ValueError('存在非有限或非正价格/成交额')
    if ((bars.high < bars[['open', 'close', 'low']].max(axis=1))
            | (bars.low > bars[['open', 'close', 'high']].min(axis=1))).any():
        raise ValueError('OHLC 非法')
    days = bars.index.normalize()
    for day, group in bars.groupby(days):
        clocks = list(group.index.strftime('%H:%M'))
        exact = all(ts.second == 0 and ts.microsecond == 0 for ts in group.index)
        tail = day == days[-1] and clocks == TIMES[:len(clocks)] and len(clocks) < 16
        if day.weekday() >= 5 or not exact or (clocks != TIMES and not tail):
            raise ValueError(f'不完整或非法交易时段: {day.date()}')
    return bars


def load_data(path) -> pd.DataFrame:
    """读取指数 15 分钟快照（m1.read_snapshot 统一解析），并按交易时段校验。"""
    return frame_from_rows(m1.read_snapshot(Path(path), SYMBOL))


def schedule_for(path=None) -> dict:
    """读取月度阈值发布：训练窗必须早于生效月且带数据审计字段，口径必须为 M_THRESHOLD_SOURCE。"""
    resolved = Path(path) if path is not None else DEFAULT_THRESHOLDS
    if not resolved.exists():
        raise FileNotFoundError(f'月度阈值不存在：{resolved}')
    files = sorted(resolved.glob('threshold_*.json')) if resolved.is_dir() else [resolved]
    schedule = {}
    for item in files:
        meta = json.loads(item.read_text())
        month = meta['month']; available = date.fromisoformat(meta['available_from'])
        start = date.fromisoformat(meta['window_start']); end = date.fromisoformat(meta['window_end'])
        audit = meta['data_audit']
        first = date.fromisoformat(audit['first_training_bar'][:10])
        last = date.fromisoformat(audit['last_training_bar'][:10])
        if (meta.get('publication_kind') != 'monthly_threshold'
                or meta.get('version') != M_THRESHOLD_SOURCE
                or available.isoformat() != month + '-01'
                or not start <= first <= last <= end < available
                or (end - last).days > INCOMPLETE_WINDOW_TOLERANCE_DAYS):
            raise ValueError(f'{item}: 训练窗口、月份或策略来源非法')
        th = m1.load_threshold(item)
        if th.month != month or month in schedule:
            raise ValueError(f'{item}: 重复或不一致的阈值月份')
        if th.source != M_THRESHOLD_SOURCE or not np.isfinite(
                [th.entry_z30, th.entry_z60, th.exit_z30, th.exit_z60]).all():
            raise ValueError(f'{item}: 阈值来源或数值非法')
        schedule[month] = th
    if not schedule:
        raise ValueError('没有有效月度阈值')
    return schedule


def to_m1_bars(bars: pd.DataFrame) -> list:
    volume = bars['volume'] if 'volume' in bars else pd.Series(0.0, index=bars.index)
    turnover = bars['turnover'] if 'turnover' in bars else pd.Series(0.0, index=bars.index)
    return [m1.Bar(key=str(ts), open=float(o), high=float(h), low=float(l), close=float(c),
                       volume=float(v or 0.0), turnover=float(t or 0.0), symbol=SYMBOL)
            for ts, o, h, l, c, v, t in zip(bars.index, bars.open, bars.high, bars.low, bars.close,
                                            volume.fillna(0.0), turnover.fillna(0.0))]


def daily_atr(bars: pd.DataFrame, window=20) -> np.ndarray:
    day = bars.index.normalize()
    sessions = pd.factorize(day)[0]
    hi = bars.high.groupby(day).max(); lo = bars.low.groupby(day).min()
    cl = bars.close.groupby(day).last(); prev = cl.shift(1)
    tr = pd.concat([hi - lo, (hi - prev).abs(), (lo - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(window).mean().shift(1).reindex(cl.index).to_numpy(dtype=float)[sessions]


# ============================ 参数与文本 ============================
def resolve_weights(preset=None, weights=None) -> tuple[tuple[float, float, float], str]:
    """返回 (权重, 通知标签)。预设标签为 M1/M2/M3；显式权重为 M3(w1/w2/w3)。"""
    if preset and weights:
        raise ValueError('--preset 与 --weights 只能给一个')
    if weights:
        values = tuple(float(x) for x in (weights.split(',') if isinstance(weights, str) else weights))
        tag = 'M3(' + '/'.join(f'{w:g}' for w in values) + ')'
    else:
        preset = preset or 'm3'
        if preset not in PRESETS:
            raise ValueError(f'未知预设 {preset}')
        values, tag = PRESETS[preset], preset.upper()
    if (len(values) != len(LAYERS) or not all(math.isfinite(w) and w >= 0 for w in values)
            or abs(sum(values) - 1) > 1e-9):
        raise ValueError('权重须为三个非负数且和为 1')
    return values, tag


def format_position(x: float) -> str:
    """仓位档位：分母 ≤ 8 的分数写分数（¼ ½ ¾ 用单字），否则写整数百分比。"""
    if abs(x) < 1e-9:
        return '0'
    if abs(x - 1) < 1e-9:
        return '1'
    f = Fraction(x).limit_denominator(8)
    if abs(float(f) - x) < 1e-9:
        return {Fraction(1, 4): '¼', Fraction(1, 2): '½', Fraction(3, 4): '¾'}.get(f, f'{f.numerator}/{f.denominator}')
    return f'{round(x * 100)}%'


def format_reason(symbols) -> str:
    ordered = [s for s in REASONS if s in set(symbols)]
    return '、'.join(REASONS[s] for s in ordered) + '（' + '·'.join(ordered) + '）'


def exit_symbols(layer: Layer, normal: bool, stopped: bool) -> tuple[str, ...]:
    if layer.name != 'HS':
        return (layer.name,)
    return tuple(s for s, on in (('H', normal), ('S', stopped)) if on)


# ============================ 信号 ============================
def build_signals(bars: pd.DataFrame, schedule, m1_bars=None) -> pd.DataFrame:
    """M 入场、三层的退出条件（m1_exit、regular_exit、exit_trend_hold）与趋势态；只用已完成 K 线，回测与在线共用。"""
    day = bars.index.normalize()
    sessions = pd.factorize(day)[0]
    prev = bars.close.shift(1)
    tr = pd.concat([bars.high - bars.low, (bars.high - prev).abs(),
                    (bars.low - prev).abs()], axis=1).max(axis=1)
    atr16 = tr.rolling(16).mean().shift(1).replace(0, np.nan)
    typical = (bars.high + bars.low + bars.close) / 3
    intraday_mean = typical.groupby(day).transform(lambda s: s.expanding().mean())
    center = (bars.close - intraday_mean) / atr16
    hi8 = bars.high.shift(1).rolling(8).max()
    lo8 = bars.low.shift(1).rolling(8).min()
    channel = (bars.close - lo8) / (hi8 - lo8).replace(0, np.nan)

    lbs = m1_bars if m1_bars is not None else to_m1_bars(bars)
    feats = m1.build_features(lbs)
    if len(feats) != len(bars):
        raise ValueError('生产解析器与快照根数不一致')
    thresholds = [schedule.get(b.trading_date[:7]) for b in lbs]
    M = np.array([bool(th and f and f.z30 >= th.entry_z30 and f.z60 >= th.entry_z60)
                  for f, th in zip(feats, thresholds)])
    momentum_down = np.array([bool(th and f and f.z30 <= th.exit_z30 and f.z60 <= th.exit_z60)
                              for f, th in zip(feats, thresholds)])
    clock = bars.index.strftime('%H:%M').to_numpy()
    buy = M & np.isin(clock, ENTRY_CLOCKS)
    m1_exit = momentum_down & np.isin(clock, EXIT_CLOCKS)
    regular_exit = ((center <= -1) | (channel <= 0.25)).to_numpy() & np.isin(clock, EXIT_CLOCKS)

    daily = bars.close.groupby(day).last()
    ma20 = daily.rolling(20).mean().shift(1)
    above_ma20 = (daily.shift(1) > ma20).astype(float)
    # 盘中只能观察到此前已完成的交易日：新高标记取前 5 个交易日。
    fresh = (daily >= daily.rolling(20).max().shift(1)).astype(float).rolling(5).max().shift(1)
    strong = ((above_ma20.reindex(daily.index).to_numpy(dtype=float)[sessions] > 0.5)
              | (fresh.reindex(daily.index).to_numpy(dtype=float)[sessions] > 0.5))
    return pd.DataFrame(dict(
        z30=[f.z30 if f else np.nan for f in feats],
        z60=[f.z60 if f else np.nan for f in feats],
        D_value=center, P_value=channel, M=M,
        buy=buy, m1_exit=m1_exit, regular_exit=regular_exit, strong=strong, exit_trend_hold=regular_exit & ~strong,
        atr_daily=daily_atr(bars)), index=bars.index)


# ============================ 执行 ============================
class Executor:
    """回测与在线共用的执行状态机：下一根开盘成交、T+1 卖出延后、峰值回撤止损。

    session 为交易日序号（相邻交易日相差 1）；bar 为 K 线序号。调用顺序固定：
    每根 K 线先 open_bar（处理到期的待成交单），再 close_bar（更新峰值、决定新的待成交单）。
    """

    def __init__(self, lag=LAG_BARS, trail_k=TRAIL_K):
        if lag < 1 or trail_k < 0:
            raise ValueError('参数非法')
        self.lag, self.trail_k = lag, trail_k
        self.position = 0
        self.entered = None          # 买入成交所在交易日序号
        self.peak = None             # 持仓以来最高收盘（含买入成交根）
        self.pending = ''            # 'BUY' / 'SELL'
        self.due = None              # 待成交单最早成交的 K 线序号
        self.source = None           # 产生待成交单的信号 K 线序号

    def open_bar(self, bar, session):
        """K 线开盘：待成交单到期且满足 T+1 时成交，返回成交方向，否则返回空串。"""
        if not self.pending or bar < self.due:
            return ''
        if self.pending == 'SELL' and not session > self.entered:
            return ''
        side, self.pending = self.pending, ''
        if side == 'BUY':
            self.position, self.entered = 1, session
        else:
            self.position, self.entered, self.peak = 0, None, None
        return side

    def close_bar(self, bar, session, close, buy, sell, atr, decide=True):
        """K 线收盘：更新峰值；无待成交单时决定新的待成交单，返回其方向或空串。"""
        if self.position:
            self.peak = close if self.peak is None else max(self.peak, close)
        if self.pending or not decide:
            return ''
        stopped = bool(self.position and self.trail_k > 0 and np.isfinite(atr)
                       and close < self.peak - self.trail_k * atr)
        if not self.position and buy:
            side = 'BUY'
        elif self.position and (sell or stopped):
            side = 'SELL'
        else:
            return ''
        self.pending, self.due, self.source = side, bar + self.lag, bar
        return side

    def stop_triggered(self, close, atr):
        return bool(self.position and self.trail_k > 0 and np.isfinite(atr) and self.peak is not None
                    and close < self.peak - self.trail_k * atr)


# ============================ 回测 ============================
def simulate(op, cl, sessions, buy, exits: dict, atr, weights, cost_bps=COST_BPS, lag=LAG_BARS):
    """各层由 Executor 独立执行；账户在目标仓位变化的 K 线开盘按目标仓位×净值调整份额，当日买入份额当日不可卖。

    返回净值、持仓市值占比、目标仓位与账户成交列表（每笔含成交前后目标仓位、参与的层与原因）。
    """
    if not 0 <= cost_bps < 10000:
        raise ValueError('参数非法')
    active = [(layer, w) for layer, w in zip(LAYERS, weights) if w > 0]
    exs = {layer.name: Executor(lag, layer.trail_k) for layer, _ in active}
    why = {layer.name: () for layer, _ in active}            # 待成交单的原因符号
    n = len(op); fee = 1 - cost_bps / 10000
    eq = np.empty(n); held = np.zeros(n); target = np.zeros(n)
    cash, units, fresh, session = 1.0, 0.0, 0.0, -1
    trades = []
    for i in range(n):
        fills = []
        for layer, w in active:
            ex = exs[layer.name]; source = ex.source
            side = ex.open_bar(i, sessions[i])
            if side:
                fills.append(dict(layer=layer.name, side=side, signal=source, reasons=why[layer.name]))
            target[i] += w * ex.position
        if sessions[i] != session:
            session, fresh = sessions[i], 0.0
        before = target[i - 1] if i else 0.0
        if target[i] != before:
            value = cash + units * op[i]
            want = target[i] * value / op[i]
            if want > units + 1e-12:
                qty = want - units; cost = qty * op[i] / fee
                if cost > cash:
                    qty, cost = cash * fee / op[i], cash
                cash -= cost; units += qty; fresh += qty
            elif want < units - 1e-12:
                qty = min(units - want, units - fresh)
                if qty > 1e-12:
                    cash += qty * op[i] * fee; units -= qty
            trades.append(dict(bar=i, side='BUY' if target[i] > before else 'SELL', before=float(before),
                               after=float(target[i]), signal=max(f['signal'] for f in fills),
                               layers=[f['layer'] for f in fills],
                               reasons=sorted({s for f in fills for s in f['reasons']}, key=list(REASONS).index),
                               price=float(op[i])))
        eq[i] = cash + units * cl[i]; held[i] = units * cl[i] / eq[i]
        if i < n - 1:
            for layer, _ in active:
                ex = exs[layer.name]
                stopped = bool(layer.trail_k and ex.stop_triggered(cl[i], atr[i]))
                normal = bool(exits[layer.name][i])
                side = ex.close_bar(i, sessions[i], cl[i], buy[i], normal, atr[i])
                if side:
                    why[layer.name] = ('M',) if side == 'BUY' else exit_symbols(layer, normal, stopped)
    return eq, held, target, trades


def metrics(eq, held, trades, index, cost_bps=COST_BPS, lag=LAG_BARS) -> dict:
    daily = pd.Series(eq, index=index).groupby(index.normalize()).last()
    years = (index[-1] - index[0]).days / 365.25
    r = daily.pct_change().fillna(daily.iloc[0] - 1).to_numpy()
    downside = float(np.sqrt(np.mean(np.minimum(r, 0) ** 2)))
    mdd = float(-np.min(np.r_[1., eq] / np.maximum.accumulate(np.r_[1., eq]) - 1))
    cagr = float(eq[-1] ** (1 / years) - 1)
    return dict(version=VERSION, start=str(index[0]), end=str(index[-1]), total_return=float(eq[-1] - 1), cagr=cagr,
                max_drawdown=-mdd, calmar=cagr / mdd if mdd else None,
                sortino=float(r.mean() / downside * np.sqrt(252)) if downside else None,
                trades=len(trades), increases=sum(t['side'] == 'BUY' for t in trades),
                decreases=sum(t['side'] == 'SELL' for t in trades), exposure=float(held.mean()),
                cost_bps_per_side=cost_bps, lag_bars=lag)


def replay(bars_path, start=BACKTEST_START, end=None, thresholds=None, weights=PRESETS['m3'],
           cost_bps=COST_BPS, lag=LAG_BARS, etf_panel=None):
    """指数口径回放；给出 etf_panel 时另算 ETF 执行账本（目标仓位、成交时点与 T+1 完全相同）。"""
    bars = load_data(bars_path)
    end = str(bars.index[-1]) if end is None else end
    if pd.Timestamp(end) > bars.index[-1]:
        raise ValueError('请求的 end 超出数据范围')
    schedule = schedule_for(thresholds)
    test = bars.loc[start:end]
    if len(test) < 2 or (test.index[-1] - test.index[0]).days < 1:
        raise ValueError('回测区间不足')
    if len(bars.loc[:test.index[0]]) < 20 * 16:
        raise ValueError('至少需要20个交易日预热')
    missing = set(test.index.strftime('%Y-%m')) - set(schedule)
    if missing:
        raise ValueError(f'缺少月度阈值：{sorted(missing)}')
    sig = build_signals(bars.loc[:end], schedule)
    sel = sig.loc[test.index]
    sessions = pd.factorize(test.index.normalize())[0]
    exits = {layer.name: sel[layer.exit_column].to_numpy() for layer in LAYERS}
    eq, held, target, trades = simulate(test.open.to_numpy(), test.close.to_numpy(), sessions, sel.buy.to_numpy(),
                                        exits, sel.atr_daily.to_numpy(), weights, cost_bps, lag)
    stat = metrics(eq, held, trades, test.index, cost_bps, lag) | dict(asset='index', weights=list(weights))
    if etf_panel is None:
        etf_eq = np.full(len(test), np.nan)
        fills = [dict(symbol=SYMBOL, price=t['price']) | _fill_fields(test, t) for t in trades]
        stat['etf_execution'] = None
    else:
        etf_eq, etf_held, fills = etf_ledger(test, sessions, trades, etf_panel, cost_bps)
        stat['etf_execution'] = metrics(etf_eq, etf_held, trades, test.index, cost_bps, lag)
    columns = ['side', 'signal', 'execution_bar_end', 'execution_time', 'position_before', 'position_after',
               'layers', 'reasons', 'index_price', 'symbol', 'price', 'cash', 'units', 'ranking_observation_end']
    curve = pd.DataFrame(dict(index_equity=eq, target=target, exposure=held, etf_equity=etf_eq), index=test.index)
    return stat, pd.DataFrame(fills, columns=columns), curve, sig


def _fill_fields(test, t) -> dict:
    ts = test.index[t['bar']]
    return dict(side=t['side'], signal=str(test.index[t['signal']]), execution_bar_end=str(ts),
                execution_time=str(ts - pd.Timedelta(minutes=15)), position_before=t['before'],
                position_after=t['after'], layers='+'.join(t['layers']), reasons='·'.join(t['reasons']),
                index_price=t['price'])


def etf_ledger(test, sessions, trades, panel, cost_bps=COST_BPS):
    """ETF 执行账本：从空仓进场时用 etf_leader 选头名，此后加减仓都用这只，直到回到空仓。

    份额规则与指数账户相同；持有或成交的 ETF 缺根时报错，不补值、不回退指数。
    """
    frames = {s: frame_from_rows(panel[s], require_turnover=False) for s in STRENGTH_SYMBOLS}

    def bar_of(symbol, ts):
        if ts not in frames[symbol].index:
            raise ValueError(f'{symbol} 在 {ts} 缺根（持有或成交中），不补值、不回退指数')
        return frames[symbol].loc[ts]

    n = len(test); eq = np.empty(n); held_w = np.zeros(n)
    cash, units, fresh, session, held = 1.0, 0.0, 0.0, -1, None
    by_bar = {t['bar']: t for t in trades}; fee = 1 - cost_bps / 10000; fills = []
    for i, ts in enumerate(test.index):
        if sessions[i] != session:
            session, fresh = sessions[i], 0.0
        if i in by_bar:
            t = by_bar[i]; observation_end = None
            if held is None:
                rank = etf_leader(panel, test.index[t['signal']])
                held, observation_end = rank['ranking'][0]['symbol'], rank['observation_end']
            price = float(bar_of(held, ts)['open'])
            want = t['after'] * (cash + units * price) / price
            if want > units + 1e-12:
                qty = want - units; cost = qty * price / fee
                if cost > cash:
                    qty, cost = cash * fee / price, cash
                cash -= cost; units += qty; fresh += qty
            elif want < units - 1e-12:
                qty = min(units - want, units - fresh)
                if qty > 1e-12:
                    cash += qty * price * fee; units -= qty
            fills.append(_fill_fields(test, t) | dict(symbol=held, price=price, cash=cash, units=units,
                                                      ranking_observation_end=observation_end))
        close = float(bar_of(held, ts)['close']) if units > 1e-12 else 0.0
        eq[i] = cash + units * close; held_w[i] = units * close / eq[i]
        if units <= 1e-12:
            held = None
    return eq, held_w, fills


# ============================ ETF 执行层 ============================
def regression_score(closes, n) -> float:
    values = np.log(np.asarray(closes, dtype=float))
    x = np.arange(n) - (n - 1) / 2
    slope = float((x * (values - values.mean())).sum() / (x * x).sum())
    return float(np.expm1(slope * (n - 1)))


def etf_leader(panel: dict, signal_time) -> dict:
    """四只 ETF 按最近 N 根共同完成 K 线的对数价格回归收益排名，得分相同按 STRENGTH_SYMBOLS 顺序。"""
    rows = {s: {b['time_key']: b['close'] for b in (panel.get(s) or [])
                if b['time_key'] <= str(signal_time)} for s in STRENGTH_SYMBOLS}
    common = set.intersection(*(set(v) for v in rows.values())) if rows else set()
    keys = sorted(common)[-STRENGTH_N:]
    if len(keys) != STRENGTH_N or pd.Timestamp(keys[-1]) != pd.Timestamp(signal_time):
        raise ValueError(f'{signal_time} 前四只 ETF 共同 15 分钟根不足 N={STRENGTH_N}')
    scores = {s: regression_score([rows[s][k] for k in keys], STRENGTH_N) for s in STRENGTH_SYMBOLS}
    order = {s: i for i, s in enumerate(STRENGTH_SYMBOLS)}
    ranked = sorted(STRENGTH_SYMBOLS, key=lambda s: (-scores[s], order[s]))
    return dict(observation_end=keys[-1], ranking=[dict(rank=i + 1, symbol=s, score=scores[s])
                                                   for i, s in enumerate(ranked)])


def load_etf_panel(path) -> dict:
    obj = json.loads(Path(path).read_text())
    if set(obj['symbols']) != set(STRENGTH_SYMBOLS):
        raise ValueError('需要固定四只ETF面板')
    return {s: obj['panel'][s] for s in STRENGTH_SYMBOLS}


def input_manifest(bars_path, panel_path, thresholds):
    """回测输入清单：行情、阈值、ETF 面板与脚本的 SHA-256；同时核对各月阈值的训练行情哈希。"""
    resolved = Path(thresholds) if thresholds else DEFAULT_THRESHOLDS
    files = sorted(resolved.glob('threshold_*.json')) if resolved.is_dir() else [resolved]
    metas = [json.loads(p.read_text()) for p in files]
    bars = m1.load_bars(Path(bars_path), symbol=SYMBOL)
    for meta in metas:
        train = [b for b in bars if meta['window_start'] <= b.trading_date <= meta['window_end']]
        if m1.sha256_bars(train) != meta['data_audit']['training_bars_sha256']:
            # 行情只到某日时，训练窗超出行情范围的月份无法核对，跳过。
            if meta['window_end'] <= bars[-1].trading_date:
                raise ValueError(f"训练行情哈希不匹配: {meta['month']}")
    inputs = [Path(bars_path), *files]
    if panel_path is not None:
        inputs.append(Path(panel_path))
    return dict(version=VERSION, inputs={str(p.resolve()): m1.sha256_file(p) for p in inputs},
        implementation_sha256=m1.sha256_file(Path(__file__)),
        feature_engine_sha256=m1.sha256_file(Path(m1.__file__)),
        numpy_version=np.__version__, pandas_version=pd.__version__,
        threshold_publication_is_retrospective=any(
            m['published_at'][:10] > m['available_from'] for m in metas),
        calendar_verified=False, etf_adjustment='vendor forward-adjusted snapshot',
        limitations=['Historical rolling reconstruction; not independent out-of-sample',
            'Calendar completeness and independent vendor data not verified',
            'Fractional shares; no capacity, limit-lock, suspension or actual fill model'])


# ============================ Futu 接入 ============================
def fetch_bars(out_index: Path, out_panel: Path | None = None, start='2021-11-01',
               end=None, host='127.0.0.1', port=11111) -> dict:
    """拉指数（不复权）与四只 ETF（前复权）的 15 分钟历史。"""
    try:
        from futu import AuType, KLType, OpenQuoteContext, RET_OK
    except ImportError as exc:                                   # pragma: no cover
        raise RuntimeError('需要 futu-api（pip install futu-api）并启动 OpenD') from exc
    end = end or datetime.now(MARKET_TZ).strftime('%Y-%m-%d')
    with managed_futu_context(OpenQuoteContext, host=host, port=port) as ctx:
        index_rows = m1.pull_history(ctx, SYMBOL, start, end, AuType.NONE, KLType, RET_OK)
        panel = {}
        if out_panel is not None:
            for s in STRENGTH_SYMBOLS:
                panel[s] = m1.pull_history(ctx, s, start, end, AuType.QFQ, KLType, RET_OK)
    out_index.parent.mkdir(parents=True, exist_ok=True)
    out_index.write_text(json.dumps(dict(
        schema_version=1, source='futu.request_history_kline', symbol=SYMBOL,
        start=start, end=end, autype='NONE', fetched_at=datetime.now(MARKET_TZ).isoformat(),
        bar_time_convention='end', bars=index_rows), ensure_ascii=False))
    print(f'指数快照：{len(index_rows)} 根 → {out_index}')
    if out_panel is not None:
        out_panel.write_text(json.dumps(dict(
            source='futu.request_history_kline 15m forward-adjusted',
            symbols=list(STRENGTH_SYMBOLS), panel=panel), ensure_ascii=False))
        print(f'ETF 面板：{ {k: len(v) for k, v in panel.items()} } → {out_panel}')
    return {'index': index_rows, 'panel': panel}


# ============================ 在线运行 ============================
STATE_SCHEMA = 1
HISTORY_BARS = 1000          # Futu get_cur_kline 单次上限
MIN_SESSIONS = 26            # 20 日均线、此前 5 日新高与日 ATR(20) 所需的完整交易日
RETAINED_BARS = 1200
OLD_SESSION = -10 ** 6       # 早于保留历史的交易日：T+1 已满足
LAYER_FIELDS = ('position', 'entry_date', 'entry_bar', 'peak_close', 'pending')


def bar_key(value, shift_minutes=0) -> str:
    return m1.parse_timestamp(value, shift_minutes) + ':00'


def threshold_args(args) -> argparse.Namespace:
    """阈值维护所需参数；校准窗口固定为 M_THRESHOLD_SOURCE 对应的校准版本。"""
    return argparse.Namespace(symbol=SYMBOL, config=args.config, futu_time_convention=args.futu_time_convention,
                              window_months=m1.WINDOW_MONTHS)


class LiveState:
    """M3 在线状态：各启用层的持仓与待成交单。只记录信号与假定成交，不对应真实账户。"""

    def __init__(self, path: Path, weights, initial_layers=None, entry_date=None, held_etf=None):
        self.path, self.weights = path, list(weights)
        names = [layer.name for layer, w in zip(LAYERS, weights) if w > 0]
        if path.exists():
            raw = json.loads(path.read_text(encoding='utf-8'))
            if raw.get('strategy') != STRATEGY or raw.get('version') != VERSION:
                raise ValueError(f"状态文件 {path} 属于 {raw.get('strategy')}/{raw.get('version')}，"
                                 f'不是 {STRATEGY}/{VERSION}')
            if raw.get('schema') != STATE_SCHEMA:
                raise ValueError('状态文件 schema 不一致')
            if raw.get('weights') != self.weights:
                raise ValueError(f"状态文件权重 {raw.get('weights')} 与本次 {self.weights} 不一致")
            self.layers = raw.get('layers') or {}
            self.held_etf, self.last_decided_bar = raw.get('held_etf'), raw.get('last_decided_bar')
        else:
            flags = [0] * len(names) if initial_layers is None else list(initial_layers)
            if len(flags) != len(names) or any(f not in (0, 1) for f in flags):
                raise ValueError(f'--initial-layers 需要 {len(names)} 个 0/1（依次对应 {",".join(names)}）')
            if any(flags) and (not entry_date or held_etf not in STRENGTH_SYMBOLS):
                raise ValueError('初始持仓需要 --entry-date 与 --held-etf（四只ETF之一）')
            self.layers = {name: dict(position=f, entry_date=entry_date if f else None, entry_bar=None,
                                      peak_close=None, pending=None) for name, f in zip(names, flags)}
            self.held_etf = held_etf if any(flags) else None
            self.last_decided_bar = None
            self.save()
        if sorted(self.layers) != sorted(names):
            raise ValueError('状态文件的层与权重不一致')
        for name, ls in self.layers.items():
            if set(ls) != set(LAYER_FIELDS) or ls['position'] not in (0, 1):
                raise ValueError(f'状态文件 {name} 层无效')
            if ls['position'] and not ls['entry_date']:
                raise ValueError(f'状态文件 {name} 层缺少 entry_date')
            if ls['entry_date']:
                datetime.strptime(ls['entry_date'], '%Y-%m-%d')
            p = ls['pending']
            if p is not None and (not isinstance(p, dict) or p.get('side') not in ('BUY', 'SELL') or not p.get('signal_bar')):
                raise ValueError(f'状态文件 {name} 层 pending 无效')
        if any(ls['position'] for ls in self.layers.values()) and self.held_etf not in STRENGTH_SYMBOLS:
            raise ValueError('持仓状态缺少 held_etf')

    @property
    def pending(self) -> bool:
        return any(ls['pending'] for ls in self.layers.values())

    def save(self):
        payload = dict(strategy=STRATEGY, version=VERSION, schema=STATE_SCHEMA, weights=self.weights,
                       layers=self.layers, held_etf=self.held_etf, last_decided_bar=self.last_decided_bar,
                       updated_at=datetime.now(MARKET_TZ).isoformat(timespec='seconds'))
        write_json_atomic(self.path, payload)


class LiveEngine:
    """逐根完成 K 线决策；各层的决策与成交顺序与 simulate 完全相同：
    新一根 K 线到达 → 上一根视为完成，先 close_bar 决策，再在新一根开盘处理到期的待成交单。"""

    def __init__(self, provider, state: LiveState, emit, shift_minutes=0, tag='M3'):
        self.provider, self.state, self.emit_raw = provider, state, emit
        self.tag = tag
        self.active = [(layer, w) for layer, w in zip(LAYERS, state.weights) if w > 0]
        self.shift = shift_minutes
        self.bars: list[dict] = []
        self.current: dict | None = None
        self.strength: dict[str, dict[str, dict]] = {s: {} for s in STRENGTH_SYMBOLS}
        self.lock = threading.Lock()

    def bar_from_row(self, row) -> dict:
        get = row.get if hasattr(row, 'get') else (lambda key, default=None: default)
        bar = dict(time_key=bar_key(get('time_key', ''), self.shift),
                   **{k: float(get(k, 0) or 0) for k in ('open', 'high', 'low', 'close', 'turnover')},
                   code=str(get('code', SYMBOL)))
        if min(bar['open'], bar['high'], bar['low'], bar['close']) <= 0:
            raise ValueError(f"{bar['time_key']} 实时OHLC无效")
        return bar

    def frame(self) -> pd.DataFrame:
        """已完成 K 线；去掉首个不完整交易日后按回测同一规则校验（缺根、重复、非法价格即失败）。"""
        rows = self.bars
        first_day = rows[0]['time_key'][:10]
        if [r['time_key'][11:16] for r in rows if r['time_key'][:10] == first_day] != TIMES:
            rows = [r for r in rows if r['time_key'][:10] != first_day]
        return frame_from_rows(rows, require_turnover=False)

    @staticmethod
    def session_of(frame: pd.DataFrame):
        days = list(dict.fromkeys(frame.index.strftime('%Y-%m-%d')))
        index = {d: i for i, d in enumerate(days)}

        def lookup(day):
            if day is None:
                return None
            if day in index:
                return index[day]
            return len(days) if day > days[-1] else OLD_SESSION
        return lookup, len(days)

    def schedule(self, frame) -> dict:
        months = sorted(set(frame.index.strftime('%Y-%m')))
        current = months[-1]
        result = {current: self.provider.for_date(f'{current}-01')}   # 当月缺失直接失败
        for month in months[:-1]:
            try:
                result[month] = self.provider.for_date(f'{month}-01')
            except (ValueError, OSError):
                pass                                                    # 早于当月的阈值只影响历史行，决策不使用
        return result

    def strength_panel(self) -> dict:
        return {s: [self.strength[s][k] for k in sorted(self.strength[s])] for s in STRENGTH_SYMBOLS}

    def bootstrap_strength(self, rows_by_symbol: dict):
        for symbol in STRENGTH_SYMBOLS:
            bars = [self.bar_from_row(r) for r in rows_by_symbol[symbol]]
            if len(bars) < STRENGTH_N:
                raise ValueError(f'{symbol} 预热不足 N={STRENGTH_N}')
            self.strength[symbol] = {b['time_key']: b for b in bars[-RETAINED_BARS:]}

    def on_bar(self, bar: dict):
        with self.lock:
            if self.current is None or bar['time_key'] == self.current['time_key']:
                self.current = bar
                return
            if bar['time_key'] < self.current['time_key']:
                return
            prior, self.current = self.current, bar
            lookup = self.finalize(prior) or self.session_of(self.frame())[0]
            if self.state.pending:
                self.execute(bar, lookup)

    def on_strength_bar(self, symbol: str, bar: dict):
        with self.lock:
            book = self.strength[symbol]; book[bar['time_key']] = bar
            if len(book) > RETAINED_BARS:
                for key in sorted(book)[:-RETAINED_BARS]:
                    del book[key]

    def emit(self, event: dict):
        self.emit_raw(dict(strategy=STRATEGY, version=VERSION, symbol=SYMBOL, tag=self.tag,
                           emitted_at=datetime.now(MARKET_TZ).isoformat(timespec='seconds'), **event))

    def layer_executor(self, layer: Layer, lookup) -> Executor:
        ls = self.state.layers[layer.name]; ex = Executor(trail_k=layer.trail_k)
        ex.position = ls['position']
        ex.entered = lookup(ls['entry_date']) if ls['position'] else None
        ex.peak = ls['peak_close']
        if ls['pending']:
            ex.pending, ex.due = ls['pending']['side'], -1      # 信号根之后的任意一根均已到期
        return ex

    def position(self, intended=False) -> float:
        """目标仓位；intended 时把待成交单视为已成交。"""
        total = 0.0
        for layer, w in self.active:
            ls = self.state.layers[layer.name]; p = ls['pending']
            total += w * ((p['side'] == 'BUY') if intended and p else bool(ls['position']))
        return total

    # ---------- 预热与恢复 ----------
    def bootstrap(self, rows: list):
        bars = sorted({b['time_key']: b for b in map(self.bar_from_row, rows)}.values(), key=lambda b: b['time_key'])
        self.bars, self.current = bars[:-1], bars[-1]
        frame = self.frame()
        lookup, sessions = self.session_of(frame)
        if sessions < MIN_SESSIONS:
            raise ValueError(f'实时预热仅 {sessions} 个完整交易日，至少需要 {MIN_SESSIONS}')
        st = self.state
        signals = [ls['pending']['signal_bar'] for ls in st.layers.values() if ls['pending']]
        for b in (b for b in bars if signals and b['time_key'] > min(signals)):   # 停机期间到期的待成交单
            if not st.pending:
                break
            self.execute(b, lookup)
        for ls in st.layers.values():               # 停机期间的收盘也计入持仓峰值
            if ls['position']:
                since = ls['entry_bar'] or ls['entry_date']
                held = [b['close'] for b in self.bars if b['time_key'] >= since]
                if held:
                    ls['peak_close'] = max([ls['peak_close'] or held[0], *held])
        st.save()
        missed = [b['time_key'] for b in self.bars if st.last_decided_bar and b['time_key'] > st.last_decided_bar]
        self.emit(dict(type='READY', history_bars=len(self.bars), sessions=sessions, forming_bar=self.current['time_key'],
                       weights=st.weights, position=self.position(), layers=st.layers, held_etf=st.held_etf,
                       threshold=asdict(self.provider.for_date(self.current['time_key'][:10]))))
        if missed:
            self.emit(dict(type='ERROR', message=f'停机期间 {len(missed)} 根K线未判定（{missed[0]}—{missed[-1]}），不补发信号'))

    # ---------- 决策与假定成交 ----------
    def execute(self, bar: dict, lookup):
        st = self.state; session = lookup(bar['time_key'][:10])
        before = self.position(); fills = []
        for layer, _ in self.active:
            ls = st.layers[layer.name]; p = ls['pending']
            if not p or p['signal_bar'] >= bar['time_key']:
                continue
            side = self.layer_executor(layer, lookup).open_bar(0, session)
            if not side:
                continue
            ls['pending'] = None
            if side == 'BUY':
                ls.update(position=1, entry_date=bar['time_key'][:10], entry_bar=bar['time_key'], peak_close=None)
                st.held_etf = st.held_etf or p['etf']
            else:
                ls.update(position=0, entry_date=None, entry_bar=None, peak_close=None)
            fills.append(dict(layer=layer.name, side=side, signal_bar=p['signal_bar']))
        if not fills:
            return
        etf, after = st.held_etf, self.position()
        if after == 0 and not any(ls['pending'] and ls['pending']['side'] == 'BUY' for ls in st.layers.values()):
            st.held_etf = None
        st.save()
        self.emit(dict(type='EXECUTION_ASSUMED', execution_bar_end=bar['time_key'], index_open=bar['open'], etf=etf,
                       fills=fills, position_before=before, position_after=after, note='只更新信号状态，不发送订单'))

    def finalize(self, bar: dict):
        if self.bars and bar['time_key'] <= self.bars[-1]['time_key']:
            return None
        self.bars.append(bar); self.bars = self.bars[-RETAINED_BARS:]
        frame = self.frame()
        lookup, _ = self.session_of(frame)
        schedule = self.schedule(frame)
        sig = build_signals(frame, schedule)
        row, ts = sig.iloc[-1], frame.index[-1]
        st = self.state; day = bar['time_key'][:10]; session = lookup(day)
        before = self.position(intended=True)
        decisions = []
        for layer, w in self.active:
            ls = st.layers[layer.name]; ex = self.layer_executor(layer, lookup)
            stopped = bool(layer.trail_k and ex.stop_triggered(bar['close'], row.atr_daily))
            normal = bool(row[layer.exit_column])
            side = ex.close_bar(len(self.bars) - 1, session, bar['close'], bool(row.buy), normal, float(row.atr_daily))
            ls['peak_close'] = ex.peak
            if side:
                decisions.append(dict(layer=layer, weight=w, side=side,
                                      reasons=('M',) if side == 'BUY' else exit_symbols(layer, normal, stopped),
                                      sellable=bool(ls['entry_date'] and day > ls['entry_date'])))
        event = dict(type='SIGNAL', bar_key=bar['time_key'], execution='NEXT_BAR_OPEN', close=bar['close'],
                     signals=dict(M=bool(row.M), z30=float(row.z30), z60=float(row.z60), D=float(row.D_value),
                                  P=float(row.P_value), strong=bool(row.strong), atr_daily=float(row.atr_daily)),
                     threshold=asdict(schedule[ts.strftime('%Y-%m')]))
        ranking = None
        if any(d['side'] == 'BUY' for d in decisions) and st.held_etf is None:
            try:
                ranking = etf_leader(self.strength_panel(), ts)
            except ValueError as exc:
                decisions = [d for d in decisions if d['side'] != 'BUY']
                self.emit(dict(type='ERROR', bar_key=bar['time_key'], message=f'强度排名不可用，进场信号未发布: {exc}'))
        etf = st.held_etf or (ranking['ranking'][0]['symbol'] if ranking else None)
        for d in decisions:
            st.layers[d['layer'].name]['pending'] = dict(side=d['side'], signal_bar=bar['time_key'],
                                                         **({'etf': etf} if d['side'] == 'BUY' else {}))
        after = self.position(intended=True)
        action = 'BUY' if after > before else 'SELL' if after < before else 'NONE'
        moved = [d for d in decisions if d['side'] == action] or decisions
        reasons = sorted({s for d in moved for s in d['reasons']}, key=list(REASONS).index)
        event.update(action=action, position_before=before, position_after=after,
                     position_text=f'{format_position(before)}→{format_position(after)}',
                     layers=[d['layer'].name for d in decisions], reasons=reasons,
                     reason=format_reason(reasons) if reasons else '')
        if action == 'BUY':
            event.update(strength=ranking, held_etf=etf)
        elif action == 'SELL':
            event.update(held_etf=st.held_etf, t1_sellable=all(d['sellable'] for d in moved))
        st.last_decided_bar = bar['time_key']
        st.save()
        self.emit(event)
        return lookup


def format_event(event: dict) -> tuple[str, str]:
    tag, kind, action = event.get('tag', 'M3'), event.get('type', ''), event.get('action', '')
    label = {'BUY': '进场', 'SELL': '离场'}.get(action, action)
    subject = f"{tag} {SYMBOL} {label if kind == 'SIGNAL' else kind}"
    if kind == 'SIGNAL':
        lines = [f'[{tag}] {SYMBOL} {label}', f"信号K线: {str(event.get('bar_key', ''))[:16]}",
                 f"仓位: {format_position(event['position_before'])} → {format_position(event['position_after'])}",
                 f"原因: {event.get('reason', '')}"]
        ranked = (event.get('strength') or {}).get('ranking') or []
        if action == 'BUY' and ranked:
            lines.append('强度排名: ' + ' > '.join(f"{r['symbol']}({r['score']:.3%})" for r in ranked))
        else:
            lines.append(f"持有ETF: {event.get('held_etf')}"
                         + ('；T+1：次一交易日开盘卖出' if action == 'SELL' and not event.get('t1_sellable') else ''))
        return subject, '\n'.join(lines)
    if kind == 'ERROR':
        return subject, f"[{tag}] {SYMBOL} 实时推理错误\n{event.get('message', '')}"
    position = event.get('position')
    return subject, (f"[{tag}] {SYMBOL} {kind}\n仓位: {format_position(position) if position is not None else ''}\n"
                     f"时间: {event.get('emitted_at', '')}")


def _run_live(args, emit, runtime, script_hashes, stopped, weights, tag) -> int:
    from futu import (AuType, CurKlineHandlerBase, KLType, OpenQuoteContext, RET_ERROR, RET_OK,
                      SubType, SysNotifyHandlerBase, SysNotifyType)
    provider = m1.ThresholdDirectoryProvider(runtime.thresholds_dir, symbol=SYMBOL, window_months=m1.WINDOW_MONTHS)
    initial = [int(x) for x in args.initial_layers.split(',')] if args.initial_layers else None
    state = LiveState(runtime.state_file, weights, initial, args.entry_date, args.held_etf)
    engine = LiveEngine(provider, state, emit, 15 if args.futu_time_convention == 'start' else 0, tag)
    fatal: queue.SimpleQueue[str] = queue.SimpleQueue()
    target = m1.normalize_symbol(SYMBOL)
    strength = {m1.normalize_symbol(s): s for s in STRENGTH_SYMBOLS}

    class Handler(CurKlineHandlerBase):
        def on_recv_rsp(self, rsp_pb):
            ret, data = super().on_recv_rsp(rsp_pb)
            if ret != RET_OK:
                fatal.put(f'Futu K线回调失败: {data}')
                return RET_ERROR, data
            try:
                for i in range(len(data)):
                    bar = engine.bar_from_row(data.iloc[i]); code = m1.normalize_symbol(bar['code'])
                    if code == target:
                        engine.on_bar(bar)
                    elif code in strength:
                        engine.on_strength_bar(strength[code], bar)
            except Exception as exc:  # noqa: BLE001 - 回调线程内的任何异常都让进程退出由 PM2 重启
                fatal.put(f'实时K线处理失败: {exc!r}')
                return RET_ERROR, data
            return RET_OK, data

    class SystemHandler(SysNotifyHandlerBase):
        def on_recv_rsp(self, rsp_pb):
            ret, content = super().on_recv_rsp(rsp_pb)
            if ret != RET_OK:
                fatal.put(f'Futu 系统通知失败: {content}')
                return RET_ERROR, content
            kind, _sub, data = content
            if kind == SysNotifyType.CONN_STATUS and isinstance(data, dict) and data.get('qot_logined') is False:
                fatal.put('Futu 行情连接已断开')
            return RET_OK, content

    host, port = m1.resolve_live_connection(args.config)
    with managed_futu_context(OpenQuoteContext, host=host, port=port) as ctx:
        symbols = [SYMBOL, *STRENGTH_SYMBOLS]
        ret, msg = ctx.subscribe(symbols, [SubType.K_15M], subscribe_push=False)
        if ret != RET_OK:
            raise RuntimeError(f'订阅预热失败: {msg}')
        rows = {}
        for symbol, autype in [(s, AuType.QFQ) for s in STRENGTH_SYMBOLS] + [(SYMBOL, AuType.NONE)]:
            ret, data = ctx.get_cur_kline(symbol, args.history_bars, KLType.K_15M, autype)
            if ret != RET_OK:
                raise RuntimeError(f'获取 {symbol} 预热K线失败: {data}')
            rows[symbol] = [data.iloc[i] for i in range(len(data))]
        engine.bootstrap_strength({s: rows[s] for s in STRENGTH_SYMBOLS})
        engine.bootstrap(rows[SYMBOL])
        if ctx.set_handler(Handler()) != RET_OK or ctx.set_handler(SystemHandler()) != RET_OK:
            raise RuntimeError('注册Futu回调失败')
        ret, msg = ctx.subscribe(symbols, [SubType.K_15M], subscribe_push=True)
        if ret != RET_OK:
            raise RuntimeError(f'开启K线推送失败: {msg}')
        deadline = time.monotonic() + args.duration if args.duration > 0 else None
        ensured = m1.current_market_month().strftime('%Y-%m')
        next_maintenance = time.monotonic() + m1.LIVE_MAINTENANCE_INTERVAL_SECONDS
        while not stopped.wait(1.0):
            if not fatal.empty():
                raise RuntimeError(fatal.get_nowait())
            if {p: m1.sha256_file(p) for p in script_hashes} != script_hashes:
                raise RuntimeError('脚本文件在 live 运行期间发生变化；退出并由PM2重启')
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                break
            month = m1.current_market_month().strftime('%Y-%m')
            if month != ensured and now >= next_maintenance:
                try:
                    ensured, generated = m1.ensure_live_threshold(threshold_args(args), runtime)
                    engine.emit(dict(type='THRESHOLD_READY', month=ensured, generated=generated))
                except Exception as exc:  # noqa: BLE001 - 下个维护周期重试
                    engine.emit(dict(type='ERROR', message=f'{month} 自动更新阈值失败，将重试: {exc!r}'))
                next_maintenance = now + m1.LIVE_MAINTENANCE_INTERVAL_SECONDS
    engine.emit(dict(type='STOPPED', position=engine.position()))
    return 0


def run_live(args) -> int:
    if m1.VERSION != M_THRESHOLD_SOURCE:
        raise RuntimeError(f'M1 校准版本 {m1.VERSION} 与 M3 绑定的 {M_THRESHOLD_SOURCE} 不一致；需要显式升级版本')
    weights, tag = resolve_weights(args.preset, args.weights)
    if args.weights:
        raise ValueError('live 只接受 --preset；显式权重只用于回测')
    notifier = None
    if args.config and not args.no_notify:
        import configparser
        sys.path.insert(0, str(ROOT))
        from notification_engine import NotificationEngine
        config = configparser.ConfigParser()
        if not config.read(Path(args.config).resolve(), encoding='utf-8'):
            raise ValueError(f'通知配置文件不存在或不可读: {args.config}')
        notifier = EventNotifier(NotificationEngine(config), format_event, name=f'{tag.lower()}-live-notifier',
                                 sheet_strategy=None if args.no_sheet else STRATEGY,
                                 notify_lifecycle=args.notify_lifecycle)

    def emit(event):
        print(json.dumps(event, ensure_ascii=False, default=float), flush=True)
        if notifier is not None:
            notifier.notify(event)

    def stamp(**event):
        return dict(strategy=STRATEGY, version=VERSION, symbol=SYMBOL, tag=tag,
                    emitted_at=datetime.now(MARKET_TZ).isoformat(timespec='seconds'), **event)
    try:
        runtime = m1.LiveRuntimePaths.from_argument(args.runtime_dir)
        runtime.prepare()
        with graceful_stop_event() as stopped, runtime_file_lock(runtime.lock_file):
            scripts = [Path(__file__).resolve(), Path(m1.__file__).resolve()]
            hashes = {p: m1.sha256_file(p) for p in scripts}
            month, generated = m1.ensure_live_threshold(threshold_args(args), runtime)
            emit(stamp(type='THRESHOLD_READY', month=month, generated=generated))
            if stopped.is_set():
                return 0
            return _run_live(args, emit, runtime, hashes, stopped, weights, tag)
    except Exception as exc:
        emit(stamp(type='ERROR', message=repr(exc)))
        raise
    finally:
        if notifier is not None:
            notifier.close()


def cli(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('fetch-bars', help='只读获取指数和ETF历史行情（同 M2）')
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--etf-out', type=Path, default=None, help='省略则只拉指数')
    p.add_argument('--start', default='2021-11-01'); p.add_argument('--end', default=None)
    p.add_argument('--host', default='127.0.0.1'); p.add_argument('--port', type=int, default=11111)
    for name, text in (('backtest', '指数口径回测；给出 --etf-panel 时另算ETF执行账本'),
                       ('live', '连接Futu OpenD，逐根输出目标仓位变化并通知；不下单')):
        p = sub.add_parser(name, help=text)
        p.add_argument('--preset', choices=tuple(PRESETS), default=None, help='权重预设，默认 m3')
        if name == 'backtest':
            p.add_argument('--weights', default=None, help='显式权重 w1,w2,w3（与 --preset 互斥，只用于回测）')
            p.add_argument('--bars', type=Path, required=True)
            p.add_argument('--etf-panel', type=Path, default=None)
            p.add_argument('--start', default=BACKTEST_START); p.add_argument('--end', default=None)
            p.add_argument('--thresholds', type=Path, default=None)
            p.add_argument('--cost-bps', type=float, default=COST_BPS)
            p.add_argument('--lag', type=int, default=LAG_BARS)
            p.add_argument('--output', type=Path, required=True)
        else:
            p.set_defaults(weights=None)
            p.add_argument('--runtime-dir', required=True, help='绝对路径；保存月度阈值、校准行情、状态与实例锁')
            p.add_argument('--config', help='项目config.ini：Futu连接与通知渠道')
            p.add_argument('--history-bars', type=int, default=HISTORY_BARS)
            p.add_argument('--initial-layers', default=None, help='各启用层的初始持仓 0/1，逗号分隔（m3 为三项，m1/m2 为一项）')
            p.add_argument('--entry-date'); p.add_argument('--held-etf', choices=STRENGTH_SYMBOLS)
            p.add_argument('--futu-time-convention', choices=('end', 'start'), default='end')
            p.add_argument('--duration', type=int, default=0, help='秒；0表示持续运行')
            p.add_argument('--notify-lifecycle', action='store_true')
            p.add_argument('--no-sheet', action='store_true', help='不写共享表')
            p.add_argument('--no-notify', action='store_true', help='只输出日志，不发通知')
    args = ap.parse_args(argv)
    if args.cmd == 'live':
        return run_live(args)
    if args.cmd == 'fetch-bars':
        fetch_bars(args.out, args.etf_out, args.start, args.end, args.host, args.port)
        return 0
    weights, tag = resolve_weights(args.preset, args.weights)
    panel = None if args.etf_panel is None else load_etf_panel(args.etf_panel)
    manifest = input_manifest(args.bars, args.etf_panel, args.thresholds)
    stat, trades, curve, sig = replay(args.bars, args.start, args.end, args.thresholds, weights,
                                      args.cost_bps, args.lag, panel)
    stat['tag'] = tag
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'metrics.json').write_text(json.dumps(stat, ensure_ascii=False, indent=2))
    (args.output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    trades.to_csv(args.output / 'trades.csv', index=False)
    curve.to_csv(args.output / 'equity.csv', index_label='time')
    sig.to_csv(args.output / 'signals.csv', index_label='time')
    print(json.dumps(stat, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(cli())
