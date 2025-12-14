#!/usr/bin/env python3
"""
Test direct 15m bar fetching from IBKR to verify accurate price detection.
"""
import asyncio
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from core.ibkr.client import IBKRClient
from core.signal_engine import prepare_bars_with_indicators, detect_15m_bias
from core.logger import logger


async def test_direct_15m_fetch():
    """Test fetching 15m bars directly from IBKR."""
    
    client = IBKRClient()
    
    try:
        logger.info("🔌 Connecting to IBKR...")
        await client.connect()
        
        test_symbols = ["META", "AAPL", "TSLA"]
        
        for symbol in test_symbols:
            logger.info(f"\n{'='*60}")
            logger.info(f"Testing {symbol}")
            logger.info(f"{'='*60}")
            
            # Fetch direct 15m bars
            logger.info(f"📥 Fetching 15m bars for {symbol}...")
            df15_raw = await client.get_historical_bars_direct(
                symbol, 
                bar_size="15 mins", 
                duration_str="1 D"
            )
            
            if df15_raw is None or df15_raw.empty:
                logger.error(f"❌ No data returned for {symbol}")
                continue
            
            logger.info(f"✅ Fetched {len(df15_raw)} bars")
            logger.info(f"📊 Last 3 bars:")
            print(df15_raw[['open', 'high', 'low', 'close', 'volume']].tail(3))
            
            # Add indicators
            logger.info(f"\n📈 Adding indicators...")
            df15_prepared = prepare_bars_with_indicators(df15_raw, timeframe="15min")
            
            if not df15_prepared.empty:
                logger.info(f"✅ Prepared {len(df15_prepared)} complete bars with indicators")
                logger.info(f"📊 Latest bar with indicators:")
                last_bar = df15_prepared.iloc[-1]
                print(f"  Close: ${last_bar['close']:.2f}")
                print(f"  EMA50: ${last_bar['ema50']:.2f}")
                print(f"  VWAP: ${last_bar['vwap']:.2f}")
                print(f"  MACD: {last_bar['macd_hist']:.4f}")
                print(f"  RSI: {last_bar['rsi']:.2f}")
                print(f"  SuperTrend: {last_bar['supertrend']}")
                
                # Detect 15m bias
                logger.info(f"\n🎯 Detecting 15m bias...")
                bias = detect_15m_bias(df15_prepared, symbol=symbol)
                if bias:
                    logger.info(f"✅ Detected {bias} bias")
                else:
                    logger.info(f"❌ No clear bias detected")
            else:
                logger.warning(f"⚠️ No complete bars after filtering")
        
    except Exception as e:
        logger.exception(f"❌ Test failed: {e}")
    finally:
        logger.info("\n🔌 Disconnecting...")
        await client.disconnect()


if __name__ == "__main__":
    logger.info("🧪 Starting direct 15m fetch test...")
    asyncio.run(test_direct_15m_fetch())
    logger.info("✅ Test completed")
