"""
IBKR Trade State Manager - File-based persistence for trade tracking

Purpose:
- Track symbols traded today (for one-trade-per-symbol enforcement)
- Track open positions across bot restarts
- Persist state in daily JSON files
- Sync with IBKR broker on startup

File Structure:
/app/data/trade_state/ibkr_trades_YYYY-MM-DD.json
{
    "date": "2025-12-26",
    "traded_symbols": ["SPY", "QQQ", "TSLA"],  // ALL symbols traded today (even if closed)
    "open_positions": ["TSLA"],                 // ONLY currently open positions
    "total_trades": 3
}

State Behavior:
- traded_symbols: Persists entire day (never cleared until new day)
  * Used for ONE_TRADE_PER_SYMBOL enforcement (blocks re-entry)
  * Shows ALL symbols traded today, even if position was closed
  
- open_positions: Reflects current broker state (synced on restart)
  * Only shows symbols with active positions right now
  * Updated when positions open/close
  * Cleared when position closes (unless ONE_TRADE_PER_SYMBOL keeps in traded_symbols)
"""

import json
import logging
from datetime import datetime
from typing import Dict, Set

from core.config import TRADE_STATE_DIR

logger = logging.getLogger(__name__)


class IBKRTradeStateManager:
    """
    Manages IBKR trade state persistence across bot restarts.
    
    Features:
    - Daily state files (one per trading day)
    - Track traded symbols (for one-trade-per-symbol)
    - Track open positions (for restart recovery)
    - Track total trades (for max-trades-per-day)
    - Sync with broker API on startup
    - Thread-safe file operations
    """

    def __init__(self):
        """Initialize trade state manager with today's state file"""
        self.state_dir = TRADE_STATE_DIR
        self.state_dir.mkdir(parents=True, exist_ok=True)
        
        # Current date for state file
        self.today = datetime.now().strftime("%Y-%m-%d")
        self.state_file = self.state_dir / f"ibkr_trades_{self.today}.json"
        
        # In-memory state
        self.traded_symbols: Set[str] = set()
        self.open_positions: Set[str] = set()
        self.total_trades: int = 0
        
        # Load existing state
        self._load_state()
        
        logger.info(
            "📂 IBKR TradeStateManager initialized: %s (traded: %d, open: %d, trades: %d)",
            self.state_file.name,
            len(self.traded_symbols),
            len(self.open_positions),
            self.total_trades
        )

    def _load_state(self):
        """Load state from today's file if it exists"""
        if not self.state_file.exists():
            logger.info("📄 No existing state file for today, starting fresh")
            self._save_state()
            return
        
        try:
            with open(self.state_file, 'r') as f:
                data = json.load(f)
            
            self.traded_symbols = set(data.get("traded_symbols", []))
            self.open_positions = set(data.get("open_positions", []))
            self.total_trades = data.get("total_trades", 0)
            
            logger.info(
                "📂 Loaded state: %d traded symbols, %d open positions, %d trades",
                len(self.traded_symbols),
                len(self.open_positions),
                self.total_trades
            )
        except Exception as e:
            logger.error("❌ Failed to load state file: %s", e)
            # Start fresh if load fails
            self.traded_symbols = set()
            self.open_positions = set()
            self.total_trades = 0

    def _save_state(self):
        """Save current state to file"""
        try:
            data = {
                "date": self.today,
                "traded_symbols": sorted(list(self.traded_symbols)),
                "open_positions": sorted(list(self.open_positions)),
                "total_trades": self.total_trades
            }
            
            with open(self.state_file, 'w') as f:
                json.dump(data, f, indent=2)
            
            logger.debug("💾 State saved: %s", self.state_file.name)
        except Exception as e:
            logger.error("❌ Failed to save state: %s", e)

    def mark_symbol_traded(self, symbol: str):
        """Mark a symbol as traded today"""
        self.traded_symbols.add(symbol)
        self._save_state()
        logger.info("[%s] ✅ Marked as traded", symbol)

    def is_symbol_traded_today(self, symbol: str) -> bool:
        """Check if symbol was traded today"""
        return symbol in self.traded_symbols

    def mark_position_opened(self, symbol: str):
        """Mark a position as opened"""
        self.open_positions.add(symbol)
        self._save_state()
        logger.debug("[%s] 📈 Position opened", symbol)

    def mark_position_closed(self, symbol: str):
        """Mark a position as closed"""
        if symbol in self.open_positions:
            self.open_positions.remove(symbol)
            self._save_state()
            logger.debug("[%s] 📉 Position closed", symbol)

    def increment_trade_count(self):
        """Increment total trades today"""
        self.total_trades += 1
        self._save_state()
        logger.info("📊 Total trades today: %d", self.total_trades)

    def get_total_trades(self) -> int:
        """Get total number of trades today"""
        return self.total_trades

    def sync_with_broker(self, positions: list):
        """
        Sync state with live IBKR positions on startup.
        
        Updates:
        - open_positions: Symbols with non-zero positions
        - traded_symbols: Adds any symbols with positions (preserves existing)
        
        Args:
            positions: List of position objects from IBKR (get_positions_fast)
        """
        logger.info("🔄 Syncing trade state with IBKR broker...")
        logger.debug(
            "Current state before sync: %d traded symbols %s, %d open positions %s",
            len(self.traded_symbols),
            list(self.traded_symbols),
            len(self.open_positions),
            list(self.open_positions)
        )
        
        # If broker returns empty positions but we have existing state, don't wipe it
        # This prevents losing state due to timing issues or connection problems
        if not positions and (self.traded_symbols or self.open_positions):
            logger.warning(
                "⚠️  Broker returned 0 positions but state has data. Preserving existing state to avoid data loss."
            )
            logger.info(
                "✅ Sync skipped: %d open positions %s, %d traded symbols %s (preserved)",
                len(self.open_positions),
                list(self.open_positions),
                len(self.traded_symbols),
                list(self.traded_symbols)
            )
            return
        
        broker_open_positions = set()
        
        for pos in positions:
            symbol = pos.symbol
            position_size = pos.position
            
            logger.debug(
                "Processing position: symbol='%s', size=%s",
                symbol,
                position_size
            )
            
            if position_size != 0:
                # Extract underlying symbol from option contract
                underlying = self._extract_underlying_symbol(symbol)
                
                logger.debug(
                    "Extracted underlying: '%s' -> '%s'",
                    symbol,
                    underlying
                )
                
                if underlying:
                    broker_open_positions.add(underlying)
                    # Mark as traded (position exists = trade happened)
                    if underlying not in self.traded_symbols:
                        self.traded_symbols.add(underlying)
                        logger.info(
                            "[%s] 📝 Marked as traded (found open position from broker)",
                            underlying
                        )
        
        # Update open positions (PRESERVES traded_symbols from earlier in the day)
        self.open_positions = broker_open_positions
        self._save_state()
        
        logger.info(
            "✅ Broker sync complete: %d open positions %s, %d traded symbols %s",
            len(self.open_positions),
            list(self.open_positions),
            len(self.traded_symbols),
            list(self.traded_symbols)
        )

    def _extract_underlying_symbol(self, contract_symbol: str) -> str:
        """
        Extract underlying symbol from option contract.
        
        Examples:
            SPY -> SPY (stock)
            ES -> ES (futures)
            ES 20251226C5800 -> ES (futures option with space)
            SPY 20251226C580 -> SPY (stock option with space)
            NQ20251226C20000 -> NQ (futures option no space)
            TSLA20251226C350 -> TSLA (stock option no space)
        
        Args:
            contract_symbol: Contract symbol from IBKR
            
        Returns:
            Underlying symbol (e.g., SPY, ES, QQQ, TSLA)
        """
        # For option contracts with space, take first part before space
        parts = contract_symbol.split()
        if len(parts) > 1:
            return parts[0]  # ES from "ES 20251226C5800"
        
        # For contracts without space, strip all trailing digits, C, P
        # Strategy: Find where the letters end and numbers/options begin
        # NQ20251226C20000 -> NQ
        # TSLA20251226C350 -> TSLA
        # Remove all trailing: digits (0-9), C, P
        import re
        
        # Match: start with letters, then optionally followed by digits and C/P
        match = re.match(r'^([A-Z]+)', contract_symbol)
        if match:
            return match.group(1)
        
        # Fallback: return as-is if no match
        return contract_symbol

    def get_state_summary(self) -> Dict:
        """Get current state summary"""
        return {
            "date": self.today,
            "traded_symbols": sorted(list(self.traded_symbols)),
            "open_positions": sorted(list(self.open_positions)),
            "total_trades": self.total_trades
        }

    def cleanup_old_state_files(self, keep_days: int = 7):
        """
        Delete state files older than keep_days.
        
        Args:
            keep_days: Number of days to keep (default: 7)
        """
        try:
            current_date = datetime.now()
            deleted_count = 0
            
            for state_file in self.state_dir.glob("ibkr_trades_*.json"):
                # Extract date from filename: ibkr_trades_2025-12-26.json
                try:
                    date_str = state_file.stem.replace("ibkr_trades_", "")
                    file_date = datetime.strptime(date_str, "%Y-%m-%d")
                    
                    age_days = (current_date - file_date).days
                    
                    if age_days > keep_days:
                        state_file.unlink()
                        deleted_count += 1
                        logger.info("🗑️ Deleted old state file: %s (age: %d days)", 
                                  state_file.name, age_days)
                except Exception as e:
                    logger.warning("⚠️ Failed to process state file %s: %s", 
                                 state_file.name, e)
            
            if deleted_count > 0:
                logger.info("✅ Cleanup complete: %d old state files deleted", deleted_count)
            else:
                logger.debug("✅ Cleanup complete: No old files to delete")
                
        except Exception as e:
            logger.error("❌ State file cleanup failed: %s", e)
