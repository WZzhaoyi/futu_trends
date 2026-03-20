import glob, json, math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import configparser
import pandas as pd
from tools import calc_momentum, calc_returns_score, sanitize_path_component

SNAPSHOT_DIR = './output/snapshots'


def _safe_group(group: str) -> str:
    return sanitize_path_component(group) if group else 'default'


def _snapshot_path(group: str, ktype: str, date_str: str, snapshot_dir: str) -> Path:
    return Path(snapshot_dir) / f"snapshot_{_safe_group(group)}_{ktype}_{date_str}.json"


# ── 快照读写 ────────────────────────────────────────────────────────────────

def save_snapshot(trends_df: pd.DataFrame, group: str, ktype: str,
                  snapshot_dir: str = SNAPSHOT_DIR,
                  date_str: str|None = None) -> Path:
    """
    持久化快照。
    date_str 优先使用传入值；若为 None，则从 trends_df['kline_date'] 列推导；
    再 fallback 到 datetime.now()。
    """
    if date_str is None:
        if 'kline_date' in trends_df.columns:
            valid = trends_df['kline_date'].replace('', pd.NA).dropna()
            date_str = str(valid.max()) if len(valid) > 0 else datetime.now().strftime('%Y%m%d')
        else:
            date_str = datetime.now().strftime('%Y%m%d')

    Path(snapshot_dir).mkdir(parents=True, exist_ok=True)

    def _f(v) -> Optional[float]:
        try:
            f = float(v)
            return None if math.isnan(f) else round(f, 6)
        except (TypeError, ValueError):
            return None

    data, rank = [], 1
    for code, row in trends_df.iterrows():
        if code == 'ZERO_AXIS':
            continue
        data.append({'code': str(code), 'name': str(row['name']),
                     'momentum': _f(row['momentum']), 'rank': rank,
                     'ret_20d': _f(row.get('ret_20d')),
                     'ret_60d': _f(row.get('ret_60d')),
                     'score': _f(row.get('score'))})
        rank += 1

    path = _snapshot_path(group, ktype, date_str, snapshot_dir)
    with open(path, 'w', encoding='utf-8') as fp:
        json.dump({'date': date_str, 'group': group or 'default',
                   'ktype': ktype, 'data': data}, fp, ensure_ascii=False, indent=2)
    return path


def _load_latest(group: str, ktype: str, snapshot_dir: str) -> Optional[dict]:
    """找日期最新的快照文件。"""
    pattern = str(Path(snapshot_dir) / f"snapshot_{_safe_group(group)}_{ktype}_*.json")
    files = glob.glob(pattern)
    if not files:
        return None
    best, best_date = None, None
    for f in files:
        try:
            fd = datetime.strptime(Path(f).stem.rsplit('_', 1)[-1], '%Y%m%d')
            if best_date is None or fd > best_date:
                best_date, best = fd, f
        except ValueError:
            continue
    if best is None:
        return None
    with open(best, 'r', encoding='utf-8') as fp:
        return json.load(fp)


def load_previous_snapshot(group: str, ktype: str, current_date: datetime,
                            snapshot_dir: str = SNAPSHOT_DIR,
                            lookback_days: int = 7) -> Optional[dict]:
    """找距 current_date 最近的约 lookback_days 天前的快照。"""
    pattern = str(Path(snapshot_dir) / f"snapshot_{_safe_group(group)}_{ktype}_*.json")
    files = glob.glob(pattern)
    if not files:
        return None
    target = current_date - timedelta(days=lookback_days)
    cands = []
    for f in files:
        try:
            fd = datetime.strptime(Path(f).stem.rsplit('_', 1)[-1], '%Y%m%d')
            if fd < current_date:
                cands.append((fd, f))
        except ValueError:
            continue
    if not cands:
        return None
    cands.sort(key=lambda x: abs((x[0] - target).days))
    with open(cands[0][1], 'r', encoding='utf-8') as fp:
        return json.load(fp)


# ── 异常检测 ────────────────────────────────────────────────────────────────

