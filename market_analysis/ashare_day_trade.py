#!/usr/bin/env python
# coding: utf-8
# -*- coding: utf-8 -*-
"""
早盘特征研究整合版:趋势日识别 + 震荡日结构 + 结论报告
环境:聚宽研究环境

结构:
  Part 1 数据与日表:分年拉分钟线(个股前复权/skip_paused),日度特征、
         标签、交易经济量、滚动阈值(shift(1) 无前视)、触板标记
  Part 2 统计:
    表A 单特征五分位        表B 日内高低点时间     表C path_eff regime稳定性
    表D 联合条件+方向       表E drift经济价值      表F 双高regime稳定性
    表M 触板日归因(截尾诊断)
    表G/G2 乖离回归         表H 均价线状态         表I 日内时段
    表J 破界(可交易口径)  表L 早盘交叉           表K 乖离regime稳定性
  Part 3 结论报告:按统一判定标准自动评估各候选规则,输出可执行的
         操作指示与读盘要点(数值全部来自本次统计,换标的自动重估)

口径:个股 fq='pre'、真实分时均价线(money/volume,聚宽volume同步复权故
因子自消)、触板日剔除出表A-L统计并在表M单独归因;收益均未含成本。
实盘判定工具见 market_analysis/morning_features.py(futu OpenD,不复权口径)。
"""
from jqdata import *
import pandas as pd
import numpy as np

pd.set_option('display.max_columns', None)
pd.set_option('display.width', 220)
pd.set_option('display.max_rows', 200)

# ================= 配置 =================
CODE   = '002050.XSHE'
START, END = '2021-01-04', '2026-06-30'
N_OPEN   = 30                # 开盘观察窗口(分钟)
LOOKBACK = 20                # 标准化基准窗口(交易日)
ROLL_Q   = 250               # 规则级信号的滚动分位窗口(无前视)
PCT_Q    = 0.8
TREND_BODY = 0.70            # 趋势日: |C-O|/(H-L) 阈值
TREND_EDGE = 0.85            # 趋势日: 收盘位于区间端部15%以内
FA_HI, FA_LO = 0.90, 0.10    # frac_above 固定阈值
Z_GRID   = [0.15, 0.25, 0.35]   # 乖离阈值(近20日均日振幅为单位)
Z_MAIN   = 0.25
H1       = 30                # 乖离/破界后的收益观察窗口(分钟)
K_GRID   = [5, 10, 20]       # 破界确认等待(分钟)
REGIMES = [('2021-2024.09', '2021-01-04', '2024-09-23'),
           ('2024.10-now',  '2024-09-24', '2026-06-30')]
FEATS = ['amp30_norm', 'path_eff', 'frac_above', 'vol30_ratio', 'gap_norm']

# 结论报告的规则判定标准
MIN_N, MIN_LIFT, MIN_INCR, MIN_WIN = 50, 2.5, 0.002, 0.55
MIN_T = 2.0                  # 增量的 t 统计量下限(对不同波动率标的自动校准)
COST = 0.0012                # 双边成本估计(佣金+印花+滑点)

info = get_security_info(CODE)
IS_STOCK = (info.type == 'stock')
LIM = 0.20 if (IS_STOCK and CODE[:3] in ('300', '688', '301', '689')) else 0.10
print('标的: %s (%s) | 类型: %s%s' % (CODE, info.display_name, info.type,
      ' | 涨跌幅限 %.0f%%' % (LIM * 100) if IS_STOCK else ''))

# ================= Part 1 | 数据与日表 =================
def fetch_minutes(code, start, end):
    chunks = []
    for y in range(int(start[:4]), int(end[:4]) + 1):
        s = max('%d-01-01' % y, start)
        e = min('%d-12-31' % y, end)
        df = get_price(code, start_date=s, end_date=e + ' 16:00:00',
                       frequency='1m',
                       fields=['open', 'high', 'low', 'close', 'volume', 'money'],
                       skip_paused=True, fq=('pre' if IS_STOCK else None))
        if df is not None and len(df):
            chunks.append(df)
            print('%d: %d bars' % (y, len(df)))
    px = pd.concat(chunks).sort_index()
    px = px[px['volume'] > 0]
    px['date'] = px.index.date
    return px

