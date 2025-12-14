#!/usr/bin/env python3
"""
Test script to verify Telegram configuration for both brokers.
Sends test messages to both Angel One and IBKR Telegram channels.
"""
import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from core.utils import send_telegram
from core.config import (
    TELEGRAM_TOKEN, 
    TELEGRAM_CHAT_ID,
    IBKR_TELEGRAM_TOKEN,
    IBKR_TELEGRAM_CHAT_ID
)

def test_telegram_config():
    """Test Telegram configuration for both brokers."""
    print("=" * 60)
    print("Telegram Configuration Test")
    print("=" * 60)
    
    # Check Angel One config
    print("\n1. Angel One Telegram Bot:")
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        print(f"   ✅ Token configured: {TELEGRAM_TOKEN[:10]}...")
        print(f"   ✅ Chat ID configured: {TELEGRAM_CHAT_ID}")
        
        # Send test message
        try:
            send_telegram(
                "🧪 Test message from Angel One Bot\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "This is a test of the separate Telegram configuration.\n"
                "If you receive this, Angel One bot is configured correctly!",
                broker="ANGEL"
            )
            print("   ✅ Test message sent successfully")
        except Exception as e:
            print(f"   ❌ Failed to send test message: {e}")
    else:
        print("   ❌ Token or Chat ID not configured")
        print(f"      Token: {'SET' if TELEGRAM_TOKEN else 'NOT SET'}")
        print(f"      Chat ID: {'SET' if TELEGRAM_CHAT_ID else 'NOT SET'}")
    
    # Check IBKR config
    print("\n2. IBKR Telegram Bot:")
    if IBKR_TELEGRAM_TOKEN and IBKR_TELEGRAM_CHAT_ID:
        print(f"   ✅ Token configured: {IBKR_TELEGRAM_TOKEN[:10]}...")
        print(f"   ✅ Chat ID configured: {IBKR_TELEGRAM_CHAT_ID}")
        
        # Send test message
        try:
            send_telegram(
                "🧪 Test message from IBKR Bot\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "This is a test of the separate Telegram configuration.\n"
                "If you receive this, IBKR bot is configured correctly!",
                broker="IBKR"
            )
            print("   ✅ Test message sent successfully")
        except Exception as e:
            print(f"   ❌ Failed to send test message: {e}")
    else:
        print("   ❌ Token or Chat ID not configured")
        print(f"      Token: {'SET' if IBKR_TELEGRAM_TOKEN else 'NOT SET'}")
        print(f"      Chat ID: {'SET' if IBKR_TELEGRAM_CHAT_ID else 'NOT SET'}")
    
    # Summary
    print("\n" + "=" * 60)
    print("Summary:")
    angel_ok = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)
    ibkr_ok = bool(IBKR_TELEGRAM_TOKEN and IBKR_TELEGRAM_CHAT_ID)
    
    if angel_ok and ibkr_ok:
        print("✅ Both Telegram bots are configured correctly!")
        print("\nCheck your Telegram to verify you received 2 test messages:")
        print("   1. Message from Angel One Bot")
        print("   2. Message from IBKR Bot")
    elif angel_ok:
        print("⚠️  Only Angel One bot is configured")
        print("   Configure IBKR_TELEGRAM_TOKEN and IBKR_TELEGRAM_CHAT_ID in .env")
    elif ibkr_ok:
        print("⚠️  Only IBKR bot is configured")
        print("   Configure TELEGRAM_TOKEN and TELEGRAM_CHAT_ID in .env")
    else:
        print("❌ Neither bot is configured")
        print("   Configure all Telegram variables in .env file")
    
    print("=" * 60)

if __name__ == "__main__":
    test_telegram_config()
