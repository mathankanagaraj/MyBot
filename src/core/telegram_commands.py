"""
Telegram Command Handler for Bot Control

Supports commands:
- pos: Get current positions and P&L summary
- stop: Stop the bot for the day
- start: Start/resume the bot
"""
import asyncio
import time
from typing import Optional, Callable, Dict, Any
import requests

from core.logger import logger


class TelegramCommandHandler:
    """Handle incoming Telegram commands for bot control"""
    
    def __init__(
        self,
        token: str,
        chat_id: str,
        broker: str,
        stop_callback: Optional[Callable] = None,
        start_callback: Optional[Callable] = None,
        positions_callback: Optional[Callable] = None,
    ):
        """
        Initialize Telegram command handler
        
        Args:
            token: Telegram bot token
            chat_id: Telegram chat ID to monitor
            broker: "ANGEL" or "IBKR" for logging
            stop_callback: Function to call when 'stop' command received
            start_callback: Function to call when 'start' command received
            positions_callback: Async function to call when 'pos' command received
        """
        self.token = token
        self.chat_id = chat_id
        self.broker = broker.upper()
        self.stop_callback = stop_callback
        self.start_callback = start_callback
        self.positions_callback = positions_callback
        
        self.last_update_id = 0
        self.running = False
        self.task = None
        
        logger.info(f"[{self.broker}] Telegram command handler initialized")
    
    def send_message(self, text: str):
        """Send a message to Telegram"""
        if not self.token or not self.chat_id:
            logger.warning(f"[{self.broker}] Telegram not configured")
            return
        
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            requests.post(
                url,
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                timeout=5
            )
        except Exception as e:
            logger.exception(f"[{self.broker}] Failed to send Telegram message: {e}")
    
    async def get_updates(self) -> list:
        """Get new messages from Telegram"""
        try:
            url = f"https://api.telegram.org/bot{self.token}/getUpdates"
            params = {
                "offset": self.last_update_id + 1,
                "timeout": 10,
                "allowed_updates": ["message"]
            }
            
            # Run in executor to avoid blocking
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: requests.get(url, params=params, timeout=15)
            )
            
            data = response.json()
            
            if data.get("ok"):
                return data.get("result", [])
            else:
                logger.error(f"[{self.broker}] Telegram API error: {data}")
                return []
        
        except Exception as e:
            logger.error(f"[{self.broker}] Failed to get Telegram updates: {e}")
            return []
    
    async def process_command(self, command: str, message_id: int):
        """Process a received command"""
        command = command.lower().strip()
        
        # Use print for immediate visibility (logger might not be working in async context)
        logger.info(f"[{self.broker}] 📥 Telegram command: '{command}'")
        print(f"[{self.broker}] 📊 Processing '{command}'...")
        
        if command == "pos":
            # Get positions and P&L
            if self.positions_callback:
                try:
                    await self.positions_callback()
                    print(f"[{self.broker}] ✅ Command completed")
                except Exception as e:
                    print(f"[{self.broker}] ❌ Error in callback: {e}")
                    logger.exception(f"[{self.broker}] Error getting positions: {e}")
                    self.send_message(f"❌ Error getting positions: {str(e)[:100]}")
            else:
                print(f"[{self.broker}] ⚠️ No positions callback configured")
                self.send_message(f"⚠️ Positions callback not configured for {self.broker} bot")
        
        elif command == "stop":
            # Stop the bot
            self.send_message(f"🛑 Stopping {self.broker} bot for the day...")
            if self.stop_callback:
                try:
                    self.stop_callback()
                    logger.info(f"[{self.broker}] Stop callback executed")
                except Exception as e:
                    logger.exception(f"[{self.broker}] Error executing stop callback: {e}")
                    self.send_message(f"❌ Error stopping bot: {str(e)[:100]}")
            else:
                self.send_message(f"⚠️ Stop callback not configured for {self.broker} bot")
        
        elif command == "start":
            # Start/resume the bot
            self.send_message(f"▶️ Starting {self.broker} bot...")
            if self.start_callback:
                try:
                    self.start_callback()
                    logger.info(f"[{self.broker}] Start callback executed")
                except Exception as e:
                    logger.exception(f"[{self.broker}] Error executing start callback: {e}")
                    self.send_message(f"❌ Error starting bot: {str(e)[:100]}")
            else:
                self.send_message(f"⚠️ Start callback not configured for {self.broker} bot")
        
        else:
            # Unknown command
            help_text = (
                f"<b>{self.broker} Bot Commands</b>\n\n"
                "📍 <b>pos</b> - Show current positions & P&L\n"
                "🛑 <b>stop</b> - Stop bot for the day\n"
                "▶️ <b>start</b> - Start/resume bot\n\n"
                f"Unknown command: '{command}'"
            )
            self.send_message(help_text)
    
    async def listen_loop(self):
        """Main loop to listen for commands"""
        logger.info(f"[{self.broker}] 🎧 Telegram command listener started")
        print(f"[{self.broker}] 🎧 Telegram listener active")
        
        # Initialize by clearing any old messages in the queue
        # This ensures we only respond to NEW commands sent after bot starts
        try:
            url = f"https://api.telegram.org/bot{self.token}/getUpdates"
            params = {"timeout": 0}  # Quick check
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: requests.get(url, params=params, timeout=5)
            )
            data = response.json()
            
            print(f"[{self.broker}] Init check: got {len(data.get('result', []))} pending updates")
            
            if data.get("ok") and data.get("result"):
                # Got pending updates - mark them all as read by getting with offset
                max_id = max(u.get("update_id", 0) for u in data["result"])
                # Call again with offset to confirm/delete them
                confirm_params = {"offset": max_id + 1, "timeout": 0}
                await loop.run_in_executor(
                    None,
                    lambda: requests.get(url, params=confirm_params, timeout=5)
                )
                self.last_update_id = max_id
                print(f"[{self.broker}] Cleared {len(data['result'])} old messages (last ID: {max_id}), now at offset {self.last_update_id}")
                logger.info(f"[{self.broker}] Cleared {len(data['result'])} old messages, ready for new commands")
            else:
                print(f"[{self.broker}] No pending updates, last_update_id stays at {self.last_update_id}")
        except Exception as e:
            print(f"[{self.broker}] Failed to initialize update offset: {e}")
        
        # Send startup notification (may fail silently if Telegram API has issues)
        try:
            self.send_message(
                f"🎧 <b>{self.broker} Bot Command Listener Active</b>\n\n"
                "Available commands:\n"
                "📍 <b>pos</b> - Show positions\n"
                "🛑 <b>stop</b> - Stop bot\n"
                "▶️ <b>start</b> - Start bot"
            )
            print(f"[{self.broker}] Sent startup notification to Telegram")
        except Exception as e:
            print(f"[{self.broker}] Failed to send startup notification: {e}")
            logger.error(f"[{self.broker}] Failed to send startup notification: {e}")
        
        error_count = 0
        max_errors = 10
        
        while self.running:
            try:
                updates = await self.get_updates()
                
                print(f"[{self.broker}] Poll (offset={self.last_update_id+1}): Got {len(updates)} update(s)")
                
                for update in updates:
                    # Update the last_update_id
                    update_id = update.get("update_id")
                    if update_id:
                        self.last_update_id = max(self.last_update_id, update_id)
                        print(f"[{self.broker}] Update {update_id}: Updating last_update_id to {self.last_update_id}")
                    
                    # Check if this is a message from the right chat
                    message = update.get("message")
                    if not message:
                        print(f"[{self.broker}] Update {update_id}: No message field")
                        continue
                    
                    chat = message.get("chat", {})
                    chat_id_from_msg = str(chat.get("id"))
                    if chat_id_from_msg != str(self.chat_id):
                        print(f"[{self.broker}] Update {update_id}: Wrong chat ID ({chat_id_from_msg} != {self.chat_id})")
                        continue
                    
                    # Extract command text
                    text = message.get("text", "").strip()
                    if not text:
                        print(f"[{self.broker}] Update {update_id}: Empty text")
                        continue
                    
                    message_id = message.get("message_id")
                    
                    print(f"[{self.broker}] Update {update_id}: Processing text='{text}'")
                    
                    # Process the command
                    await self.process_command(text, message_id)
                
                # Reset error count on success
                error_count = 0
                
                # Small delay before next poll
                await asyncio.sleep(2)
            
            except asyncio.CancelledError:
                logger.info(f"[{self.broker}] Command listener cancelled")
                break
            
            except Exception as e:
                error_count += 1
                logger.exception(f"[{self.broker}] Error in command listener (#{error_count}): {e}")
                
                if error_count >= max_errors:
                    logger.error(f"[{self.broker}] Too many errors, stopping command listener")
                    self.send_message(f"🚨 Command listener stopped after {max_errors} errors")
                    break
                
                # Exponential backoff
                await asyncio.sleep(min(30, 2 ** error_count))
        
        logger.info(f"[{self.broker}] Telegram command listener stopped")
    
    def start(self):
        """Start the command listener in background"""
        if self.running:
            logger.warning(f"[{self.broker}] Command listener already running")
            return
        
        self.running = True
        self.task = asyncio.create_task(self.listen_loop())
        logger.info(f"[{self.broker}] Command listener started")
    
    async def stop(self):
        """Stop the command listener"""
        if not self.running:
            return
        
        self.running = False
        
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        
        logger.info(f"[{self.broker}] Command listener stopped")