def day_vwap(c, v, m):
    """个股:真实分时均价线(money/volume);指数:量权点位代理"""
    if IS_STOCK:
        return np.cumsum(m) / np.cumsum(v)
    return np.cumsum(c * v) / np.cumsum(v)

def build_daily(px, n=N_OPEN):
    rows = []
    for date, day in px.groupby('date'):
        day = day.sort_index()
        if len(day) < 230:                     # 非完整交易日跳过
            continue
        o = day['open'].iloc[0]
        H, L, C = day['high'].max(), day['low'].min(), day['close'].iloc[-1]
        o30 = day.iloc[:n]
        closes30 = o30['close'].values
        h30, l30 = o30['high'].max(), o30['low'].min()
        vwap30 = day_vwap(closes30, o30['volume'].values, o30['money'].values)
        step = np.abs(np.diff(np.r_[o, closes30])).sum()
        rows.append(dict(
            date=pd.Timestamp(date), open_px=o,
            amp30_raw  = h30 - l30,
            path_eff   = abs(closes30[-1] - o) / step if step > 0 else 0.0,
            frac_above = float((closes30 > vwap30).mean()),
            vol30      = o30['volume'].sum(),
            p30      = closes30[-1],               # 10:00收盘价 = 信号确认时点可成交价
            lo_after = day['low'].iloc[n:].min(),  # 10:00后最低(多头MAE)
            hi_after = day['high'].iloc[n:].max(), # 10:00后最高(空头MAE)
            day_high=H, day_low=L, day_close=C,
            body      = abs(C - o) / (H - L) if H > L else 0.0,
            close_pos = (C - L) / (H - L) if H > L else 0.5,
            t_high    = int(day['high'].values.argmax()),
            t_low     = int(day['low'].values.argmin()),
            range_done_30 = (h30 - l30) / (H - L) if H > L else 1.0,
        ))
    d = pd.DataFrame(rows).set_index('date').sort_index()

    # ---- 标准化(基准一律 shift(1),杜绝前视) ----
    d['prev_close'] = d['day_close'].shift(1)
    d['gap']     = d['open_px'] / d['prev_close'] - 1
    d['day_amp'] = (d['day_high'] - d['day_low']) / d['prev_close']
    d['amp30']   = d['amp30_raw'] / d['prev_close']
    d['ref_amp'] = d['day_amp'].rolling(LOOKBACK).mean().shift(1)
    ref_vol      = d['vol30'].rolling(LOOKBACK).mean().shift(1)
    d['ref_ret'] = d['day_close'].pct_change().rolling(LOOKBACK).std().shift(1)
    d['amp30_norm']  = d['amp30'] / d['ref_amp']
    d['vol30_ratio'] = d['vol30'] / ref_vol
    d['gap_norm']    = d['gap'] / d['ref_ret']
    d['ext_amp']     = d['day_amp'] - d['amp30']

    # ---- 趋势日标签与方向 ----
    d['trend_day'] = ((d['body'] >= TREND_BODY) &
                      ((d['close_pos'] >= TREND_EDGE) |
                       (d['close_pos'] <= 1 - TREND_EDGE))).astype(int)
    d['trend_up'] = ((d['trend_day'] == 1) & (d['close_pos'] >= 0.5)).astype(int)
    d['trend_dn'] = ((d['trend_day'] == 1) & (d['close_pos'] < 0.5)).astype(int)

    # ---- 交易经济量 ----
    d['drift']     = d['day_close'] / d['p30'] - 1        # 10:00持有到收盘
    d['mae_long']  = d['lo_after'] / d['p30'] - 1         # 多头最大不利偏移(负值)
    d['mae_short'] = -(d['hi_after'] / d['p30'] - 1)      # 空头最大不利偏移(负值)
    d['drift_vol_adj'] = d['drift'] / d['ref_ret']

    # ---- 规则级信号阈值:滚动分位,shift(1) 无前视 ----
    d['pe_thr'] = (d['path_eff'].rolling(ROLL_Q, min_periods=100)
                   .quantile(PCT_Q).shift(1))

    # ---- 触板标记(表A-L剔除,表M归因) ----
    if IS_STOCK:
        d['touch_up'] = d['day_high'] >= d['prev_close'] * (1 + LIM) * 0.995
        d['touch_dn'] = d['day_low']  <= d['prev_close'] * (1 - LIM) * 0.995
    else:
        d['touch_up'] = False
        d['touch_dn'] = False
    d['touch_limit'] = d['touch_up'] | d['touch_dn']
    return d.dropna(subset=['amp30_norm', 'vol30_ratio', 'gap_norm'])