def detect_anomalies(trends_df: pd.DataFrame, prev_snapshot: dict) -> list:
    prev_map = {r['code']: r for r in prev_snapshot['data']}
    anomalies, curr_rank = [], 1

    for code, row in trends_df.iterrows():
        if code == 'ZERO_AXIS':
            continue
        if code not in prev_map:
            curr_rank += 1
            continue
        prev, name = prev_map[code], row['name']
        prev_rank = prev.get('rank', 0)
        rank_change = prev_rank - curr_rank

        if abs(rank_change) >= 5:
            label = '跃升' if rank_change > 0 else '暴跌'
            anomalies.append({'code': code, 'name': name, 'rule': 'rank_change',
                               'detail': f'排名{label}{abs(rank_change)}位 ({prev_rank}->{curr_rank})',
                               'severity': 'high' if abs(rank_change) >= 10 else 'medium'})

        cr20, pr20 = row.get('ret_20d'), prev.get('ret_20d')
        if cr20 is not None and pr20 is not None:
            if pr20 > 0 and cr20 < 0:
                anomalies.append({'code': code, 'name': name, 'rule': 'momentum_flip',
                                   'detail': f'动能翻空 20d: {pr20:+.1f}%->{cr20:+.1f}%',
                                   'severity': 'high'})
            elif pr20 < 0 and cr20 > 0:
                anomalies.append({'code': code, 'name': name, 'rule': 'momentum_flip',
                                   'detail': f'动能翻多 20d: {pr20:+.1f}%->{cr20:+.1f}%',
                                   'severity': 'high'})

        cr60, pr60 = row.get('ret_60d'), prev.get('ret_60d')
        if cr60 is not None and pr60 is not None:
            d60 = cr60 - pr60
            if abs(d60) >= 5:
                label = '加速' if d60 > 0 else '减速'
                anomalies.append({'code': code, 'name': name, 'rule': 'trend_change',
                                   'detail': f'趋势{label} 60d: {pr60:+.1f}%->{cr60:+.1f}% (d{d60:+.1f}%)',
                                   'severity': 'medium'})
        curr_rank += 1

    return anomalies


def format_anomaly_report(anomalies: list, prev_date: str) -> str:
    """兼容旧调用。"""
    if not anomalies:
        return ''
    lines = [f'-- 变动对比 (vs {prev_date}) --']
    for a in sorted(anomalies, key=lambda x: 0 if x['severity'] == 'high' else 1):
        lines.append(f"{a['name']} {a['detail']}")
    return '\n'.join(lines)


def _trend_icon(ret_20d) -> str:
    if ret_20d is None:
        return '-'
    return u'↑' if ret_20d > 0 else (u'↓' if ret_20d < 0 else '->')


def format_comparison_report(curr_snap: dict, prev_snap: dict, anomalies: list) -> str:
    """
    三段式对比报告：
      1. 标题 + 对比周期
      2. Top 10 排名对比表
      3. 最大变化 + 关键发现
    """
    from datetime import datetime as _dt
    curr_date, prev_date = curr_snap['date'], prev_snap['date']
    cd = _dt.strptime(curr_date, '%Y%m%d')
    pd_ = _dt.strptime(prev_date, '%Y%m%d')
    trading_days = round((cd - pd_).days * 5 / 7)

    curr_map = {r['code']: r for r in curr_snap['data']}
    prev_map = {r['code']: r for r in prev_snap['data']}

    def _fs(v):
        return f'{v:+.2f}' if v is not None else 'N/A'

    def _rank_label(change):
        if change > 0: return u'📈'
        if change < 0: return u'📉'
        return ''

    lines = []

    # 标题
    lines.append(f'板块轮动对比分析 ({pd_.month}/{pd_.day} -> {cd.month}/{cd.day})')
    lines.append(f'对比周期: {trading_days}个交易日  |  {prev_date[:4]}-{prev_date[4:6]}-{prev_date[6:]} vs {curr_date[:4]}-{curr_date[4:6]}-{curr_date[6:]}')
    lines.append('')

    # Top 10
    lines.append('Top 10 排名对比')
    lines.append('名称 | 前得分 | 后得分 | 排名变动 | 60日涨幅变化 | 变化')
    top10 = sorted(curr_snap['data'], key=lambda r: r.get('rank', 999))[:10]
    for r in top10:
        code = r['code']
        prev = prev_map.get(code, {})
        cr, pr = r.get('rank', '?'), prev.get('rank', '?')
        chg = (pr - cr) if isinstance(pr, int) and isinstance(cr, int) else 0
        pr60 = f'{prev["ret_60d"]:+.1f}%' if prev.get('ret_60d') is not None else 'N/A'
        cr60 = f'{r["ret_60d"]:+.1f}%' if r.get('ret_60d') is not None else 'N/A'
        lines.append(f'{r["name"]} | {_fs(prev.get("score"))} | {_fs(r.get("score"))} | #{pr}->#{cr} | {pr60}->{cr60} | {_rank_label(chg)}')
    lines.append('')

    # 最大变化
    big = [an for an in anomalies if an['rule'] == 'rank_change']
    if big:
        lines.append('最大变化')
        lines.append('名称 | 代码 | 前排名 | 后排名 | 得分变化 | 趋势变化')
        for a in sorted(big, key=lambda x: -abs(prev_map.get(x['code'], {}).get('rank', 0) - curr_map.get(x['code'], {}).get('rank', 0)))[:10]:
            code = a['code']
            c, p = curr_map.get(code, {}), prev_map.get(code, {})
            pt, ct = _trend_icon(p.get('ret_20d')), _trend_icon(c.get('ret_20d'))
            lines.append(f'{a["name"]} | {code} | #{p.get("rank","?")} | #{c.get("rank","?")} | {_fs(p.get("score"))}->{_fs(c.get("score"))} | {pt}->{ct}')
        lines.append('')

    # 关键发现
    findings = []
    for rule, tag in [('rank_change', '跃升'), ('rank_change', '暴跌'),
                      ('momentum_flip', '翻多'), ('momentum_flip', '翻空')]:
        for a in [x for x in anomalies if x['rule'] == rule and tag in x['detail']][:2]:
            findings.append(f'- {a["name"]} {a["detail"]}')
    if findings:
        lines.append('关键发现:')
        lines.extend(findings)

    return '\n'.join(lines)


