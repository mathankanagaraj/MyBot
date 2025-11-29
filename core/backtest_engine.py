# core/backtest_engine.py
import pandas as pd
from core.indicators import add_indicators
from datetime import timedelta
from core.worker import compute_stop_target, risk_per_contract_value, rr_ratio_value

def load_csv(path):
    df = pd.read_csv(path, parse_dates=['timestamp']).set_index('timestamp')
    return df

def simulate_symbol(df5, df15):
    df5 = add_indicators(df5)
    df15 = add_indicators(df15)
    trades = []
    for i in range(30, len(df15)):
        slice15 = df15.iloc[:i+1]
        last15 = slice15.iloc[-2]; prev15 = slice15.iloc[-3]
        cond_bear = (last15['close'] < last15['ema50']) and (last15['close'] < last15['vwap']) and (last15['macd_hist'] < 0 and last15['macd_hist'] < prev15['macd_hist']) and (last15['obv'] < prev15['obv'])
        cond_bull = (last15['close'] > last15['ema50']) and (last15['close'] > last15['vwap']) and (last15['macd_hist'] > 0 and last15['macd_hist'] > prev15['macd_hist']) and (last15['obv'] > prev15['obv'])
        bias = 'BEAR' if cond_bear else ('BULL' if cond_bull else None)
        if not bias:
            continue
        t15 = slice15.index[-1]
        window5 = df5[(df5.index > t15) & (df5.index <= t15 + timedelta(minutes=25))]
        for j in range(3, len(window5)):
            df5slice = window5.iloc[:j+1]
            last5 = df5slice.iloc[-2]; prev5 = df5slice.iloc[-3]; prev52 = df5slice.iloc[-4]
            # entry check same as live
            ema_short = last5['ema9']; ema_long = last5['ema21']
            if bias == 'BEAR':
                if not (ema_short < ema_long): continue
                pullback = (prev5['close'] > prev5['open']) or (prev52['close'] > prev52['open'])
                entry_candle = last5['close'] < last5['open']
                macd_ok = last5['macd_hist'] < prev5['macd_hist']
                obv_ok = last5['obv'] < prev5['obv']
                high_wick = last5['high'] - max(last5['close'], last5['open'])
                body = abs(last5['close'] - last5['open']) or 1e-9
                wick_ok = (high_wick / body) >= 1.2
                vol_ok = last5['volume'] >= prev5['volume']
                if not all([pullback, entry_candle, macd_ok, obv_ok, wick_ok, vol_ok]): continue
            else:
                if not (ema_short > ema_long): continue
                pullback = (prev5['close'] < prev5['open']) or (prev52['close'] < prev52['open'])
                entry_candle = last5['close'] > last5['open']
                macd_ok = last5['macd_hist'] > prev5['macd_hist']
                obv_ok = last5['obv'] > prev5['obv']
                low_wick = min(last5['close'], last5['open']) - last5['low']
                body = abs(last5['close'] - last5['open']) or 1e-9
                wick_ok = (low_wick / body) >= 1.2
                vol_ok = last5['volume'] >= prev5['volume']
                if not all([pullback, entry_candle, macd_ok, obv_ok, wick_ok, vol_ok]): continue

            entry_under = last5['close']
            entry_prem = max(0.03, 0.001 * entry_under)
            stop, target, risk = compute_stop_target(entry_prem, risk_per_contract_value(entry_prem), rr_ratio_value())
            # scan forward
            forward = df5[df5.index > last5.name].iloc[:60]
            outcome = 'TIMEOUT'; exit_prem = entry_prem
            for k, row in forward.iterrows():
                mult = 5.0
                pct = (row['close'] - entry_under) / entry_under
                if bias == 'BEAR': pct = -pct
                option_now = entry_prem * (1 + pct * mult)
                if option_now <= stop:
                    outcome = 'STOP'; exit_prem = option_now; break
                if option_now >= target:
                    outcome = 'TARGET'; exit_prem = option_now; break
            trades.append({'entry_time': last5.name, 'bias': bias, 'entry_prem': entry_prem, 'exit_prem': exit_prem, 'outcome': outcome})
            break
    return pd.DataFrame(trades)