px = fetch_minutes(CODE, START, END)
d_all = build_daily(px)
d = d_all[~d_all['touch_limit']]
print('触板日剔除: %d / %d(归因见表M)'
      % (int(d_all['touch_limit'].sum()), len(d_all)))
BASE_TREND = d['trend_day'].mean()
BASE_UP, BASE_DN = d['trend_up'].mean(), d['trend_dn'].mean()
BASE_DRIFT = d['drift'].mean()
YEARS = (d.index[-1] - d.index[0]).days / 365.25
print('\n有效交易日 %d | 趋势日base rate %.3f | 无条件日内drift基线 %+.4f | 样本年数 %.1f\n'
      % (len(d), BASE_TREND, BASE_DRIFT, YEARS))

# ================= Part 2 | 统计 =================
def quintile_table(d_, feat):
    q = pd.qcut(d_[feat], 5, labels=False, duplicates='drop')
    g = d_.groupby(q)
    t = pd.DataFrame({
        'n':             g['trend_day'].size(),
        'P_trend':       g['trend_day'].mean(),
        'day_amp':       g['day_amp'].mean(),
        'ext_amp':       g['ext_amp'].mean(),
        'range_done_30': g['range_done_30'].mean(),
        'feat_mean':     g[feat].mean(),
    })[['n', 'P_trend', 'day_amp', 'ext_amp', 'range_done_30', 'feat_mean']]
    t.index.name = feat + '_quintile'
    return t.round(4)

def hilo_timing(d_):
    def frac(s):
        return pd.Series({
            'high_in_first30': (s['t_high'] < 30).mean(),
            'high_in_last30':  (s['t_high'] >= 210).mean(),
            'low_in_first30':  (s['t_low'] < 30).mean(),
            'low_in_last30':   (s['t_low'] >= 210).mean(),
            'n': float(len(s))})
    return pd.concat({
        'trend_day': frac(d_[d_['trend_day'] == 1]),
        'range_day': frac(d_[d_['trend_day'] == 0])}, axis=1).round(3)

print('================ 表A | 单特征五分位 ================')
for f in FEATS:
    print('----- %s -----' % f)
    print(quintile_table(d, f), '\n')

print('================ 表B | 日内高低点时间分布 ================')
TB = hilo_timing(d)
print(TB, '\n')

print('================ 表C | regime稳定性: path_eff 顶/底档 P(趋势日) ================')
for name, s_, e_ in REGIMES:
    sub = d.loc[s_:e_]
    if len(sub) < 100:
        continue
    q = pd.qcut(sub['path_eff'], 5, labels=False, duplicates='drop')
    print('%s: n=%d, base=%.3f, 顶档=%.3f, 底档=%.3f, 顶/底差=%+.3f'
          % (name, len(sub), sub['trend_day'].mean(),
             sub[q == 4]['trend_day'].mean(), sub[q == 0]['trend_day'].mean(),
             sub[q == 4]['trend_day'].mean() - sub[q == 0]['trend_day'].mean()))

