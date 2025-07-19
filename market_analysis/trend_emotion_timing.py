# %% [markdown]
# 市场行为中存在一种对立现象：在短期基础上，价格倾向于均值回归 (mean revert)，而在中期则能观察到动量效应 (momentum manner)。这对任何趋势跟踪策略都具有本质性的背景影响，因为趋势跟踪信号常常在超买或超卖条件下触发。

# %%
import pandas as pd
import os

# %% [markdown]
# ### 计算40个趋势指标信号​​
# ​​指标类型​​（论文图1）：
# 变化率（RoC）：24/32/48/64/96/128/192/256/384/512日
# 
# 简单移动平均（SMA）：24/32/48/64/96/128/192/256/384/512日
# 
# 均线交叉（Crossover）：(20,400)、(50,400)、(100,400)、(200,400)、(20,200)、(50,200)、(100,200)、(20,100)、(50,100)、(20,50)
# 
# 线性回归斜率（LinearReg）：3/4/5/6/7/8/9/12/15/18日
# ​
# ​信号规则​​：
# 买入信号 → 1.0
# 卖出信号 → -1.0
# 模糊/无信号 → 0.0（如斜率绝对值＜统计误差）
# 
# TrendScore = (1/40) * Σ(TrendIndicator_i)  # i=1 to 40

# %%
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

def rate_of_change(price:pd.Series,N:int):
    # is the price higher than n days ago
    high = price.rolling(window=N).max()
    return np.select([price > high, price < high], [1, -1], default=0)

def simple_moving_average(price:pd.Series,N:int):
    # is the price above or below the SMA
    sma = price.rolling(window=N).mean()
    return np.select([price > sma, price < sma], [1, -1], default=0)

def cross_system(price:pd.Series,N:int,M:int):
    # is shorter SMA above or below the longer SMA
    assert N < M, "N must be less than M"
    sma_N = price.rolling(window=N).mean()
    sma_M = price.rolling(window=M).mean()
    return np.select([sma_N > sma_M, sma_N < sma_M], [1, -1], default=0)

def linear_regression(price:pd.Series,N:int):
    # slope of the regression greater, less or close to 0?
    def linear_regression_slope(y:np.ndarray):
        x = np.arange(len(y))
        cov = np.cov(x, y)
        beta1 = cov[0,1] / cov[0,0]
        residuals = y - (beta1*x + np.mean(y) - beta1*np.mean(x))
        se = np.sqrt(np.sum(residuals**2)/(len(y)-2)) / np.sqrt(np.sum((x-np.mean(x))**2))
        return np.select([beta1 > se, beta1 < -se, np.abs(beta1) < se], [1, -1, 0])
    return price.rolling(window=N).apply(linear_regression_slope)

def trend_score(price:pd.Series):
    roc = [24,32,48,64,96,128,192,256,384,512]
    sma = [24,32,48,64,96,128,192,256,384,512]
    cross = [(20,400),(50,400),(100,400),(200,400),(20,200),(50,200),(100,200),(20,100),(50,100),(20,50)]
    lr = [60,80,100,120,140,160,180,240,300,360] # 3/4/5/6/7/8/9/12/15/18 months
    # roc = [24,32,48,64,96,128,192,256]
    # sma = [24,32,48,64,96,128,192,256]
    # cross = [(20,200),(50,200),(100,200),(20,100),(50,100),(20,50)]
    # lr = [60,80,100,120,140,160,180,240] # 3/4/5/6/7/8/9/12 months
    score = pd.Series(0,index=price.index)
    for i in range(len(roc)):
        score += rate_of_change(price,roc[i])
    for i in range(len(sma)):
        score += simple_moving_average(price,sma[i])
    for i in range(len(cross)):
        score += cross_system(price,cross[i][0],cross[i][1])
    for i in range(len(lr)):
        score += linear_regression(price,lr[i])
    return score / (len(roc) + len(sma) + len(cross) + len(lr))