# ── 内部补录工具 ─────────────────────────────────────────────────────────────

def _compute_row(futu_code: str, name: str, df: pd.DataFrame,
                 momentum_period: int) -> Optional[dict]:
    """backfill 专用轻量计算（跳过 KD/MACD 等信号）。"""
    if df is None or len(df) < momentum_period:
        return None
    close = df['close']
    try:
        m = calc_momentum(close, momentum_period)
        last_m = float(m.iloc[-1])
        if math.isnan(last_m):
            return None
    except Exception:
        return None
    ret_20d, ret_60d, score = calc_returns_score(close)
    return {'code': futu_code, 'name': name, 'momentum': last_m,
            'ret_20d': ret_20d, 'ret_60d': ret_60d, 'score': score}


def _load_code_pd(config: configparser.ConfigParser, group: str) -> pd.DataFrame:
    """从配置加载标的列表。"""
    from tools import code_in_futu_group
    host = config.get('CONFIG', 'FUTU_HOST')
    port = int(config.get('CONFIG', 'FUTU_PORT'))
    code_list = [c.strip() for c in
                 config.get('CONFIG', 'FUTU_CODE_LIST', fallback='').split(',') if c.strip()]
    code_pd = pd.DataFrame(columns=pd.Index(['code', 'name']))
    if group:
        ls = code_in_futu_group(group, host, port)
        if isinstance(ls, pd.DataFrame):
            code_pd = pd.concat([code_pd, ls[['code', 'name']]])
    if code_list:
        code_pd = pd.concat([code_pd, pd.DataFrame({'code': code_list, 'name': code_list})])
    return code_pd


def _last_trading_date(config: configparser.ConfigParser) -> str:
    """通过拉取第一个标的的K线，获取最近交易日日期字符串(YYYYMMDD)。"""
    from data import get_kline_data
    group = config.get('CONFIG', 'FUTU_GROUP', fallback='')
    code_pd = _load_code_pd(config, group)
    for code in code_pd['code'].values:
        try:
            df = get_kline_data(code, config, max_count=5)
            if df is not None and not df.empty:
                if 'time_key' in df.columns:
                    return pd.to_datetime(df['time_key'].iloc[-1]).strftime('%Y%m%d')
                return pd.to_datetime(df.index[-1]).strftime('%Y%m%d')
        except Exception:
            continue
    return datetime.now().strftime('%Y%m%d')


