#!/usr/bin/env python3
"""中证流通 M2 择时：回测与在线信号，不下单。

backtest: 指数口径回测，可选 ETF 面板生成执行账本；导出成交、净值、信号与输入哈希。
fetch-bars: 只读拉取 Futu 历史 15 分钟行情（指数不复权、ETF 前复权）。
live: 订阅 Futu 15 分钟 K 线推送，用与回测相同的 build_signals 与 Executor 生成信号并通知。
月度 M 阈值来自 M1 的校准发布。
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
import queue
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import csi_flow_timing as m1  # M1：M 特征、月度阈值的校准与发布
from live_runtime import (
    EventNotifier,
    graceful_stop_event,
    managed_futu_context,
    runtime_file_lock,
    write_json_atomic,
)

ROOT = Path(__file__).resolve().parents[1]
VERSION = "M2-m-only-trendhold-atrvalve-nocool-etf-leader-causal"
SYMBOL = "SH.000902"
STRENGTH_SYMBOLS = ("SH.510300", "SH.510500", "SH.510050", "SH.512100")

TRAIL_K = 1.5                 # 峰值收盘回撤止损倍数（日 ATR(20)）
ENTRY_CLOCKS = ("10:30", "14:00")
EXIT_CLOCKS = ("10:00", "10:30", "14:00")
STRENGTH_N = 9
COST_BPS = 5.0
LAG_BARS = 1
MARKET_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


# M 票阈值取自 M1 按月发布的 threshold_YYYY-MM.json。加载时逐个校验发布口径等于
# M_THRESHOLD_SOURCE，拒绝其他策略或口径的发布；M1 校准口径变化时须同时升级本策略版本。
# 阈值由 M1 校准生成，不手工修改。
M_THRESHOLD_SOURCE = "M1-LF-held-downside-exact-grid-strength-t1-defer-v2"
DEFAULT_THRESHOLDS = ROOT / 'data/csi_flow_thresholds'   # 回测默认目录；在线运行使用运行目录
INCOMPLETE_WINDOW_TOLERANCE_DAYS = 7   # 训练窗末日距月末容许的最大缺口（周末/假期）
BACKTEST_START = '2021-01-04'           # 默认评价起点：阈值训练窗需此前 9 个月 15 分钟行情（快照自 2020-01）



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
    obj = json.loads(Path(path).read_text())
    if (obj.get('symbol') != SYMBOL or obj.get('bar_time_convention') != 'end'
            or obj.get('autype') != 'NONE'):
        raise ValueError(f'需要 {SYMBOL}、end 标记、autype=NONE 的 15 分钟快照')
    return frame_from_rows(obj['bars'])


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
        parsed = m1.load_threshold_schedule(item)
        if set(parsed) != {month} or month in schedule:
            raise ValueError(f'{item}: 重复或不一致的阈值月份')
        th = parsed[month]
        if th.source != M_THRESHOLD_SOURCE or not np.isfinite(
                [th.entry_z30, th.entry_z60, th.exit_z30, th.exit_z60]).all():
            raise ValueError(f'{item}: 阈值来源或数值非法')
        schedule.update(parsed)
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


# ============================ 信号 ============================
def build_signals(bars: pd.DataFrame, schedule, m1_bars=None) -> pd.DataFrame:
    """M 票与入场、D/P 普通退出、多头趋势态；只用已完成 K 线，回测与在线共用。"""
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
    M = np.array([bool(th and f and f.z30 >= th.entry_z30 and f.z60 >= th.entry_z60)
                  for f, th in zip(feats, (schedule.get(b.trading_date[:7]) for b in lbs))])
    clock = bars.index.strftime('%H:%M').to_numpy()
    buy = M & np.isin(clock, ENTRY_CLOCKS)
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
        buy=buy, regular_exit=regular_exit, strong=strong, exit_trend_hold=regular_exit & ~strong,
        atr_daily=daily_atr(bars)), index=bars.index)


# ============================ 执行 / 回测 ============================
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


def simulate(op, cl, sessions, buy, sell, atr, cost_bps=COST_BPS, lag=LAG_BARS, trail_k=TRAIL_K):
    """二元全仓回放，规则由 Executor 执行；返回净值、仓位与成交 (side, 信号根, 成交根, 开盘价)。"""
    if not 0 <= cost_bps < 10000:
        raise ValueError('参数非法')
    ex = Executor(lag, trail_k)
    eq = np.empty(len(op)); pos = np.zeros(len(op), int)
    cash, units, trades = 1.0, 0.0, []
    fee = 1 - cost_bps / 10000
    for i in range(len(op)):
        source = ex.source
        side = ex.open_bar(i, sessions[i])
        if side == 'BUY':
            units, cash = cash * fee / op[i], 0.0
        elif side == 'SELL':
            cash, units = units * op[i] * fee, 0.0
        if side:
            trades.append((side, source, i, float(op[i])))
        pos[i] = ex.position; eq[i] = cash + units * cl[i]
        ex.close_bar(i, sessions[i], cl[i], buy[i], sell[i], atr[i], decide=i < len(op) - 1)
    return eq, pos, trades


def metrics(eq, pos, trades, sessions, index, cost_bps=COST_BPS, lag=LAG_BARS) -> dict:
    daily = pd.Series(eq, index=index).groupby(index.normalize()).last()
    years = (index[-1] - index[0]).days / 365.25
    dl = np.diff(np.r_[0.0, np.log(daily)])
    complete = dl[:-1] if index[-1].strftime('%H:%M') != '15:00' else dl
    holds = [sessions[e[2]] - sessions[b[2]]
             for b, e in zip([t for t in trades if t[0] == 'BUY'],
                             [t for t in trades if t[0] == 'SELL'])]
    return dict(version=VERSION, start=str(index[0]), end=str(index[-1]),
                total_return=float(eq[-1] - 1), cagr=float(eq[-1] ** (1 / years) - 1),
                max_drawdown=float(np.min(np.r_[1., eq] / np.maximum.accumulate(np.r_[1., eq]) - 1)),
                buys=sum(t[0] == 'BUY' for t in trades), sells=sum(t[0] == 'SELL' for t in trades),
                exposure=float(pos.mean()),
                avg_hold_sessions=round(float(np.mean(holds)), 2) if holds else 0.0,
                worst_complete_day=float(np.expm1(complete.min())),
                cost_bps_per_side=cost_bps, lag_bars=lag)


def replay(bars_path, start=BACKTEST_START, end=None, thresholds=None,
           cost_bps=COST_BPS, lag=LAG_BARS, etf_panel=None):
    """指数口径回放；给出 etf_panel 时另算 ETF 执行账本（信号、成交时点与 T+1 完全相同）。"""
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
    index_eq, pos, trades = simulate(test.open.to_numpy(), test.close.to_numpy(), sessions,
        sel.buy.to_numpy(), sel.exit_trend_hold.to_numpy(), sel.atr_daily.to_numpy(), cost_bps, lag)
    stat = metrics(index_eq, pos, trades, sessions, test.index, cost_bps, lag)
    stat['asset'] = 'index'
    if etf_panel is None:
        etf_eq, fills = np.full(len(test), np.nan), [
            dict(side=side, signal=str(test.index[src]), execution_bar_end=str(test.index[ex]),
                 execution_time=str(test.index[ex] - pd.Timedelta(minutes=15)),
                 index_price=price, symbol=SYMBOL, price=price)
            for side, src, ex, price in trades]
        stat['etf_execution'] = None
    else:
        etf_eq, fills = etf_ledger(test, trades, etf_panel, cost_bps)
        stat['etf_execution'] = metrics(etf_eq, pos, trades, sessions, test.index, cost_bps, lag)
    columns = ['side', 'signal', 'execution_bar_end', 'execution_time', 'index_price',
               'symbol', 'price', 'cash', 'units', 'ranking_observation_end']
    curve = pd.DataFrame(dict(index_equity=index_eq, position=pos, etf_equity=etf_eq), index=test.index)
    return stat, pd.DataFrame(fills, columns=columns), curve, sig


def etf_ledger(test, trades, panel, cost_bps=COST_BPS):
    """ETF 执行模块：入场时用 etf_leader 选头名并持有至卖出；成交取该 ETF 执行根开盘价。

    只要求持有中或当根成交的 ETF 有该根行情；未持有的 ETF 停牌缺根不影响账本。
    持有或成交的 ETF 缺根时报错，不补值、不回退指数。
    """
    frames = {s: frame_from_rows(panel[s], require_turnover=False) for s in STRENGTH_SYMBOLS}

    def bar_of(symbol, ts):
        if ts not in frames[symbol].index:
            raise ValueError(f'{symbol} 在 {ts} 缺根（持有或成交中），不补值、不回退指数')
        return frames[symbol].loc[ts]

    eq = np.empty(len(test)); cash, units, held = 1., 0., None
    by_bar = {t[2]: t for t in trades}; fee = 1 - cost_bps / 10000; fills = []
    for i, ts in enumerate(test.index):
        if i in by_bar:
            side, src, _, index_price = by_bar[i]
            observation_end = None
            if side == 'BUY':
                rank = etf_leader(panel, test.index[src])
                held, observation_end = rank['ranking'][0]['symbol'], rank['observation_end']
                price = float(bar_of(held, ts)['open'])
                units, cash = cash * fee / price, 0.
            else:
                price = float(bar_of(held, ts)['open'])
                cash, units = units * price * fee, 0.
            fills.append(dict(side=side, signal=str(test.index[src]), execution_bar_end=str(ts),
                              execution_time=str(ts - pd.Timedelta(minutes=15)), index_price=index_price,
                              symbol=held, price=price, cash=cash, units=units,
                              ranking_observation_end=observation_end))
        eq[i] = cash + (units * bar_of(held, ts)['close'] if units else 0.)
    return eq, fills


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


# ============================ Futu 接入 ============================
def _futu_api():
    try:
        from futu import AuType, KLType, OpenQuoteContext, RET_OK, SubType
    except ImportError as exc:                                   # pragma: no cover
        raise RuntimeError('需要 futu-api（pip install futu-api）并启动 OpenD') from exc
    return AuType, KLType, OpenQuoteContext, RET_OK, SubType


def _pull_history(ctx, code, start, end, autype, KLType, RET_OK, count=1000) -> list:
    rows, key = [], None
    while True:
        ret, data, key = ctx.request_history_kline(
            code, start=start, end=end, ktype=KLType.K_15M, autype=autype,
            max_count=count, page_req_key=key)
        if ret != RET_OK:
            raise RuntimeError(f'{code} 历史 K 线获取失败：{data}')
        keep = [c for c in ('time_key', 'open', 'high', 'low', 'close', 'volume', 'turnover')
                if c in data.columns]
        rows.extend(data[keep].to_dict('records'))
        if key is None:
            break
    return rows


def fetch_bars(out_index: Path, out_panel: Path | None = None, start='2021-11-01',
               end=None, host='127.0.0.1', port=11111) -> dict:
    """拉指数（不复权）与四只 ETF（前复权）的 15 分钟历史。"""
    AuType, KLType, OpenQuoteContext, RET_OK, _ = _futu_api()
    end = end or datetime.now(MARKET_TZ).strftime('%Y-%m-%d')
    with OpenQuoteContext(host=host, port=port) as ctx:
        index_rows = _pull_history(ctx, SYMBOL, start, end, AuType.NONE, KLType, RET_OK)
        panel = {}
        if out_panel is not None:
            for s in STRENGTH_SYMBOLS:
                panel[s] = _pull_history(ctx, s, start, end, AuType.QFQ, KLType, RET_OK)
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
STRATEGY = 'm2'
STATE_SCHEMA = 1
HISTORY_BARS = 1000          # Futu get_cur_kline 单次上限
MIN_SESSIONS = 26            # 20 日均线、此前 5 日新高与日 ATR(20) 所需的完整交易日
RETAINED_BARS = 1200
OLD_SESSION = -10 ** 6       # 早于保留历史的交易日：T+1 已满足


def bar_key(value, shift_minutes=0) -> str:
    return m1.parse_timestamp(value, shift_minutes) + ':00'


class LiveState:
    """M2 在线状态。只记录信号与假定成交，不对应真实账户。"""

    FIELDS = ('position', 'entry_date', 'entry_bar', 'held_etf', 'peak_close', 'exit_date',
              'pending', 'last_decided_bar')

    def __init__(self, path: Path, initial_position='flat', entry_date=None, held_etf=None):
        self.path = path
        if path.exists():
            raw = json.loads(path.read_text(encoding='utf-8'))
            if raw.get('strategy') != STRATEGY or raw.get('version') != VERSION:
                raise ValueError(f"状态文件 {path} 属于 {raw.get('strategy')}/{raw.get('version')}，"
                                 f'不是 {STRATEGY}/{VERSION}')
            if raw.get('schema') != STATE_SCHEMA:
                raise ValueError('状态文件 schema 不一致')
            for name in self.FIELDS:
                setattr(self, name, raw.get(name))
        else:
            self.position = 1 if initial_position == 'long' else 0
            self.entry_date = entry_date if self.position else None
            self.held_etf = held_etf if self.position else None
            self.entry_bar = self.peak_close = self.exit_date = self.pending = self.last_decided_bar = None
            if self.position and (not entry_date or held_etf not in STRENGTH_SYMBOLS):
                raise ValueError('--initial-position long 需要 --entry-date 与 --held-etf（四只ETF之一）')
            self.save()
        if self.position not in (0, 1):
            raise ValueError('状态文件 position 只能是0或1')
        if self.position and (not self.entry_date or self.held_etf not in STRENGTH_SYMBOLS):
            raise ValueError('持仓状态缺少 entry_date 或 held_etf')
        if self.pending is not None and (not isinstance(self.pending, dict)
                                         or self.pending.get('side') not in ('BUY', 'SELL')
                                         or not self.pending.get('signal_bar')):
            raise ValueError('状态文件 pending 无效')
        for name in ('entry_date', 'exit_date'):
            if getattr(self, name):
                date.fromisoformat(getattr(self, name))

    def save(self):
        payload = dict(strategy=STRATEGY, version=VERSION, schema=STATE_SCHEMA,
                       **{name: getattr(self, name) for name in self.FIELDS},
                       updated_at=datetime.now(MARKET_TZ).isoformat(timespec='seconds'))
        write_json_atomic(self.path, payload)


class LiveEngine:
    """逐根完成 K 线决策；决策与成交的顺序与 simulate 完全相同：
    新一根 K 线到达 → 上一根视为完成，先 close_bar 决策，再在新一根开盘处理到期的待成交单。"""

    def __init__(self, provider, state: LiveState, emit, shift_minutes=0):
        self.provider, self.state, self.emit_raw = provider, state, emit
        self.shift = shift_minutes
        self.bars: list[dict] = []
        self.current: dict | None = None
        self.strength: dict[str, dict[str, dict]] = {s: {} for s in STRENGTH_SYMBOLS}
        self.lock = threading.Lock()

    # ---------- 行情 ----------
    def bar_from_row(self, row) -> dict:
        get = row.get if hasattr(row, 'get') else (lambda key, default=None: default)
        bar = dict(time_key=bar_key(get('time_key', ''), self.shift),
                   **{k: float(get(k, 0) or 0) for k in ('open', 'high', 'low', 'close', 'turnover')},
                   code=str(get('code', SYMBOL)))
        if min(bar['open'], bar['high'], bar['low'], bar['close']) <= 0:
            raise ValueError(f"{bar['time_key']} 实时OHLC无效")
        return bar

    def emit(self, event: dict):
        self.emit_raw(dict(strategy=STRATEGY, version=VERSION, symbol=SYMBOL,
                           emitted_at=datetime.now(MARKET_TZ).isoformat(timespec='seconds'), **event))

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

    def executor(self, lookup) -> Executor:
        st = self.state; ex = Executor()
        ex.position = st.position
        ex.entered = lookup(st.entry_date) if st.position else None
        ex.peak = st.peak_close
        if st.pending:
            ex.pending, ex.due = st.pending['side'], -1      # 信号根之后的任意一根均已到期
        return ex

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

    # ---------- 预热与恢复 ----------
    def bootstrap_strength(self, rows_by_symbol: dict):
        for symbol in STRENGTH_SYMBOLS:
            bars = [self.bar_from_row(r) for r in rows_by_symbol[symbol]]
            if len(bars) < STRENGTH_N:
                raise ValueError(f'{symbol} 预热不足 N={STRENGTH_N}')
            self.strength[symbol] = {b['time_key']: b for b in bars[-RETAINED_BARS:]}

    def bootstrap(self, rows: list):
        bars = sorted({b['time_key']: b for b in map(self.bar_from_row, rows)}.values(), key=lambda b: b['time_key'])
        self.bars, self.current = bars[:-1], bars[-1]
        frame = self.frame()
        lookup, sessions = self.session_of(frame)
        if sessions < MIN_SESSIONS:
            raise ValueError(f'实时预热仅 {sessions} 个完整交易日，至少需要 {MIN_SESSIONS}')
        st = self.state
        later = [b for b in bars if st.pending and b['time_key'] > st.pending['signal_bar']]
        for b in later:                      # 停机期间到期的待成交单：按顺序在其后各根开盘重试
            if not st.pending:
                break
            self.execute(b, lookup)
        if st.position:                      # 停机期间的收盘也计入持仓峰值
            since = st.entry_bar or st.entry_date          # 初始持仓只有入场日期
            held = [b['close'] for b in self.bars if b['time_key'] >= since]
            if held:
                st.peak_close = max([st.peak_close or held[0], *held])
            st.save()
        missed = [b['time_key'] for b in self.bars if st.last_decided_bar and b['time_key'] > st.last_decided_bar]
        self.emit(dict(type='READY', history_bars=len(self.bars), sessions=sessions, forming_bar=self.current['time_key'],
                       position=st.position, held_etf=st.held_etf, pending=st.pending, peak_close=st.peak_close,
                       threshold=asdict(self.provider.for_date(self.current['time_key'][:10]))))
        if missed:
            self.emit(dict(type='ERROR', message=f'停机期间 {len(missed)} 根K线未判定（{missed[0]}—{missed[-1]}），不补发信号'))

    # ---------- 决策与假定成交 ----------
    def execute(self, bar: dict, lookup):
        st = self.state
        ex = self.executor(lookup)
        side = ex.open_bar(0, lookup(bar['time_key'][:10]))
        if not side:
            return
        pending = st.pending; st.pending = None
        if side == 'BUY':
            st.position, st.entry_date, st.entry_bar = 1, bar['time_key'][:10], bar['time_key']
            st.held_etf, st.peak_close = pending['etf'], None
        else:
            held = st.held_etf
            st.position, st.entry_date, st.entry_bar, st.held_etf, st.peak_close = 0, None, None, None, None
            st.exit_date = bar['time_key'][:10]
        st.save()
        self.emit(dict(type='EXECUTION_ASSUMED', side=side, signal_bar=pending['signal_bar'],
                       execution_bar_end=bar['time_key'], index_open=bar['open'],
                       etf=st.held_etf if side == 'BUY' else held, position=st.position,
                       note='只更新信号状态，不发送订单'))

    def finalize(self, bar: dict):
        if self.bars and bar['time_key'] <= self.bars[-1]['time_key']:
            return None
        self.bars.append(bar); self.bars = self.bars[-RETAINED_BARS:]
        frame = self.frame()
        lookup, _ = self.session_of(frame)
        schedule = self.schedule(frame)
        sig = build_signals(frame, schedule)
        row, ts = sig.iloc[-1], frame.index[-1]
        st = self.state
        ex = self.executor(lookup)
        position_before = st.position
        stop = ex.position and ex.stop_triggered(bar['close'], row.atr_daily)
        side = ex.close_bar(len(self.bars) - 1, lookup(bar['time_key'][:10]), bar['close'],
                            bool(row.buy), bool(row.exit_trend_hold), float(row.atr_daily))
        st.peak_close = ex.peak
        event = dict(type='SIGNAL', bar_key=bar['time_key'], action=side or 'NONE', position_before=position_before,
                     execution='NEXT_BAR_OPEN', close=bar['close'],
                     signals=dict(M=bool(row.M), D=float(row.D_value), P=float(row.P_value), z30=float(row.z30),
                                  z60=float(row.z60), strong=bool(row.strong), atr_daily=float(row.atr_daily),
                                  peak_close=ex.peak),
                     threshold=asdict(schedule[ts.strftime('%Y-%m')]))
        if side == 'BUY':
            try:
                ranking = etf_leader(self.strength_panel(), ts)
            except ValueError as exc:
                st.last_decided_bar = bar['time_key']; st.save()
                self.emit(dict(type='ERROR', bar_key=bar['time_key'], message=f'强度排名不可用，BUY信号未发布: {exc}'))
                return lookup
            event.update(strength=ranking, reason='M成立（z30、z60达到当月阈值）')
            st.pending = dict(side='BUY', signal_bar=bar['time_key'], etf=ranking['ranking'][0]['symbol'])
        elif side == 'SELL':
            normal = bool(row.exit_trend_hold)
            event.update(reason='ATR保护与普通退出同时触发' if stop and normal else 'ATR保护退出' if stop else '普通退出（非趋势态）',
                         held_etf=st.held_etf, t1_sellable=bool(st.entry_date and bar['time_key'][:10] > st.entry_date))
            st.pending = dict(side='SELL', signal_bar=bar['time_key'])
        st.last_decided_bar = bar['time_key']
        st.save()
        self.emit(event)
        return lookup

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


def format_event(event: dict, tag='M2') -> tuple[str, str]:
    kind, action = event.get('type', ''), event.get('action', '')
    subject = f"{tag} {SYMBOL} {action if kind == 'SIGNAL' else kind}"
    if kind == 'SIGNAL':
        sig = event.get('signals') or {}
        lines = [f'[{tag}] {SYMBOL} {action}', f"信号K线: {event.get('bar_key')}（下一根开盘执行）",
                 f"触发: {event.get('reason', '')}", f"阈值月份: {(event.get('threshold') or {}).get('month', '')}",
                 f"z30/z60: {sig.get('z30', float('nan')):.3f} / {sig.get('z60', float('nan')):.3f}；"
                 f"D={sig.get('D', float('nan')):.2f} P={sig.get('P', float('nan')):.2f}；多头趋势态={sig.get('strong')}",
                 f"信号前仓位: {event.get('position_before')}"]
        if action == 'BUY':
            ranked = (event.get('strength') or {}).get('ranking') or []
            lines.append('强度排名: ' + ' > '.join(f"{r['symbol']}({r['score']:.3%})" for r in ranked))
        else:
            lines.append(f"持有ETF: {event.get('held_etf')}" + ('' if event.get('t1_sellable') else '；T+1：次一交易日开盘卖出'))
        return subject, '\n'.join(lines)
    if kind == 'ERROR':
        return subject, f"[{tag}] {SYMBOL} 实时推理错误\n{event.get('message', '')}"
    return subject, (f"[{tag}] {SYMBOL} {kind}\n仓位: {event.get('position')}\n"
                     f"时间: {event.get('emitted_at', '')}")


def threshold_args(args) -> argparse.Namespace:
    """M1 阈值维护所需参数；窗口与 T+1 口径固定为 M_THRESHOLD_SOURCE 对应的 M1 版本。"""
    return argparse.Namespace(symbol=SYMBOL, config=args.config, futu_time_convention=args.futu_time_convention,
                              window_months=m1.WINDOW_MONTHS, t1_sell_mode=m1.T1_SELL_MODE)


def _run_live(args, emit, runtime, script_hashes, stopped) -> int:
    from futu import (AuType, CurKlineHandlerBase, KLType, OpenQuoteContext, RET_ERROR, RET_OK,
                      SubType, SysNotifyHandlerBase, SysNotifyType)
    provider = m1.ThresholdDirectoryProvider(runtime.thresholds_dir, symbol=SYMBOL, allow_freeze=False,
                                                 window_months=m1.WINDOW_MONTHS,
                                                 t1_sell_mode=m1.T1_SELL_MODE)
    state = LiveState(runtime.state_file, args.initial_position, args.entry_date, args.held_etf)
    engine = LiveEngine(provider, state, emit, 15 if args.futu_time_convention == 'start' else 0)
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
                    row = data.iloc[i]
                    bar = engine.bar_from_row(row); code = m1.normalize_symbol(bar['code'])
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
    engine.emit(dict(type='STOPPED', position=state.position))
    return 0


def run_live(args) -> int:
    if m1.VERSION != M_THRESHOLD_SOURCE:
        raise RuntimeError(f'M1 校准版本 {m1.VERSION} 与 M2 绑定的 {M_THRESHOLD_SOURCE} 不一致；'
                           '需要显式升级 M2 版本')
    notifier = None
    if args.config and not args.no_notify:
        import configparser
        sys.path.insert(0, str(ROOT))
        from notification_engine import NotificationEngine
        config = configparser.ConfigParser()
        if not config.read(Path(args.config).resolve(), encoding='utf-8'):
            raise ValueError(f'通知配置文件不存在或不可读: {args.config}')
        notifier = EventNotifier(NotificationEngine(config), format_event,
                                 name='m2-live-notifier', sheet_strategy=None if args.no_sheet else STRATEGY,
                                 notify_lifecycle=args.notify_lifecycle)

    def emit(event):
        print(json.dumps(event, ensure_ascii=False, default=float), flush=True)
        if notifier is not None:
            notifier.notify(event)
    try:
        runtime = m1.LiveRuntimePaths.from_argument(args.runtime_dir)
        runtime.prepare()
        with graceful_stop_event() as stopped, runtime_file_lock(runtime.lock_file):
            scripts = [Path(__file__).resolve(), Path(m1.__file__).resolve()]
            hashes = {p: m1.sha256_file(p) for p in scripts}
            month, generated = m1.ensure_live_threshold(threshold_args(args), runtime)
            emit(dict(strategy=STRATEGY, version=VERSION, symbol=SYMBOL, type='THRESHOLD_READY',
                      month=month, generated=generated, emitted_at=datetime.now(MARKET_TZ).isoformat(timespec='seconds')))
            if stopped.is_set():
                return 0
            return _run_live(args, emit, runtime, hashes, stopped)
    except Exception as exc:
        emit(dict(strategy=STRATEGY, version=VERSION, symbol=SYMBOL, type='ERROR', message=repr(exc),
                  emitted_at=datetime.now(MARKET_TZ).isoformat(timespec='seconds')))
        raise
    finally:
        if notifier is not None:
            notifier.close()



def cli(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('fetch-bars', help='只读获取指数和ETF历史行情')
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--etf-out', type=Path, default=None, help='省略则只拉指数')
    p.add_argument('--start', default='2021-11-01'); p.add_argument('--end', default=None)
    p.add_argument('--host', default='127.0.0.1'); p.add_argument('--port', type=int, default=11111)
    p = sub.add_parser('backtest', help='指数口径回测；给出 --etf-panel 时另算ETF执行账本')
    p.add_argument('--bars', type=Path, required=True)
    p.add_argument('--etf-panel', type=Path, default=None)
    p.add_argument('--start', default=BACKTEST_START); p.add_argument('--end', default=None)
    p.add_argument('--thresholds', type=Path, default=None)
    p.add_argument('--cost-bps', type=float, default=COST_BPS)
    p.add_argument('--lag', type=int, default=LAG_BARS)
    p.add_argument('--output', type=Path, required=True)
    p = sub.add_parser('live', help='连接Futu OpenD，逐根输出M2信号并通知；不下单')
    p.add_argument('--runtime-dir', required=True, help='绝对路径；保存月度阈值、校准行情、状态与实例锁')
    p.add_argument('--config', help='项目config.ini：Futu连接与通知渠道')
    p.add_argument('--history-bars', type=int, default=HISTORY_BARS)
    p.add_argument('--initial-position', choices=('flat', 'long'), default='flat')
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
    panel = None if args.etf_panel is None else load_etf_panel(args.etf_panel)
    manifest = input_manifest(args.bars, args.etf_panel, args.thresholds)
    stat, trades, curve, sig = replay(args.bars, args.start, args.end, args.thresholds,
                                      args.cost_bps, args.lag, panel)
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
