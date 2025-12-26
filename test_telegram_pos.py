#!/usr/bin/env python3
"""
Test script to verify Telegram 'pos' command fetches fresh data.
Sends 3 'pos' commands with 2-minute intervals and monitors logs.
"""
import requests
import time
import os
import subprocess
from datetime import datetime

def load_env():
    """Load environment variables from .env file"""
    env_vars = {}
    with open('.env') as f:
        for line in f:
            if '=' in line and not line.startswith('#'):
                key, value = line.strip().split('=', 1)
                env_vars[key] = value
    return env_vars

def send_pos_command(token, chat_id, test_num):
    """Send 'pos' command to Telegram bot"""
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    response = requests.post(
        url, 
        json={'chat_id': chat_id, 'text': 'pos'}, 
        timeout=5
    )
    return response.json()['ok']

def get_recent_logs(seconds=15):
    """Get recent logs from Docker container"""
    cmd = f"docker-compose logs ibkr_bot --since {seconds}s 2>&1"
    result = subprocess.run(
        cmd, 
        shell=True, 
        capture_output=True, 
        text=True,
        cwd="/Users/mathan/Documents/GitHub/MyBot"
    )
    return result.stdout

def extract_position_data(logs):
    """Extract position P&L data from logs"""
    positions = {}
    for line in logs.split('\n'):
        if 'Position P&L calc:' in line or 'MktPrice' in line:
            # Extract symbol and values
            if 'META' in line or 'TSLA' in line or 'ES' in line:
                parts = line.split('|')
                if len(parts) >= 3:
                    symbol = None
                    mkt_price = None
                    pnl = None
                    
                    for part in parts:
                        if 'META' in part or 'TSLA' in part or 'ES' in part:
                            for sym in ['META', 'TSLA', 'ES']:
                                if sym in part:
                                    symbol = sym
                                    break
                        if 'MktPrice' in part:
                            try:
                                mkt_price = part.split('$')[1].split('/')[0]
                            except:
                                pass
                        if 'P&L' in part and symbol:
                            try:
                                pnl = part.split('$')[1].strip()
                            except:
                                pass
                    
                    if symbol and (mkt_price or pnl):
                        positions[symbol] = {
                            'market_price': mkt_price,
                            'pnl': pnl
                        }
    return positions

def main():
    print("="*60)
    print("Telegram 'pos' Command - Fresh Data Test")
    print("="*60)
    print("\nThis script will:")
    print("  1. Send 'pos' command to IBKR bot")
    print("  2. Wait 2 minutes")
    print("  3. Send 'pos' command again")
    print("  4. Wait 2 minutes")
    print("  5. Send 'pos' command a third time")
    print("\nWatching logs to verify fresh data is fetched each time...")
    print("="*60)
    
    env = load_env()
    token = env.get('IBKR_TELEGRAM_TOKEN')
    chat_id = env.get('IBKR_TELEGRAM_CHAT_ID')
    
    if not token or not chat_id:
        print("ERROR: Missing IBKR_TELEGRAM_TOKEN or IBKR_TELEGRAM_CHAT_ID in .env")
        return
    
    results = []
    
    for test_num in range(1, 4):
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"\n[Test {test_num}/3] [{timestamp}] Sending 'pos' command...")
        
        # Send command
        success = send_pos_command(token, chat_id, test_num)
        if not success:
            print(f"  ❌ Failed to send command")
            continue
        
        print(f"  ✅ Command sent successfully")
        
        # Wait for processing
        print(f"  ⏳ Waiting 8 seconds for bot to process...")
        time.sleep(8)
        
        # Get logs
        print(f"  📋 Checking logs for fresh data fetch...")
        logs = get_recent_logs(15)
        
        # Check for fresh data indicators
        has_fresh_request = "Requesting FRESH positions" in logs or "Fetching FRESH portfolio" in logs
        has_position_data = "Position P&L calc:" in logs
        
        # Extract position values
        positions = extract_position_data(logs)
        
        result = {
            'test': test_num,
            'timestamp': timestamp,
            'fresh_request': has_fresh_request,
            'has_data': has_position_data,
            'positions': positions
        }
        results.append(result)
        
        # Display results
        print(f"\n  📊 Results:")
        print(f"     Fresh data request logged: {'✅ YES' if has_fresh_request else '❌ NO'}")
        print(f"     Position data found: {'✅ YES' if has_position_data else '❌ NO'}")
        
        if positions:
            print(f"     Positions detected:")
            for symbol, data in positions.items():
                mkt = data.get('market_price', 'N/A')
                pnl = data.get('pnl', 'N/A')
                print(f"       {symbol}: MktPrice=${mkt}, P&L=${pnl}")
        
        # Wait before next test (except after last test)
        if test_num < 3:
            print(f"\n  ⏰ Waiting 2 minutes before next test...")
            time.sleep(120)
    
    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)
    
    for i, result in enumerate(results, 1):
        print(f"\nTest {i} [{result['timestamp']}]:")
        print(f"  Fresh data requested: {'✅' if result['fresh_request'] else '❌'}")
        print(f"  Position data found: {'✅' if result['has_data'] else '❌'}")
        if result['positions']:
            for symbol, data in result['positions'].items():
                print(f"    {symbol}: {data}")
    
    # Check if values changed between tests
    if len(results) >= 2:
        print("\n" + "-"*60)
        print("FRESHNESS VERIFICATION:")
        print("-"*60)
        
        # Compare positions between tests
        for symbol in ['META', 'TSLA', 'ES']:
            test1_data = results[0]['positions'].get(symbol, {})
            test2_data = results[1]['positions'].get(symbol, {})
            
            if test1_data and test2_data:
                test1_price = test1_data.get('market_price')
                test2_price = test2_data.get('market_price')
                
                if test1_price and test2_price:
                    if test1_price != test2_price:
                        print(f"  ✅ {symbol} price CHANGED: ${test1_price} → ${test2_price}")
                    else:
                        print(f"  ⚠️  {symbol} price SAME: ${test1_price}")
        
        print("\n💡 Note: Prices may stay the same if market hasn't moved.")
        print("   The key indicator is 'Fresh data request logged' = YES")
    
    print("\n" + "="*60)
    print("Test complete! Check your Telegram app for the 3 responses.")
    print("="*60)

if __name__ == "__main__":
    main()
