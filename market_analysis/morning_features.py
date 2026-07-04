#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
早盘特征 10:00 检查点 —— 通用实盘计算脚本(futu OpenD)

对任意标的计算开盘 N 分钟的 path_eff 与 frac_above(真实黄线口径),
与滚动分位阈值对比,输出纪律判定(P0-1 v3 研究结论,见 market_analysis/ashare_day_trade.py):

  path_eff ≥ pe_thr 且 frac_above ≤ 0.10  → 顶档&线下:当日禁止抄底、持仓减仓
  path_eff ≥ pe_thr 且 frac_above ≥ 0.90  → 双高:不追多(无边)
  其余                                     → 无信号

阈值来源(二选一):
  --pe-thr 0.31          直接给定(如来自研究环境;注意数据源口径差异)
  不传                    自动用 futu 1m 历史计算滚动 ROLL 日 PCT 分位并 shift(1),
                          日度特征缓存于 data/morning_features/,增量更新。
                          阈值与实时值同源,无集合竞价口径问题。

用法:
  盘中 10:00 判定:  python market_analysis/morning_features.py SZ.002050
  给定阈值:         python market_analysis/morning_features.py SZ.002050 --pe-thr 0.31
  历史某日复核:     python market_analysis/morning_features.py SZ.002050 --date 2026-06-30
  机器可读输出:     python market_analysis/morning_features.py SZ.002050 --json