def backtest_strategy(price: pd.Series, score: pd.Series, 
                     slippage: float = 0.001,  # 滑点 0.1%
                     commission: float = 0.0005,  # 手续费 0.05%
                     t_plus_one: bool = True,  # T+1交易开关
                     initial_capital: float = 100000):  # 初始资金
    
    """
    回测函数：基于score上穿0轴的策略
    """
    
    # 初始化
    capital = initial_capital
    position = 0  # 0: 空仓, 1: 持仓
    shares = 0
    trades = []
    equity_curve = []
    
    # 生成交易信号：score上穿0轴
    signal = pd.Series(0, index=score.index)
    
    # 信号生成逻辑
    for i in range(1, len(score)):
        if score.iloc[i] > 0 and score.iloc[i-1] <= 0:  # 上穿0轴买入
            signal.iloc[i] = 1
        elif score.iloc[i] < 0 and score.iloc[i-1] >= 0:  # 下穿0轴卖出
            signal.iloc[i] = -1
    
    # 回测循环
    for i in range(1, len(price)):
        current_price = price.iloc[i]
        current_signal = signal.iloc[i]
        
        # 计算实际交易价格（考虑滑点）
        if current_signal != 0:
            if current_signal == 1:  # 买入
                trade_price = current_price * (1 + slippage)
            else:  # 卖出
                trade_price = current_price * (1 - slippage)
        else:
            trade_price = current_price
        
        # 执行交易
        if current_signal == 1 and position == 0:  # 买入信号且当前空仓
            # 计算可买入股数
            available_capital = capital * (1 - commission)  # 扣除手续费
            shares = int(available_capital / trade_price)
            capital -= shares * trade_price * (1 + commission)  # 扣除买入费用
            position = 1
            
            trades.append({
                'date': price.index[i],
                'action': 'BUY',
                'price': trade_price,
                'shares': shares,
                'capital': capital,
                'signal': current_signal
            })
            
        elif current_signal == -1 and position == 1:  # 卖出信号且当前持仓
            # 卖出所有股票
            sell_value = shares * trade_price * (1 - commission)  # 扣除手续费
            capital += sell_value
            shares = 0
            position = 0
            
            trades.append({
                'date': price.index[i],
                'action': 'SELL',
                'price': trade_price,
                'shares': shares,
                'capital': capital,
                'signal': current_signal
            })
        
        # 计算当前权益
        current_equity = capital + shares * current_price
        equity_curve.append(current_equity)
    
    # 如果最后还持仓，强制平仓
    if position == 1:
        final_price = price.iloc[-1] * (1 - slippage)  # 考虑滑点
        sell_value = shares * final_price * (1 - commission)
        capital += sell_value
        equity_curve[-1] = capital
    
    # 转换为DataFrame
    equity_df = pd.DataFrame({
        'date': price.index[1:],
        'equity': equity_curve,
        'price': price.iloc[1:],
        'score': score.iloc[1:],
        'signal': signal.iloc[1:]
    }).set_index('date')
    
    trades_df = pd.DataFrame(trades)
    
    return equity_df, trades_df

def plot_backtest_results(price: pd.Series, score: pd.Series, equity_df: pd.DataFrame, trades_df: pd.DataFrame):
    """
    可视化回测结果
    """
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(30, 15))
    
    # 上图：价格和权益曲线
    ax1.plot(price.index, price, label='标的价格', color='blue', alpha=0.7, linewidth=1)
    ax1.plot(equity_df.index, equity_df['equity'], label='策略权益', color='red', linewidth=2)
    
    # 标记买卖点
    if not trades_df.empty:
        buy_trades = trades_df[trades_df['action'] == 'BUY']
        sell_trades = trades_df[trades_df['action'] == 'SELL']
        
        ax1.scatter(buy_trades['date'], buy_trades['price'], 
                   color='green', marker='^', s=100, label='买入信号', zorder=5)
        ax1.scatter(sell_trades['date'], sell_trades['price'], 
                   color='red', marker='v', s=100, label='卖出信号', zorder=5)
    
    ax1.set_title('策略回测结果', fontsize=16, fontweight='bold')
    ax1.set_ylabel('价格/权益', fontsize=12)
    ax1.legend()
    ax1.grid(True, alpha=0.3)
    
    # 下图：信号分数
    ax2.plot(score.index, score, label='信号分数', color='orange', linewidth=1.5)
    ax2.axhline(y=0, color='black', linestyle='--', alpha=0.5, label='0轴')
    
    # 标记信号穿越点
    if not trades_df.empty:
        ax2.scatter(buy_trades['date'], buy_trades['signal'], 
                   color='green', marker='^', s=100, label='买入信号', zorder=5)
        ax2.scatter(sell_trades['date'], sell_trades['signal'], 
                   color='red', marker='v', s=100, label='卖出信号', zorder=5)
    
    ax2.set_title('信号分数', fontsize=16, fontweight='bold')
    ax2.set_ylabel('分数', fontsize=12)
    ax2.set_xlabel('日期', fontsize=12)
    ax2.legend()
    ax2.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.show()