# ---- 信号宇宙(表D/E/F:触板剔除后;表M与报告:含触板归因) ----
sig = d.dropna(subset=['pe_thr'])
hi_pe = sig['path_eff'] >= sig['pe_thr']
hi_fa = sig['frac_above'] >= FA_HI
lo_fa = sig['frac_above'] <= FA_LO
GROUPS = [(hi_pe & hi_fa,            '双高(pe顶档&持续线上)'),
          (hi_pe & lo_fa,            '顶档&持续线下'),
          (hi_pe & ~hi_fa & ~lo_fa,  'pe顶档&中性'),
          (~hi_pe & ~hi_fa & ~lo_fa, '双弱对照')]

print('\n================ 表D | 联合条件 + 方向拆分 ================')
for m, name in GROUPS:
    s = sig[m]
    if len(s) == 0:
        continue
    print('%-22s n=%4d  P(趋势)=%.3f  P(上涨趋势)=%.3f  P(下跌趋势)=%.3f  频率=%.1f%%'
          % (name, len(s), s['trend_day'].mean(), s['trend_up'].mean(),
             s['trend_dn'].mean(), 100.0 * len(s) / len(sig)))

def drift_report(s, name, side):
    s = s.dropna(subset=['drift'])
    if len(s) == 0:
        return
    r = s['drift'] * side
    mae = s['mae_long'].mean() if side == 1 else s['mae_short'].mean()
    print('%-22s n=%4d  期望drift=%+.4f  增量=%+.4f  胜率=%.3f  MAE=%+.4f  年化贡献=%+.2f%%'
          % (name, len(s), r.mean(), r.mean() - BASE_DRIFT * side, (r > 0).mean(),
             mae, 100 * r.mean() * len(s) / YEARS))

print('\n================ 表E | drift经济价值 (10:00→收盘) ================')
print('(增量=期望drift-无条件基线%+.4f;MAE为持有途中最大不利偏移,负值)' % BASE_DRIFT)
drift_report(sig[hi_pe & hi_fa],            '双高(做多)',        +1)
drift_report(sig[hi_pe & lo_fa],            '顶档&线下(空/减仓)', -1)
drift_report(sig[~hi_pe & ~hi_fa & ~lo_fa], '双弱对照(做多)',    +1)

print('\n================ 表F | 双高信号 regime稳定性 ================')
for name, s_, e_ in REGIMES:
    sub = sig.loc[s_:e_]
    r = sub[(sub['path_eff'] >= sub['pe_thr']) &
            (sub['frac_above'] >= FA_HI)].dropna(subset=['drift'])
    if len(r) < 20:
        continue
    print('%s: n=%d  drift=%+.4f  vol调整=%+.3fσ  胜率=%.3f'
          % (name, len(r), r['drift'].mean(),
             r['drift_vol_adj'].mean(), (r['drift'] > 0).mean()))

# ---- 表M:触板日归因(含触板宇宙 sa;报告复用这些掩码) ----
print('\n================ 表M | 触板日 × 10:00信号 归因(触板日不在表A-L内) ================')
sa = d_all.dropna(subset=['pe_thr'])
pe_a = sa['path_eff'] >= sa['pe_thr']
fh_a = sa['frac_above'] >= FA_HI
fl_a = sa['frac_above'] <= FA_LO
early_touch = ((sa['touch_up'] & (sa['t_high'] < N_OPEN)) |
               (sa['touch_dn'] & (sa['t_low'] < N_OPEN)))   # 10:00前已触板:不可执行
M_GROUPS = [(pe_a & fh_a,           '双高'),
            (pe_a & fl_a,           '顶档&线下'),
            (pe_a & ~fh_a & ~fl_a,  'pe顶档&中性'),
            (~pe_a & ~fh_a & ~fl_a, '双弱对照')]
MSTAT = {}
for m, name in M_GROUPS:
    g = sa[m]
    if len(g) == 0:
        continue
    t = g[g['touch_limit']]
    late = t[~early_touch.reindex(t.index).fillna(False)]
    MSTAT[name] = dict(n=len(g), n_touch=len(t),
                       n_up=int(t['touch_up'].sum()), n_dn=int(t['touch_dn'].sum()),
                       n_early=int(early_touch.reindex(t.index).fillna(False).sum()),
                       late_drift=(late['drift'].mean() if len(late) else np.nan),
                       n_late=len(late))
    print('%-14s 总n=%4d | 触板 %2d(涨停向 %d / 跌停向 %d)| 10:00前已触 %d | '
          '10:00后触板日 drift=%s'
          % (name, len(g), len(t), MSTAT[name]['n_up'], MSTAT[name]['n_dn'],
             MSTAT[name]['n_early'],
             ('%+.4f (n=%d)' % (MSTAT[name]['late_drift'], len(late))
              if len(late) else 'n/a')))