依赖:futu OpenD 已启动;config ini 提供 CONFIG.FUTU_HOST/FUTU_PORT(可省略,默认本机)。
注意:富途 A 股 1m 历史约 2 年,首次建缓存拉取约 380 个交易日,消耗 1 只标的的历史K线额度。
"""
import argparse
import configparser
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import futu as ft

FA_HI, FA_LO = 0.90, 0.10
HIST_DAYS = 550            # 首次建缓存回看的日历天数(≈375交易日)
MIN_DAY_BARS = 200         # 历史日样本完整性下限(A股全日240根)


# ---------------- 特征计算(与研究口径一致) ----------------
def morning_features(day_df, window):
    """开盘 window 根1分钟bar → path_eff / frac_above;不足 window 根返回 partial"""
    day = day_df[day_df['volume'] > 0].sort_index()
    if len(day) == 0:
        return None
    o30 = day.iloc[:window]
    o = float(o30['open'].iloc[0])
    c = o30['close'].values.astype(float)
    v = o30['volume'].values.astype(float)
    m = o30['turnover'].values.astype(float)
    if np.nansum(m) > 0:
        vwap = np.cumsum(m) / np.cumsum(v)          # 真实黄线(个股/ETF)
    else:
        vwap = np.cumsum(c * v) / np.cumsum(v)      # 指数代理
    step = np.abs(np.diff(np.r_[o, c])).sum()
    return dict(
        path_eff=abs(c[-1] - o) / step if step > 0 else 0.0,
        frac_above=float((c > vwap).mean()),
        n_bars=len(o30),
        partial=len(o30) < window,
        last_time=str(o30.index[-1]),
        last_close=float(c[-1]),
    )


def build_daily_features(px, window):
    rows = []
    for d, day in px.groupby(px.index.date):
        if len(day) < MIN_DAY_BARS:
            continue
        f = morning_features(day, window)
        if f and not f['partial']:
            rows.append(dict(date=d, path_eff=f['path_eff'],
                             frac_above=f['frac_above']))
    return pd.DataFrame(rows)


# ---------------- futu 数据 ----------------
def fetch_history_1m(ctx, code, start, end):
    chunks, page_key = [], None
    while True:
        # 不复权:日内特征无需复权(path_eff为比值,frac_above为同日比较),
        # 且futu的QFQ不调整volume/turnover,复权价与原始vwap混用会系统性压低frac_above
        ret, data, page_key = ctx.request_history_kline(
            code, start=start, end=end, ktype=ft.KLType.K_1M,
            autype=ft.AuType.NONE, max_count=1000, page_req_key=page_key)
        if ret != ft.RET_OK:
            raise RuntimeError('request_history_kline失败: %s' % data)
        if len(data):
            chunks.append(data)
        if page_key is None:
            break
        time.sleep(0.4)
    if not chunks:
        return pd.DataFrame()
    df = pd.concat(chunks)
    df['time_key'] = pd.to_datetime(df['time_key'])
    return df.set_index('time_key').sort_index()


def fetch_today_1m(ctx, code):
    ret, err = ctx.subscribe([code], [ft.SubType.K_1M], subscribe_push=False)
    if ret != ft.RET_OK:
        raise RuntimeError('subscribe失败: %s' % err)
    ret, data = ctx.get_cur_kline(code, 500, ft.KLType.K_1M, ft.AuType.NONE)
    if ret != ft.RET_OK:
        raise RuntimeError('get_cur_kline失败: %s' % data)
    data['time_key'] = pd.to_datetime(data['time_key'])
    data = data.set_index('time_key').sort_index()
    return data[data.index.date == date.today()]


# ---------------- 阈值(滚动分位,shift(1) 语义) ----------------
def cache_path(cache_dir, code):
    return Path(cache_dir) / ('feat_%s.csv' % code.replace('.', '_'))


def update_cache(ctx, code, cache_dir, window):
    """增量维护日度特征序列,截至昨日"""
    fp = cache_path(cache_dir, code)
    if fp.exists():
        hist = pd.read_csv(fp, parse_dates=['date'])
        hist['date'] = hist['date'].dt.date
        start = max(hist['date']) + timedelta(days=1)
    else:
        hist = pd.DataFrame(columns=['date', 'path_eff', 'frac_above'])
        start = date.today() - timedelta(days=HIST_DAYS)
    end = date.today() - timedelta(days=1)
    if start <= end:
        px = fetch_history_1m(ctx, code, str(start), str(end))
        if len(px):
            new = build_daily_features(px, window)
            if len(new):
                hist = pd.concat([hist, new], ignore_index=True)
                hist = (hist.drop_duplicates('date', keep='last')
                            .sort_values('date').reset_index(drop=True))
        fp.parent.mkdir(parents=True, exist_ok=True)
        hist.to_csv(fp, index=False)
    return hist


def rolling_threshold(hist, asof, roll, pct, min_periods):
    """asof 当日的阈值 = 严格早于 asof 的最近 roll 个交易日 path_eff 的 pct 分位"""
    past = hist[hist['date'] < asof]['path_eff'].tail(roll)
    if len(past) < min_periods:
        raise RuntimeError('历史样本不足: %d < min_periods %d(先跑一次建缓存,'
                           '或用 --pe-thr 直接给定阈值)' % (len(past), min_periods))
    return float(past.quantile(pct)), len(past)


# ---------------- 判定 ----------------
def judge(path_eff, frac_above, pe_thr):
    if path_eff >= pe_thr and frac_above <= FA_LO:
        return ('顶档&线下', '当日禁止抄底、持仓应减仓 '
                '(P(下跌趋势日)≈0.42,10:00→收盘期望≈-0.5%)')
    if path_eff >= pe_thr and frac_above >= FA_HI:
        return ('双高', '不追多(期望≈0,历史胜率<0.5),仅此而已')
    if path_eff >= pe_thr:
        return ('pe顶档&中性', '无操作含义')
    return ('无信号', '普通/震荡开局,按原计划执行')


def main():
    ap = argparse.ArgumentParser(description='早盘特征 10:00 检查点(futu OpenD)')
    ap.add_argument('code', help='futu 代码,如 SZ.002050 / SH.600519 / SH.000001')
    ap.add_argument('--pe-thr', type=float, default=None,
                    help='path_eff 阈值;不传则用 futu 历史自动计算滚动分位')
    ap.add_argument('--window', type=int, default=30, help='开盘观察窗口分钟数')
    ap.add_argument('--roll', type=int, default=250, help='滚动分位窗口(交易日)')
    ap.add_argument('--pct', type=float, default=0.8, help='分位数')
    ap.add_argument('--min-periods', type=int, default=100)
    ap.add_argument('--date', default=None,
                    help='复核历史某日(YYYY-MM-DD),从缓存读取该日特征')
    ap.add_argument('--no-update', action='store_true', help='跳过历史缓存增量更新')
    ap.add_argument('--json', action='store_true', help='输出 JSON')
    ap.add_argument('--config', default=None, help='config ini 路径')
    ap.add_argument('--cache-dir',
                    default=str(Path(__file__).resolve().parents[1]
                                / 'data' / 'morning_features'))
    args = ap.parse_args()

    host, port = '127.0.0.1', 11111
    if args.config:
        cfg = configparser.ConfigParser()
        cfg.read(args.config, encoding='utf-8')
        host = cfg.get('CONFIG', 'FUTU_HOST', fallback=host)
        port = int(cfg.get('CONFIG', 'FUTU_PORT', fallback=port))

    ctx = ft.OpenQuoteContext(host=host, port=port)
    try:
        need_hist = (args.pe_thr is None) or args.date
        hist = (update_cache(ctx, args.code, args.cache_dir, args.window)
                if (need_hist and not args.no_update)
                else (pd.read_csv(cache_path(args.cache_dir, args.code),
                                  parse_dates=['date'])
                      .assign(date=lambda x: x['date'].dt.date)
                      if need_hist else None))

        if args.date:                                   # 历史复核模式
            d0 = datetime.strptime(args.date, '%Y-%m-%d').date()
            row = hist[hist['date'] == d0]
            if len(row) == 0:
                raise RuntimeError('缓存中无 %s(停牌/超出历史范围?)' % d0)
            feats = dict(path_eff=float(row['path_eff'].iloc[0]),
                         frac_above=float(row['frac_above'].iloc[0]),
                         n_bars=args.window, partial=False,
                         last_time=str(d0), last_close=None)
            asof = d0
        else:                                           # 实盘模式
            feats = morning_features(fetch_today_1m(ctx, args.code), args.window)
            if feats is None:
                raise RuntimeError('今日暂无有效1分钟bar(未开盘或停牌?)')
            asof = date.today()

        if args.pe_thr is not None:
            pe_thr, thr_n, thr_src = args.pe_thr, None, '命令行'
        else:
            pe_thr, thr_n = rolling_threshold(
                hist, asof, args.roll, args.pct, args.min_periods)
            thr_src = 'futu历史滚动%d日%.0f分位(n=%d,截至%s前一交易日)' % (
                args.roll, args.pct * 100, thr_n, asof)

        tag, advice = judge(feats['path_eff'], feats['frac_above'], pe_thr)
        if feats['partial']:
            advice = '窗口未走完(%d/%d bar),判定未确认' % (
                feats['n_bars'], args.window)

        out = dict(code=args.code, asof=str(asof), window=args.window,
                   n_bars=feats['n_bars'], partial=feats['partial'],
                   path_eff=round(feats['path_eff'], 4),
                   frac_above=round(feats['frac_above'], 4),
                   pe_thr=round(pe_thr, 4), pe_thr_source=thr_src,
                   fa_hi=FA_HI, fa_lo=FA_LO, signal=tag, advice=advice,
                   last_bar=feats['last_time'])
        if args.json:
            print(json.dumps(out, ensure_ascii=False))
            return
        print('标的: %s | 截至: %s | 窗口: 开盘%d分钟 | 不复权 | 数据源: futu OpenD'
              % (args.code, out['last_bar'], args.window))
        print('pe_thr = %.4f  [%s]' % (pe_thr, thr_src))
        print('path_eff   = %.4f  %s 阈值' % (
            feats['path_eff'], '≥' if feats['path_eff'] >= pe_thr else '<'))
        print('frac_above = %.4f  (线下≤%.2f / 线上≥%.2f)' % (
            feats['frac_above'], FA_LO, FA_HI))
        if feats['partial']:
            print('⚠ 窗口未走完: %d/%d bar,以下判定未确认' % (
                feats['n_bars'], args.window))
        print('判定: 【%s】 %s' % (tag, advice))
    finally:
        ctx.close()


if __name__ == '__main__':
    main()