def calculate_performance_metrics(equity_df: pd.DataFrame, initial_capital: float):
    """
    计算策略表现指标
    """
    # 计算收益率
    total_return = (equity_df['equity'].iloc[-1] - initial_capital) / initial_capital
    
    # 计算年化收益率
    days = (equity_df.index[-1] - equity_df.index[0]).days
    annual_return = (1 + total_return) ** (365 / days) - 1 if days > 0 else 0
    
    # 计算最大回撤
    equity_series = equity_df['equity']
    rolling_max = equity_series.expanding().max()
    drawdown = (equity_series - rolling_max) / rolling_max
    max_drawdown = drawdown.min()
    
    # 计算夏普比率（简化版）
    daily_returns = equity_df['equity'].pct_change().dropna()
    sharpe_ratio = daily_returns.mean() / daily_returns.std() * np.sqrt(252) if daily_returns.std() > 0 else 0
    
    # 计算胜率
    if not daily_returns.empty:
        win_rate = (daily_returns > 0).mean()
    else:
        win_rate = 0
    
    return {
        '总收益率': f"{total_return:.2%}",
        '年化收益率': f"{annual_return:.2%}",
        '最大回撤': f"{max_drawdown:.2%}",
        '夏普比率': f"{sharpe_ratio:.2f}",
        '胜率': f"{win_rate:.2%}"
    }

# 执行回测
def run_backtest(price: pd.Series, score: pd.Series, 
                slippage: float = 0.001, 
                commission: float = 0.0005, 
                t_plus_one: bool = True,
                initial_capital: float = 100000):
    """
    执行完整回测流程
    """
    print("开始回测...")
    print(f"参数设置: 滑点={slippage:.3f}, 手续费={commission:.4f}, T+1={t_plus_one}")
    
    # 执行回测
    equity_df, trades_df = backtest_strategy(price, score, slippage, commission, t_plus_one, initial_capital)
    
    # 计算表现指标
    metrics = calculate_performance_metrics(equity_df, initial_capital)
    
    # 打印结果
    print("\n=== 策略表现 ===")
    for key, value in metrics.items():
        print(f"{key}: {value}")
    
    print(f"\n总交易次数: {len(trades_df)}")
    if not trades_df.empty:
        print(f"买入次数: {len(trades_df[trades_df['action'] == 'BUY'])}")
        print(f"卖出次数: {len(trades_df[trades_df['action'] == 'SELL'])}")
    
    # 可视化结果
    plot_backtest_results(price, score, equity_df, trades_df)
    
    return equity_df, trades_df, metrics

def score_plot(price:pd.Series,score:pd.Series):
    # 创建图形和主坐标轴
    fig, ax1 = plt.subplots(figsize=(30, 10))
    
    # 根据score值确定颜色
    colors = ['red' if s > 0 else 'green' for s in score]
    
    # 绘制价格数据（左y轴），根据score值染色
    ax1.set_xlabel('date', fontsize=12)
    ax1.set_ylabel('price', fontsize=12)
    
    # 分段绘制价格线，每段使用不同颜色
    for i in range(len(price.index) - 1):
        ax1.plot(price.index[i:i+2], price.iloc[i:i+2], 
                color=colors[i], linewidth=1.5, alpha=0.8)
    
    # 添加价格标签到图例
    from matplotlib.lines import Line2D
    red_line = Line2D([0], [0], color='red', linewidth=1.5, label='price (score > 0)')
    green_line = Line2D([0], [0], color='green', linewidth=1.5, label='price (score ≤ 0)')
    
    ax1.tick_params(axis='y')
    
    # 创建右y轴并绘制分数数据
    ax2 = ax1.twinx()
    color2 = '#ff7f0e'  # 橙色
    ax2.set_ylabel('score', color=color2, fontsize=12)
    line2 = ax2.plot(score.index, score, color=color2, linewidth=1.5, label='score',alpha=0.8)
    ax2.tick_params(axis='y', labelcolor=color2)
    
    # 设置网格
    ax1.grid(True, alpha=0.3)
    
    # 添加图例
    ax1.legend([red_line, green_line, line2[0]], 
              ['price (score > 0)', 'price (score ≤ 0)', 'score'], 
              loc='upper left')
    
    # 设置标题
    plt.title('price and score')
    
    # 调整布局
    plt.tight_layout()
    plt.show()






# %% [markdown]
# ### 基准比较
# Ratio = Stock_Close / Benchmark_Close

# %%
import pandas as pd
import os

def joint_score(stock_price:pd.Series,benchmark_price:pd.Series):
    price_ratio = stock_price / benchmark_price

    trend_score_benchmark  = trend_score(price_ratio)
    trend_score_stock = trend_score(stock_price)
    up_condition = (trend_score_benchmark > 0) & (trend_score_stock > 0)
    down_condition = (trend_score_benchmark < 0) & (trend_score_stock < 0)

    score = np.select(
        [
        up_condition & (trend_score_stock > trend_score_benchmark),
        up_condition & (trend_score_stock < trend_score_benchmark),
        down_condition & (trend_score_stock > trend_score_benchmark),
        down_condition & (trend_score_stock < trend_score_benchmark)
        ],
        [trend_score_benchmark,trend_score_stock,trend_score_stock,trend_score_benchmark],
        default=0
    )
    return pd.Series(index=stock_price.index,data=score)