# ================= Part 2b | 震荡日结构(乖离/破界事件扫描) =================
d_all['is_range'] = ((d_all['path_eff'] < d_all['pe_thr']) &
                     (d_all['frac_above'] > FA_LO) &
                     (d_all['frac_above'] < FA_HI) &
                     (~d_all['touch_limit']))
meta = {ts.date(): (r['prev_close'], r['ref_amp'], bool(r['is_range']),
                    int(r['trend_day']))
        for ts, r in d_all.dropna(subset=['pe_thr', 'ref_amp']).iterrows()}

z_rows, or_rows = [], []
KMAX = max(K_GRID)
for date, day in px.groupby('date'):
    if date not in meta:
        continue
    prev_c, ra, is_rng, is_trend = meta[date]
    day = day.sort_index()
    if len(day) < 230:
        continue
    c = day['close'].values; h = day['high'].values
    l = day['low'].values;   v = day['volume'].values
    m = day['money'].values; n = len(c)
    vwap = day_vwap(c, v, m)
    z = (c - vwap) / prev_c / ra

    # ---- 乖离穿越事件(冷却;剔除窗口截断) ----
    for zt in Z_GRID:
        armed = True
        for t in range(N_OPEN + 1, n - 1 - H1):
            az = abs(z[t])
            if not armed:
                if az < zt / 2.0:
                    armed = True
                continue
            if az >= zt and abs(z[t - 1]) < zt:
                armed = False
                s = 1.0 if z[t] > 0 else -1.0
                e1 = t + H1
                t0 = max(t - 30, 0)
                z_rows.append(dict(
                    date=pd.Timestamp(date), thr=zt,
                    side=('above' if s > 0 else 'below'),
                    is_range=is_rng,
                    touch30=bool((s * z[t + 1:e1 + 1] <= 0).any()),
                    ret30=-s * (c[e1] / c[t] - 1),
                    mae=(-(h[t + 1:e1 + 1].max() / c[t] - 1) if s > 0
                         else l[t + 1:e1 + 1].min() / c[t] - 1),
                    slope30=(vwap[t] - vwap[t0]) / prev_c / ra,
                    tod=('A_1000-1130' if t < 120 else
                         'B_1300-1400' if t < 180 else 'C_1400-1430')))

    # ---- 开盘区间破界(从确认时点 t+K 起算;每方向取首次) ----
    or_hi, or_lo = h[:N_OPEN].max(), l[:N_OPEN].min()
    done = set()
    for t in range(N_OPEN, n - 1 - KMAX - H1):
        if 'up' not in done and c[t] > or_hi:
            key, direc = 'up', +1.0
        elif 'dn' not in done and c[t] < or_lo:
            key, direc = 'dn', -1.0
        else:
            continue
        done.add(key)
        for K in K_GRID:
            tc = t + K
            inside = ((c[t + 1:tc + 1] <= or_hi) & (c[t + 1:tc + 1] >= or_lo))
            or_rows.append(dict(date=pd.Timestamp(date), K=K, direction=key,
                                rejected=bool(inside.any()), is_range=is_rng,
                                trend_day=is_trend,
                                cont=direc * (c[tc + H1] / c[tc] - 1)))

Z  = pd.DataFrame(z_rows)
OR = pd.DataFrame(or_rows)
print('\n乖离事件 %d | 破界事件×K %d\n' % (len(Z), len(OR)))

def stat_block(g):
    return pd.DataFrame({
        'n':         g['ret30'].size(),
        'P_touch30': g['touch30'].mean(),
        'ret30':     g['ret30'].mean(),
        'win':       g['ret30'].apply(lambda x: (x > 0).mean()),
        'mae':       g['mae'].mean(),
    })[['n', 'P_touch30', 'ret30', 'win', 'mae']].round(4)