def _backfill_single(code_pd: pd.DataFrame, config: configparser.ConfigParser,
                     date_str: str, snapshot_dir: str) -> Optional[dict]:
    """
    为单个日期补录快照。若文件已存在直接加载返回；
    否则拉 K 线、截断、计算、保存，返回快照 dict。
    K 线截断到 date_str 当日或之前最近交易日。
    """
    from data import get_kline_data

    ktype = config.get('CONFIG', 'FUTU_PUSH_TYPE')
    group = config.get('CONFIG', 'FUTU_GROUP', fallback='')
    momentum_period = int(config.get('CONFIG', 'MOMENTUM_PERIOD', fallback=21))
    target_dt = datetime.strptime(date_str, '%Y%m%d')
    path = _snapshot_path(group, ktype, date_str, snapshot_dir)

    if path.exists():
        with open(path, 'r', encoding='utf-8') as fp:
            return json.load(fp)

    results = []
    for idx, futu_code in enumerate(code_pd['code'].values):
        try:
            df = get_kline_data(futu_code, config, max_count=1000)
            if df is None or df.empty:
                continue
            if 'time_key' in df.columns:
                df = df.copy()
                df.index = pd.to_datetime(df['time_key'])
            elif not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index)
            if df.index.tz is not None:
                df = df.copy()
                df.index = df.index.tz_localize(None)
            df_slice = df[df.index <= target_dt]
            if df_slice.empty:
                continue
            name = code_pd['name'].iloc[idx]
            row = _compute_row(futu_code, name, df_slice, momentum_period)
            if row:
                results.append(row)
        except Exception as e:
            print(f'  warning: {futu_code} backfill failed: {e}')

    if not results:
        return None

    results_df = (pd.DataFrame(results)
                  .sort_values('momentum', ascending=False)
                  .reset_index(drop=True))
    results_df['rank'] = results_df.index + 1
    results_df = results_df.set_index('code')
    save_snapshot(results_df, group, ktype, snapshot_dir, date_str=date_str)
    with open(path, 'r', encoding='utf-8') as fp:
        return json.load(fp)


# ── 对比主流程 ───────────────────────────────────────────────────────────────

def compare(config: configparser.ConfigParser) -> None:
    """
    对比最近两期快照，必要时自动补录上期。
    配置项：
      SNAPSHOT_DIR           快照目录，默认 snapshots/
      ROTATION_LOOKBACK_DAYS 向前查找天数，默认 7
    """
    from notification_engine import NotificationEngine

    group = config.get('CONFIG', 'FUTU_GROUP', fallback='')
    push_type = config.get('CONFIG', 'FUTU_PUSH_TYPE')
    snapshot_dir = config.get('CONFIG', 'SNAPSHOT_DIR', fallback=SNAPSHOT_DIR)
    lookback_days = int(config.get('CONFIG', 'ROTATION_LOOKBACK_DAYS', fallback='7'))
    group_key = group or 'default'

    # Step 1: 找最新快照作为本期，不存在则自动补录
    curr_snap = _load_latest(group_key, push_type, snapshot_dir)
    if not curr_snap:
        print('未找到任何快照，自动补录当期...')
        code_pd = _load_code_pd(config, group)
        if code_pd.empty:
            print('error: 无标的，请检查配置中 FUTU_GROUP / FUTU_CODE_LIST')
            return
        curr_date_str = _last_trading_date(config)
        curr_snap = _backfill_single(code_pd, config, curr_date_str, snapshot_dir)
        if not curr_snap:
            print(f'补录失败: {curr_date_str}')
            return
        print(f'当期补录完成: {curr_date_str}')
    curr_date = datetime.strptime(curr_snap['date'], '%Y%m%d')
    target_str = (curr_date - timedelta(days=lookback_days)).strftime('%Y%m%d')

    # Step 2: 找上期快照，不存在则自动补录
    prev_snap = load_previous_snapshot(group_key, push_type, curr_date, snapshot_dir, lookback_days)
    if not prev_snap:
        print(f'未找到 ~{lookback_days} 天前快照，自动补录 {target_str} ...')
        code_pd = _load_code_pd(config, group)
        if code_pd.empty:
            print('error: 无标的，请检查配置中 FUTU_GROUP / FUTU_CODE_LIST')
            return
        prev_snap = _backfill_single(code_pd, config, target_str, snapshot_dir)
        if not prev_snap:
            print(f'补录失败: {target_str}，无法对比')
            return
        print(f'补录完成: {target_str}')

    # Step 3: 异常检测 + 推送
    curr_df = pd.DataFrame(curr_snap['data']).set_index('code')
    anomalies = detect_anomalies(curr_df, prev_snap)
    report = format_comparison_report(curr_snap, prev_snap, anomalies)

    if not report:
        print(f'无异常变动 (vs {prev_snap["date"]})')
        return

    print(report)
    notification = NotificationEngine(config)
    title = f"{curr_snap['date']} {group} {push_type} 轮动对比"
    notification.send_telegram_message(report, 'https://www.futunn.com/')
    notification.send_email(title, report)
    notification.send_webhook(f'{title}\n{report}')


# ── CLI 入口 ─────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    from ft_config import get_config
    config = get_config()
    compare(config)