# %% [markdown]
# ### 计算12个振荡器​​（论文图2）
# ​
# ​类型​​：
# RSI（相对强弱指数）：5/10/14/20日
# 
# K线波动范围（Candle Range）：3/5/8/13日
# ​
# ​标准化​​：将输出缩放到[-1.0, 1.0]
# 
# -1.0：严重超卖
# 1.0：严重超买
# 
# EmotionIndex = (1/12) * Σ(Oscillator_i)  # i=1 to 12

# %%
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

def rescaled_rsi(price:pd.Series,N:int):
    # rescale the classical RSI to a codomain of -1.0 to 1.0
    delta = price.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=N).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=N).mean()
    
    # 避免除零错误
    rs = gain / loss.replace(0, np.inf)  # 将0替换为无穷大
    
    # 计算RSI，处理边界情况
    rsi = 100 - (100 / (1 + rs))
    
    # 处理无穷大和NaN值
    rsi = rsi.replace([np.inf, -np.inf], 100)  # 无穷大时RSI为100
    rsi = rsi.fillna(50)  # NaN值设为中性值50
    
    # 确保值域在0-100之间
    rsi = np.clip(rsi, 0, 100)
    
    # 缩放到-1到1
    return (rsi - 50) / 50

def rescaled_candle_range(price:pd.Series,N:int):
    # Compute the current close in relation to the high EF and low GF of the last N days
    high_ef = price.rolling(window=N).max()
    low_gf = price.rolling(window=N).min()
    candle_range = (price - low_gf) / (high_ef - low_gf)
    return 2*candle_range - 1

def score_emotion(price:pd.Series):
    rrsi = [5,10,14,20]
    rcr = [3,5,8,13]
    score = pd.Series(0,index=price.index)
    for i in range(len(rrsi)):
        score += rescaled_rsi(price,rrsi[i])
    for i in range(len(rcr)):
        score += rescaled_candle_range(price,rcr[i])
    return score / (len(rrsi) + len(rcr))

# %% [markdown]
# ### 择时模块
# 在情绪中性时评估趋势

# %%
def anchored_trend_score(joint_score:pd.Series,score_emotion:pd.Series):
    update_condition = ((score_emotion > 0) & (score_emotion.shift(1) < 0)) | ((score_emotion < 0) & (score_emotion.shift(1) > 0))
    anchored_trend = joint_score.copy()
    anchored_trend[update_condition] = joint_score[update_condition]
    anchored_trend[~update_condition] = anchored_trend.shift(1)
    return anchored_trend

def timing_indicator(anchored_score:pd.Series,score_emotion:pd.Series):
    # 交易信号：
    # 多头：Timing_Indicator > 1.0（上升趋势+超卖）
    # 空头：Timing_Indicator < -1.0（下降趋势+超买）
    timing_indicator = anchored_score - score_emotion
    return np.select([timing_indicator > 1,timing_indicator < -1], [1, -1], default=0)

def trend_emotion_timing(price:pd.Series,score_emotion:pd.Series):
    score_trend = trend_score(price)
    anchored_score = anchored_trend_score(score_trend,score_emotion)
    timing = timing_indicator(anchored_score,score_emotion)
    return pd.Series(index=price.index,data=timing)


# %%
if __name__ == "__main__":
    # 数据文件路径
    data_dir = './data'
    csv_filename = 'detect/data_US_SOXX_K_DAY.csv'
    csv_path = os.path.join(data_dir, csv_filename)

    # 读取本地CSV文件
    df_stock = pd.read_csv(csv_path, index_col=0, parse_dates=True)
    print(df_stock.tail(10))

    # 趋势指标
    score_trend = trend_score(df_stock['close'])
    score_plot(df_stock['close'],score_trend)

    equity_df, trades_df, metrics = run_backtest(df_stock['close'], score_trend)

    # # 基准数据文件路径
    # data_dir = './data'
    # csv_filename = 'detect/data_US_SPY_K_DAY.csv'
    # csv_path = os.path.join(data_dir, csv_filename)

    # # 读取本地CSV文件
    # df_benchmark = pd.read_csv(csv_path, index_col=0, parse_dates=True)
    # score_benchmark = trend_score(df_benchmark['close'])
    # score_plot(df_benchmark['close'],score_benchmark)

    # # 价格比率
    # score_ratio = trend_score(df_stock['close'] / df_benchmark['close'])
    # score_plot(df_stock['close'] / df_benchmark['close'],score_ratio)

    # # 基准联合指标
    # score_joint = joint_score(df_stock['close'],df_benchmark['close'])
    # score_plot(df_stock['close'],score_joint)

    # # 情绪指标
    # score_emotion = score_emotion(df_stock['close'])
    # score_plot(df_stock['close'],score_emotion)

    # # 择时指标
    # timing = trend_emotion_timing(df_stock['close'],score_emotion)
    # score_plot(df_stock['close'],timing)
    