print('================ 表G | 双弱日:乖离回归统计 ================')
print(stat_block(Z[Z['is_range']].groupby(['thr', 'side'])), '\n')

print('================ 表G2 | 双弱 vs 非双弱(thr=%.2f) ================' % Z_MAIN)
zm = Z[Z['thr'] == Z_MAIN]
print(stat_block(zm.groupby('is_range'))
      .rename(index={True: '双弱日', False: '非双弱日'}), '\n')

zr = zm[zm['is_range']].copy()
med = zr['slope30'].abs().median()
zr['align'] = np.where(zr['slope30'].abs() < med, 'flat',
              np.where((zr['side'] == 'above') == (zr['slope30'] > 0),
                       'sloped_with', 'sloped_against'))
print('================ 表H | 均价线状态(thr=%.2f) ================' % Z_MAIN)
print(stat_block(zr.groupby('align')), '\n')

print('================ 表I | 日内时段(thr=%.2f,全事件均有完整%d分钟窗口) ================'
      % (Z_MAIN, H1))
print(stat_block(zr.groupby('tod')), '\n')

print('================ 表J | 双弱日:破界(收益自确认时点t+K起算) ================')
orr = OR[OR['is_range']]
g = orr.groupby(['K', 'rejected', 'direction'])
TJ = pd.DataFrame({
    'n':        g['cont'].size(),
    'cont':     g['cont'].mean(),
    'win_cont': g['cont'].apply(lambda x: (x > 0).mean()),
    'P_trend':  g['trend_day'].mean(),
}).round(4)
TJ = TJ.rename(index={True: 'reject', False: 'accept'}, level='rejected')
print(TJ, '\n(reject 行的 fade 期望 = -cont)\n')

print('================ 表L | 早盘 × 均价线状态 × 方向(thr=%.2f) ================' % Z_MAIN)
print(stat_block(zr[zr['tod'] == 'A_1000-1130'].groupby(['align', 'side'])), '\n')

print('================ 表K | 乖离regime稳定性(thr=%.2f,双弱日) ================' % Z_MAIN)
for name, s_, e_ in REGIMES:
    sub = zr[(zr['date'] >= s_) & (zr['date'] <= e_)]
    if len(sub) < 50:
        continue
    print('%s: n=%d  P_touch30=%.3f  ret30=%+.4f  win=%.3f  mae=%+.4f'
          % (name, len(sub), sub['touch30'].mean(), sub['ret30'].mean(),
             (sub['ret30'] > 0).mean(), sub['mae'].mean()))

# ---- 数据质量自检 ----
one = px[px['date'] == d.index[-1].date()]
print('\n[自检] 最后交易日bar数:', len(one),
      '| 首bar:', one.index[0],
      '| 首/次bar量能比:', round(one['volume'].iloc[0] / max(one['volume'].iloc[1], 1), 2),
      '(显著>1则集合竞价并入首bar,量能类特征解读需注意)')
print('[自检] 滚动阈值就绪样本: %d / %d(前%d日无阈值属正常)'
      % (len(sig), len(d), ROLL_Q))

