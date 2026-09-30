"""Growth-value fundamental screener.

除成长/质量条件外，本策略用 **P/FCF ≤ 11** 作为便宜度闸门。它分两级落地：

L1 预筛（服务端，一行）：
  - `PCF_TTM ≤ 1200`（三市场统一）。Futu 的 PCF_TTM 是**百分比刻度**，等于
    (总市值 ÷ 经营现金流TTM) × 100，故这里对应 P/OCF ≤ 12。
  - 预筛上限**故意宽于**真正的判据：因 FCF = 经营现金流 − 净资本开支 ≤ 经营现金流，
    通常 P/FCF ≥ P/OCF，预筛必须留余量才不漏股。PCF_TTM_MAX 因此**不参与判据**，
    判据永远是 L2 的 P/FCF ≤ 11；放宽它只增加候选、不放松标准。
  - 经营现金流下限严格 >0：否则 OCF<0 的标的 PCF 为负、≤1200 恒真，
    而它们的 FCF 必然 ≤0，走到 L2 也一定不合格。

L2 精算（F10 现金流表；本策略的 --refine 本来就会拉这张表，故无新增调用）：
  - FCF_TTM = 经营现金流TTM + 固定资产交易净现金流TTM（后者负号=净流出）。
  - 判据用**同一个式子的无量纲形式**，避免市值（交易币种）与报表（报告币种）错配：
        P/FCF = (PCF_TTM ÷ 100) ÷ r,   r = FCF_TTM ÷ OCF_TTM
    r 由同一张现金流表内的两个数相除得到，币种与量纲自动约掉，
    因此**不需要任何汇率表**。

L1 预筛的两处理论例外（均已实测为 0 例误杀，见 crude 说明）：
  - 只有「净资本开支为负」（卖资产多于买资产）时 FCF 才会超过 OCF。
    A 股的 3043、港股的 5071 实测恒为单向外流，故这两个市场**可证不会误杀**；
    美股 Futu 只给 8046「固定资产交易净额」，可为正，故存在极窄的理论窗口
    P/OCF ∈ (11, 11×(1+净流入/OCF)]（实测最大净流入为 OCF 的 3%）。
    PCF_TTM_MAX = 1200 给出的 9% 余量已完整覆盖该窗口，三市场统一，不再分市场设值。
  - PCF_TTM 字段缺失的标的会被 L1 静默排除（数据缺口型误杀，与符号无关）。
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from futu_fundamental_screener import (  # noqa: E402
    POSITIVE_MIN,
    FutuFinancials,
    accumulate_filter,
    financial_filter,
    generic_futu_refine,
    main,
    num,
    ratio,
    safe_inv,
    simple_filter,
)

NAME = "growth_value"
DESCRIPTION = "Growth-value fundamental screener (P/FCF <= 11)"

MARKET_CAP_MIN = {"US": 10e9, "HK": 10e9, "A": 10e9}
TURNOVER_AVG_DAYS = 20
TURNOVER_MIN = {"US": 50e6, "HK": 5e6, "A": 50e6}
PE_MIN = POSITIVE_MIN
PE_MAX = 35.0
PB_MAX = 5.0
ROE_MIN = 8.0
# 增速下限也统一用 POSITIVE_MIN。原本写 0.0（业务上读作「不收缩」），但在百分比
# 口径上 0.01 只是 0.01%，实测三市场候选池内两者结果完全相同（A 67 / HK 76 /
# US 47）—— 全市场落在 (0, 0.01] 的那 2 只 A 股被市值门槛挡在池外。
# 统一后与其余「为正」下限保持同一写法，不再有两种看似相同的 0。
REV_GROWTH_MIN = POSITIVE_MIN
PROFIT_GROWTH_MIN = POSITIVE_MIN
DEBT_ASSET_MAX = 60.0

# ---- P/FCF ≤ 11（便宜度闸门）----
# Futu 的 PCF_TTM 是百分比刻度：(市值 ÷ 经营现金流TTM) × 100。
# L1 预筛上限取 1200（⇔ P/OCF ≤ 12），三市场统一：它比判据（P/FCF ≤ 11 ⇔ P/OCF ≤ 11）
# 宽出 9%，用来覆盖「净资本开支为负」时 FCF > OCF 的那个理论窗口。
# 放宽它只让更多候选进 L2，**不放松判据** —— 判据在 l2_passes 里，恒为 P/FCF ≤ 11。
PCF_TTM_MAX = 1200.0
# PCF_TTM 与经营现金流的「为正」下限都用 POSITIVE_MIN：
# 实测 1e-9 及以下会被 Futu 量化成 0，实际等价于「≥0」，起不到「为正」的作用。
PCF_TTM_MIN = POSITIVE_MIN
# 经营现金流必须为正：OCF=0 时 PCF 无定义、FCF 必 ≤0，负值则 PCF 为负、预筛恒真。
OCF_TTM_MIN = POSITIVE_MIN
PFCF_MAX = 11.0


def build_filters(market: str, ft):
    sf = ft.StockField
    q = ft.FinancialQuarter.ANNUAL
    return [
        simple_filter(sf.MARKET_VAL, MARKET_CAP_MIN[market]),
        accumulate_filter(
            sf.TURNOVER, TURNOVER_MIN[market], days=TURNOVER_AVG_DAYS,
        ),
        simple_filter(sf.PE_TTM, PE_MIN, PE_MAX),
        simple_filter(sf.PB_RATE, POSITIVE_MIN, PB_MAX),
        # P/OCF ≤ 11 预筛：P/FCF ≤ 11 的必要条件
        simple_filter(sf.PCF_TTM, PCF_TTM_MIN, PCF_TTM_MAX),
        financial_filter(sf.RETURN_ON_EQUITY_RATE, ROE_MIN, quarter=q),
        financial_filter(sf.SUM_OF_BUSINESS_GROWTH, REV_GROWTH_MIN, quarter=q),
        financial_filter(sf.NET_PROFIX_GROWTH, PROFIT_GROWTH_MIN, quarter=q),
        financial_filter(sf.OPERATING_CASH_FLOW_TTM, OCF_TTM_MIN, quarter=q),
        financial_filter(sf.DEBT_ASSET_RATE, max_=DEBT_ASSET_MAX, quarter=q),
    ]


def score_snapshot(candidate, snap):
    turnover = num(snap.get("turnover")) or 0
    roe = ratio(snap.get("net_profit"), snap.get("net_asset"), 100) or 0
    earnings_yield = safe_inv(snap.get("pe_ttm_ratio") or snap.get("pe_ratio"), 100) or 0
    book_discount = safe_inv(snap.get("pb_ratio")) or 0
    dividend = num(snap.get("dividend_ratio_ttm")) or 0
    liquidity = min(math.log10(turnover + 1) * 8, 80) if turnover else 0
    score = roe + earnings_yield * 1.5 + book_discount * 8 + dividend * 0.5 + liquidity * 0.2
    return {"snapshot_roe": round(roe, 4), "snapshot_score": round(score, 3)}


L2_MIN_PIOTROSKI = 4
FUTU_L2_STATEMENT_TYPES = ("income", "balance", "cashflow")


def refine_futu(candidate, financials: FutuFinancials):
    """通用 Piotroski 精算 + 本策略的 P/FCF 精算。"""
    result = generic_futu_refine(candidate, financials)
    result.update(_fcf_metrics(candidate, financials))
    return result


def _fcf_metrics(candidate, financials: FutuFinancials) -> dict:
    """算 FCF_TTM 与 P/FCF。所有口径均为 TTM，与 L1 的 PCF_TTM 对齐。"""
    latest = financials.latest("income", "balance", "cashflow")
    if not latest:
        return {
            "fcf_ok": False,
            "fcf_note": "no common report period across income, balance and cashflow",
        }
    fields = financials.fields_for(*latest)

    ocf_ttm = financials.ttm("cashflow", fields.operating_cash_flow)
    net_fixed_cf_ttm = financials.net_fixed_asset_cash_flow_ttm(fields)
    if ocf_ttm is None or net_fixed_cf_ttm is None:
        return {
            "fcf_ok": False,
            "fcf_note": "缺少经营现金流或固定资产交易的 TTM 组成项",
        }
    fcf_ttm = ocf_ttm + net_fixed_cf_ttm
    pcf_ttm = num(candidate.get("pcf_ttm"))
    out = {
        "fcf_ok": True,
        "ocf_ttm": ocf_ttm,
        "net_fixed_asset_cf_ttm": net_fixed_cf_ttm,
        "fcf_ttm": fcf_ttm,
        "fcf_over_ocf": round(fcf_ttm / ocf_ttm, 6) if ocf_ttm else None,
    }
    if ocf_ttm <= 0 or fcf_ttm <= 0 or pcf_ttm is None:
        # FCF ≤ 0 时 P/FCF 无意义；缺 PCF_TTM 无法验证，一律判不合格。
        out["p_over_fcf"] = None
        return out
    # 无量纲形式：币种在 r 里约掉，无需汇率表。
    ratio_fcf_ocf = fcf_ttm / ocf_ttm
    out["p_over_fcf"] = round((pcf_ttm / 100) / ratio_fcf_ocf, 4)
    # 判据的等价写法，便于核对：P/FCF ≤ PFCF_MAX ⇔ PCF_TTM ≤ 100 × PFCF_MAX × r。
    # 这里必须用 PFCF_MAX，**不能**用 L1 的 PCF_TTM_MAX —— 后者只是更宽的预筛上限，
    # 跟着它漂会让这个字段失去"判据阈值"的含义。
    out["pcf_ttm_ceiling"] = round(100 * PFCF_MAX * ratio_fcf_ocf, 2)
    return out


def l2_reject_reason(candidate) -> str | None:
    """L2 被拒的第一条原因；None 表示通过。

    按判据的求值顺序归因，所以每个原因是「走到该阶段才被拦下」的**边际**筛选率，
    不是各自独立的并集。仅用于计数与归因，判据以 l2_passes 为准。
    """
    l2 = candidate.get("l2") or {}
    if not l2.get("ok"):
        return "l2_unavailable"
    if l2.get("piotroski_like_available") != 8:
        return "piotroski_incomplete"
    score = l2.get("piotroski_like_score")
    if score is None or score < L2_MIN_PIOTROSKI:
        return "piotroski_below_threshold"
    if not l2.get("fcf_ok"):
        return "fcf_unavailable"  # 取不到 FCF 组成项 → 不能算合格
    p_over_fcf = l2.get("p_over_fcf")
    if p_over_fcf is None:
        return "fcf_non_positive"
    if p_over_fcf > PFCF_MAX:
        return "pfcf_above_max"
    return None


def l2_passes(candidate) -> bool:
    """--refine L2 门槛：Piotroski 式 8 项齐全且得分 ≥4，且 P/FCF ≤ 11。"""
    return l2_reject_reason(candidate) is None


if __name__ == "__main__":
    main(sys.modules[__name__])
