import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from typing import Tuple, Dict

class BacktestEngine:
    """Simple backtesting engine for trend following strategies"""
    
    def __init__(self, initial_capital: float = 100000):
        self.initial_capital = initial_capital
        self.reset()
    
    def reset(self):
        """Reset backtest state"""
        self.capital = self.initial_capital
        self.position = 0  # 0: flat, 1: long
        self.shares = 0
        self.trades = []
        self.equity_curve = []
    
    def execute_trade(self, date, action: str, price: float, shares: int, signal: int):
        """Record trade execution"""
        self.trades.append({
            'date': date,
            'action': action,
            'price': price,
            'shares': shares,
            'capital': self.capital,
            'signal': signal
        })
    
    def run_backtest(self, price: pd.Series, signal: pd.Series, 
                    slippage: float = 0.001, commission: float = 0.0005) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """Run backtest with given parameters"""
        self.reset()
        
        for i in range(1, len(price)):
            current_price = price.iloc[i]
            current_signal = signal.iloc[i]
            
            # Calculate trade price with slippage
            if current_signal != 0:
                trade_price = current_price * (1 + slippage if current_signal == 1 else 1 - slippage)
            else:
                trade_price = current_price
            
            # Execute buy signal
            if current_signal == 1 and self.position == 0:
                available_capital = self.capital * (1 - commission)
                self.shares = int(available_capital / trade_price)
                self.capital -= self.shares * trade_price * (1 + commission)
                self.position = 1
                self.execute_trade(price.index[i], 'BUY', trade_price, self.shares, current_signal)
            
            # Execute sell signal
            elif current_signal == -1 and self.position == 1:
                sell_value = self.shares * trade_price * (1 - commission)
                self.capital += sell_value
                self.shares = 0
                self.position = 0
                self.execute_trade(price.index[i], 'SELL', trade_price, 0, current_signal)
            
            # Calculate current equity
            current_equity = self.capital + self.shares * current_price
            self.equity_curve.append(current_equity)
        
        # Close final position if needed
        if self.position == 1:
            final_price = price.iloc[-1] * (1 - slippage)
            sell_value = self.shares * final_price * (1 - commission)
            self.capital += sell_value
            self.equity_curve[-1] = self.capital
        
        # Create results DataFrames
        equity_df = pd.DataFrame({
            'date': price.index[1:],
            'equity': self.equity_curve,
            'price': price.iloc[1:],
            'signal': signal.iloc[1:]
        }).set_index('date')
        
        trades_df = pd.DataFrame(self.trades)
        
        return equity_df, trades_df

def calculate_metrics(equity_df: pd.DataFrame, initial_capital: float) -> Dict[str, str]:
    """Calculate performance metrics"""
    total_return = (equity_df['equity'].iloc[-1] - initial_capital) / initial_capital
    
    days = (equity_df.index[-1] - equity_df.index[0]).days
    annual_return = (1 + total_return) ** (365 / days) - 1 if days > 0 else 0
    
    equity_series = equity_df['equity']
    rolling_max = equity_series.expanding().max()
    drawdown = (equity_series - rolling_max) / rolling_max
    max_drawdown = drawdown.min()
    
    daily_returns = equity_df['equity'].pct_change().dropna()
    sharpe_ratio = daily_returns.mean() / daily_returns.std() * np.sqrt(252) if daily_returns.std() > 0 else 0
    win_rate = (daily_returns > 0).mean() if not daily_returns.empty else 0
    
    return {
        'Total Return': f"{total_return:.2%}",
        'Annual Return': f"{annual_return:.2%}",
        'Max Drawdown': f"{max_drawdown:.2%}",
        'Sharpe Ratio': f"{sharpe_ratio:.2f}",
        'Win Rate': f"{win_rate:.2%}"
    }

def plot_results(price: pd.Series, equity_df: pd.DataFrame, trades_df: pd.DataFrame):
    """Plot backtest results"""
    fig, ax = plt.subplots(figsize=(30, 10))
    
    # Plot price and equity
    ax.plot(price.index, price, label='Price', color='blue', alpha=0.7, linewidth=1)
    ax.plot(equity_df.index, equity_df['equity'], label='Strategy Equity', color='red', linewidth=2)
    
    # Mark trade points
    if not trades_df.empty:
        buy_trades = trades_df[trades_df['action'] == 'BUY']
        sell_trades = trades_df[trades_df['action'] == 'SELL']
        
        ax.scatter(buy_trades['date'], buy_trades['price'], 
                  color='green', marker='^', s=100, label='Buy Signal', zorder=5)
        ax.scatter(sell_trades['date'], sell_trades['price'], 
                  color='red', marker='v', s=100, label='Sell Signal', zorder=5)
    
    ax.set_title('Backtest Results', fontsize=16, fontweight='bold')
    ax.set_ylabel('Price/Equity', fontsize=12)
    ax.set_xlabel('Date', fontsize=12)
    ax.legend()
    ax.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.show()

def run_backtest(price: pd.Series, signal: pd.Series, 
                slippage: float = 0.001, commission: float = 0.0005,
                initial_capital: float = 100000) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, str]]:
    """Run complete backtest with visualization"""
    print("Starting backtest...")
    print(f"Parameters: Slippage={slippage:.3f}, Commission={commission:.4f}")
    
    # Run backtest
    engine = BacktestEngine(initial_capital)
    equity_df, trades_df = engine.run_backtest(price, signal, slippage, commission)
    
    # Calculate metrics
    metrics = calculate_metrics(equity_df, initial_capital)
    
    # Print results
    print("\n=== Performance Metrics ===")
    for key, value in metrics.items():
        print(f"{key}: {value}")
    
    print(f"\nTotal Trades: {len(trades_df)}")
    if not trades_df.empty:
        print(f"Buy Trades: {len(trades_df[trades_df['action'] == 'BUY'])}")
        print(f"Sell Trades: {len(trades_df[trades_df['action'] == 'SELL'])}")
    
    # Plot results
    plot_results(price, equity_df, trades_df)
    
    return equity_df, trades_df, metrics

# Example usage
if __name__ == "__main__":
    # Example signal generation function
    def generate_cross_signals(score: pd.Series) -> pd.Series:
        """Generate signals based on score crossing zero"""
        signal = pd.Series(0, index=score.index)
        for i in range(1, len(score)):
            if score.iloc[i] > 0 and score.iloc[i-1] <= 0:
                signal.iloc[i] = 1
            elif score.iloc[i] < 0 and score.iloc[i-1] >= 0:
                signal.iloc[i] = -1
        return signal
    
    # Usage example:
    # signal = generate_cross_signals(score)
    # equity_df, trades_df, metrics = run_backtest(price, signal)
    pass 