# ================= Part 3 | 结论报告 =================
def eval_rule(s, side, base_p, col_dir):
    """统一判定:n / 方向概率提升 / 增量 / 胜率 / 双regime同向
    注意:聚宽 `from jqdata import *` 可能遮蔽内建函数(实测曾致 ok 判定
    失真),此处一律用显式 and 链与循环,不依赖 all()/sum()/any()。"""
    s = s.dropna(subset=['drift'])
    r = s['drift'] * side
    p_dir = s[col_dir].mean()
    regime_ok, n_regime = True, 0
    for _, s_, e_ in REGIMES:
        rr = s.loc[s_:e_]['drift'].dropna() * side
        if len(rr) >= 15:
            n_regime += 1
            if not bool(rr.mean() > 0):
                regime_ok = False
    regime_ok = bool(regime_ok and n_regime >= 1)
    incr = r.mean() - BASE_DRIFT * side
    se = (r.std() / np.sqrt(len(r))) if len(r) > 1 else np.nan
    tstat = incr / se if (np.isfinite(se) and se > 0) else np.nan
    checks = dict(
        n     = bool(len(s) >= MIN_N),
        lift  = bool(base_p > 0) and bool(p_dir / base_p >= MIN_LIFT),
        incr  = bool(incr >= MIN_INCR),
        tstat = bool(np.isfinite(tstat)) and bool(tstat >= MIN_T),
        win   = bool((r > 0).mean() >= MIN_WIN),
        regime= regime_ok)
    ok = (checks['n'] and checks['lift'] and checks['incr']
          and checks['tstat'] and checks['win'] and checks['regime'])
    return dict(n=len(s), p_dir=p_dir,
                lift=(p_dir / base_p if base_p > 0 else np.nan),
                drift=r.mean(), incr=incr, tstat=tstat,
                win=(r > 0).mean(),
                mae=(s['mae_long'] if side > 0 else s['mae_short']).mean(),
                peryr=len(s) / YEARS, ok=ok, checks=checks)

def check_line(ev):
    parts = []
    for k in ('n', 'lift', 'incr', 'tstat', 'win', 'regime'):
        parts.append('%s=%s' % (k, 'Y' if ev['checks'][k] else 'N'))
    return ' 核对: %s (t=%.2f)' % (' '.join(parts), ev['tstat'])

def fails(ev):
    return ','.join(k for k, v in ev['checks'].items() if not v) or '-'

LINEW = '=' * 72
print('\n%s\n结论报告 | %s (%s) | 样本 %s ~ %s | 未含成本(双边约%.2f%%)\n%s'
      % (LINEW, CODE, info.display_name, d.index[0].date(), d.index[-1].date(),
         COST * 100, LINEW))
print('判定标准: n>=%d, 方向概率提升>=%.1fx, 增量>=%.2f%%且t>=%.1f, 胜率>=%.2f, 双regime同向'
      % (MIN_N, MIN_LIFT, MIN_INCR * 100, MIN_T, MIN_WIN))

# 可执行宇宙:仅剔除10:00前已触板(信号发出时已封板、不可交易)的日子
exe = sa[~early_touch]
xpe = exe['path_eff'] >= exe['pe_thr']
xhi = exe['frac_above'] >= FA_HI
xlo = exe['frac_above'] <= FA_LO
next_thr = d_all['path_eff'].tail(ROLL_Q).quantile(PCT_Q)   # 下一交易日适用

print('\n【读盘要点】每日仅 10:00 一个检查点(或直接跑 morning_features.py %s)'
      % CODE.replace('.XSHE', '(SZ.)').replace('.XSHG', '(SH.)'))
print(' · path_eff = 开盘30分钟"走得直不直":单边不回头→高;上下拉锯→低')
print(' · frac_above = 30分钟里分时价收在黄线(均价线)上方的比例')
print(' · 下一交易日适用阈值 pe_thr = %.4f(滚动%d日%.0f分位,盘后更新)'
      % (next_thr, ROLL_Q, PCT_Q * 100))

# ---- 规则1:顶档&线下 → 减仓/禁抄底 ----
ev = eval_rule(exe[xpe & xlo], -1, exe['trend_dn'].mean(), 'trend_dn')
print('\n【规则1】path_eff>=阈值 且 frac_above<=%.2f(早盘压着黄线单边走弱)' % FA_LO)
if ev['ok']:
    print(' 状态: ✅ 通过验证 → 当日禁止抄底;有持仓应于10:00附近减仓')
    print(' 依据: 下跌趋势日概率 %.0f%%(基线 %.0f%%,%.1f倍)| 10:00卖出预期避损 %.2f%%/次'
          % (100 * ev['p_dir'], 100 * exe['trend_dn'].mean(), ev['lift'],
             100 * ev['drift']))
    print('       胜率 %.2f | 年约 %.0f 次 | 途中最大反弹平均仅 %.2f%% → 别等反弹解套'
          % (ev['win'], ev['peryr'], 100 * abs(ev['mae'])))
    print(' 尾盘: 趋势日 %.0f%% 的全天最低点落在最后30分钟 → 若需回补,宜等尾盘而非盘中'
          % (100 * TB.loc['low_in_last30', 'trend_day']))
