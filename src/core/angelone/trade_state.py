# core/angelone/trade_state.py
"""
Trade State Manager for Angel One
Handles persistence of daily trade state across bot restarts.
"""

import json
from datetime import datetime, date
from pathlib import Path
from typing import Set
import pytz

from core.logger import logger
from core.config import TRADE_STATE_DIR


class TradeStateManager:
    """
    Manages daily trade state with file-based persistence.
    Ensures bot remembers trades even after Docker restarts.
    """
    
    def __init__(self):
        self.state_dir = TRADE_STATE_DIR
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self._get_today_state_file()
        self.traded_symbols: Set[str] = set()
        self.open_positions: Set[str] = set()
        self._load_state()
    
    def _get_today_state_file(self) -> Path:
        """Get state file path for today's date"""
        ist = pytz.timezone("Asia/Kolkata")
        today = datetime.now(ist).date()
        return self.state_dir / f"angel_trades_{today.isoformat()}.json"
    
    def _load_state(self):
        """Load state from file if exists"""
        if not self.state_file.exists():
            logger.info("📄 No existing state file for today, starting fresh")
            self._save_state()
            return
        
        try:
            with open(self.state_file, 'r') as f:
                data = json.load(f)
                self.traded_symbols = set(data.get('traded_symbols', []))
                self.open_positions = set(data.get('open_positions', []))
            
            logger.info(
                "📂 Loaded state: %d traded symbols, %d open positions",
                len(self.traded_symbols),
                len(self.open_positions)
            )
        except Exception as e:
            logger.error("❌ Failed to load state file: %s", e)
            # Start fresh if load fails
            self.traded_symbols = set()
            self.open_positions = set()
    
    def _save_state(self):
        """Save current state to file"""
        try:
            ist = pytz.timezone("Asia/Kolkata")
            today = datetime.now(ist).date()
            data = {
                'date': today.isoformat(),
                'traded_symbols': sorted(list(self.traded_symbols)),
                'open_positions': sorted(list(self.open_positions)),
                'last_updated': datetime.now(ist).isoformat()
            }
            with open(self.state_file, 'w') as f:
                json.dump(data, f, indent=2)
            
            logger.debug("💾 State saved: %s", self.state_file.name)
        except Exception as e:
            logger.error("❌ Failed to save state: %s", e)
    
    def mark_symbol_traded(self, symbol: str):
        """Mark symbol as traded today"""
        self.traded_symbols.add(symbol)
        self._save_state()
        logger.info("[%s] ✅ Marked as traded", symbol)
    
    def is_symbol_traded_today(self, symbol: str) -> bool:
        """Check if symbol was traded today"""
        return symbol in self.traded_symbols
    
    def mark_position_opened(self, symbol: str):
        """Mark symbol as having an open position"""
        self.open_positions.add(symbol)
        self._save_state()
        logger.debug("[%s] 📈 Position opened", symbol)
    
    def mark_position_closed(self, symbol: str):
        """Mark symbol position as closed"""
        if symbol in self.open_positions:
            self.open_positions.remove(symbol)
            self._save_state()
            logger.debug("[%s] 📉 Position closed", symbol)
    
    def has_open_position(self, symbol: str) -> bool:
        """Check if symbol has an open position"""
        return symbol in self.open_positions
    
    def get_state_summary(self) -> dict:
        """
        Get summary of current trade state.
        
        Returns:
            Dict with traded_symbols, open_positions lists
        """
        return {
            "traded_symbols": sorted(list(self.traded_symbols)),
            "open_positions": sorted(list(self.open_positions))
        }
    
    def sync_with_broker(self, positions: list):
        """
        Sync state with broker API positions.
        Updates open_positions based on actual broker data (preserves traded_symbols).
        """
        logger.info("🔄 Syncing trade state with Angel One broker...")
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
        
        broker_symbols = set()
        for pos in positions:
            netqty = int(pos.get("netqty", "0"))
            tradingsymbol = pos.get("tradingsymbol", "")
            
            logger.debug(
                "Processing position: tradingsymbol='%s', netqty=%s",
                tradingsymbol,
                netqty
            )
            
            if netqty != 0:
                # Extract underlying symbol from option contract
                # Example: BANKNIFTY30DEC2559300PE -> BANKNIFTY
                matched_symbol = None
                for tracked in self.traded_symbols | self.open_positions:
                    if tracked in tradingsymbol:
                        matched_symbol = tracked
                        broker_symbols.add(tracked)
                        break
                
                logger.debug(
                    "Symbol extraction: '%s' -> '%s'",
                    tradingsymbol,
                    matched_symbol if matched_symbol else "NO MATCH"
                )
        
        # Update open positions to match broker
        removed = self.open_positions - broker_symbols
        added = broker_symbols - self.open_positions
        
        if removed:
            logger.info("📂 Syncing: Positions closed on broker: %s", list(removed))
            self.open_positions -= removed
        
        if added:
            logger.info("📂 Syncing: Positions opened on broker: %s", list(added))
            self.open_positions |= added
        
        if removed or added:
            self._save_state()
        
        logger.info(
            "✅ Broker sync complete: %d open positions %s, %d traded symbols %s",
            len(self.open_positions),
            list(self.open_positions),
            len(self.traded_symbols),
            list(self.traded_symbols)
        )
    
    def sync_with_order_history(self, orders: list, tracked_symbols: list):
        """
        Sync traded_symbols with order history from broker.
        Marks symbols as traded if orders were placed today.
        """
        ist = pytz.timezone("Asia/Kolkata")
        today = datetime.now(ist).date()
        
        for order in orders:
            order_time_str = order.get("updatetime", "") or order.get("ordertime", "")
            if not order_time_str:
                continue
            
            try:
                # Parse order time (format: "DD-MMM-YYYY HH:MM:SS" or similar)
                order_dt = datetime.strptime(order_time_str, "%d-%b-%Y %H:%M:%S")
                order_date = order_dt.date()
                
                if order_date == today:
                    # Check if this order is for one of our tracked symbols
                    symbol = order.get("tradingsymbol", "")
                    for tracked in tracked_symbols:
                        if tracked in symbol and tracked not in self.traded_symbols:
                            self.traded_symbols.add(tracked)
                            logger.info(
                                "[%s] 📂 Found existing order from today - marked as traded",
                                tracked
                            )
            except Exception as e:
                logger.debug(f"Error parsing order time: {e}")
                continue
        
        self._save_state()
    
    def cleanup_old_state_files(self, keep_days: int = 7):
        """Remove state files older than keep_days"""
        try:
            ist = pytz.timezone("Asia/Kolkata")
            today = datetime.now(ist).date()
            
            for file in self.state_dir.glob("angel_trades_*.json"):
                try:
                    # Extract date from filename: angel_trades_2025-12-26.json
                    date_str = file.stem.replace("angel_trades_", "")
                    file_date = date.fromisoformat(date_str)
                    
                    age_days = (today - file_date).days
                    if age_days > keep_days:
                        file.unlink()
                        logger.info(f"🗑️ Removed old state file: {file.name} ({age_days} days old)")
                except Exception as e:
                    logger.debug(f"Error processing state file {file}: {e}")
        except Exception as e:
            logger.error(f"Error cleaning up old state files: {e}")
