import akshare as ak
from datetime import datetime

fund_etf_fund_daily_em_df = ak.fund_etf_fund_daily_em()
print(fund_etf_fund_daily_em_df)

code = '159941'
date_str = '2025-11-20'

print(fund_etf_fund_daily_em_df[fund_etf_fund_daily_em_df['基金代码'] == code].iloc[0])

# 计算实时溢价率 公式：价格 / 净值 - 1
# 美股场内etf 净值使用昨日 当日净值t+1发布
price = fund_etf_fund_daily_em_df[fund_etf_fund_daily_em_df['基金代码'] == code].iloc[0]['市价']
nav = fund_etf_fund_daily_em_df[fund_etf_fund_daily_em_df['基金代码'] == code].iloc[0][f'{date_str}-单位净值']
premium_rate = float(price) / float(nav) - 1
print(f"实时溢价率: {premium_rate}")