else:
    print(' 状态: ❌ 未通过(不达标项: %s)→ 不构成规则' % fails(ev))
    print(' 数值: n=%d P(下跌趋势)=%.3f(%.1fx) 增量=%+.4f 胜率=%.3f'
          % (ev['n'], ev['p_dir'], ev['lift'], ev['incr'], ev['win']))
print(check_line(ev))

# ---- 规则2:双高 → 追多? ----
ev2 = eval_rule(exe[xpe & xhi], +1, exe['trend_up'].mean(), 'trend_up')
print('\n【规则2】path_eff>=阈值 且 frac_above>=%.2f(早盘骑着黄线单边走强)' % FA_HI)
if ev2['ok']:
    print(' 状态: ✅ 通过验证 → 可于10:00顺势加仓(罕见,复核表F后再用)')
    print(' 依据: 期望 %+.2f%%/次 增量 %+.2f%% 胜率 %.2f 年约 %.0f 次'
          % (100 * ev2['drift'], 100 * ev2['incr'], ev2['win'], ev2['peryr']))
elif ev2['drift'] < -COST:
    print(' 状态: ⛔ 负期望(%+.2f%%/次, 胜率 %.2f)→ 禁止追多,视同陷阱' %
          (100 * ev2['drift'], ev2['win']))
elif ev2['drift'] <= COST:
    print(' 状态: ⚪ 无边(期望 %+.2f%%/次, 胜率 %.2f)→ 纪律=不追多,也不反向操作'
          % (100 * ev2['drift'], ev2['win']))
else:
    print(' 状态: ❌ 正期望但未通过验证(不达标项: %s)→ 不构成规则,不追多'
          % fails(ev2))
    print(' 数值: n=%d 期望 %+.2f%%/次 增量 %+.2f%% 胜率 %.2f'
          % (ev2['n'], 100 * ev2['drift'], 100 * ev2['incr'], ev2['win']))
print(check_line(ev2))
mdw = MSTAT.get('双弱对照', {})
if IS_STOCK and mdw.get('n_late', 0) > 0:
    lim_late_all = 0
    for v in MSTAT.values():
        lim_late_all += v.get('n_late', 0)
    print(' 提示: 10:00后触板日共 %d 个,其中 %d 个来自"双弱平淡开局"'
          % (lim_late_all, mdw['n_late']))
    print('       → 大涨/涨停日无法用10:00特征预判,勿以早盘强弱追涨')

# ---- 盘中其他机会评估(数据驱动,超过成本才提示复核) ----
z_ret = zr['ret30'].mean() if len(zr) else np.nan
tj_big = TJ[TJ['n'] >= 80]['cont'].abs().max() if len(TJ) else np.nan
def _verdict(x):
    if not np.isfinite(x):
        return '(样本不足)'
    return ('→ 放弃,不要做' if abs(x) < COST
            else '→ ⚠ 超过成本线,值得复核表G-K后再议')
print('\n【盘中其他机会评估】(与双边成本 %.2f%% 对比)' % (COST * 100))
print(' · 盘中乖离回归fade(碰±%.2f振幅单位后反向): 期望 %+.4f/次(n=%d)%s'
      % (Z_MAIN, z_ret, len(zr), _verdict(z_ret)))
print(' · 开盘区间破界追随或fade: 各口径最大|期望| %.4f/次 %s'
      % (tj_big, _verdict(tj_big)))
print(' · 用10:00特征预判大涨日/涨停日: 无信息(见规则2提示)')

print('\n【口径与定位】收益未含成本;触板日剔除出统计、单独归因(表M);')
print(' 阈值滚动生成无前视。本报告是纪律辅助,不是预测——')
print(' 它的作用是在 10:00 用两个数替代盘中的情绪判断。')
