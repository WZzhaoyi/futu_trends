"""上线前检查（非日常测试）：在线引擎逐根推送 2023-02-01 至快照末尾，成交必须与回测 replay 逐笔一致。

覆盖单元测试窗口之外的少见情形：长假前后的冷却与 T+1、全部往返的状态重建、逐月阈值切换、保留历史的滚动截断。
约需 25 分钟（每根 K 线重算一次 M1 特征）；仅在 build_signals、Executor 或 LiveEngine 决策逻辑变化时重跑。
不连接 Futu，不发通知。证据写入 output/csi_flow_m2_live_e2e。日常测试见 test_csi_flow_m2_live.py。
"""
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT / 'market_analysis'))
import csi_flow_timing_m2 as m2  # noqa: E402
from test_csi_flow_m2_live import BARS, PANEL, ScheduleProvider, fills, run_engine  # noqa: E402

OUT = ROOT / 'output/csi_flow_m2_live_e2e'
START = '2023-02-01 09:45:00'


def main():
    began = time.time()
    rows = json.loads(BARS.read_text())['bars']
    keys = [r['time_key'] for r in rows]
    panel = m2.load_etf_panel(PANEL)
    _, trades, _, _ = m2.replay(BARS, START, etf_panel=panel)
    expected = [(t.side, t.signal, t.execution_bar_end, t.symbol) for t in trades.itertuples()]
    events = []
    with tempfile.TemporaryDirectory() as tmp:
        engine = run_engine(rows, keys.index(START), len(rows), Path(tmp) / 'state.json',
                            ScheduleProvider(m2.schedule_for()), panel, events)
        final_state = json.loads((Path(tmp) / 'state.json').read_text())
    got = fills(events)
    errors = [e for e in events if e['type'] == 'ERROR']
    mismatch = next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), None)
    status = 'PASS' if got == expected and not errors else 'FAIL'
    evidence = dict(status=status, bars=len(rows) - keys.index(START), fills=len(got), expected=len(expected),
                    first_mismatch=None if mismatch is None else dict(live=got[mismatch], replay=expected[mismatch]),
                    errors=[e['message'] for e in errors][:5], final_state=final_state,
                    seconds=round(time.time() - began, 1))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'verification.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    assert status == 'PASS', evidence


if __name__ == '__main__':
    main()
