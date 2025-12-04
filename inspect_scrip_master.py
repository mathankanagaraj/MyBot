import requests
import json
import sys
import os

# Add src to path
sys.path.append(os.path.join(os.getcwd(), "src"))

from core.config import SCRIP_MASTER_URL

def inspect_scrip_master():
    print(f"Downloading Scrip Master from {SCRIP_MASTER_URL}...")
    response = requests.get(SCRIP_MASTER_URL)
    data = response.json()
    print(f"Loaded {len(data)} instruments.")

    target_symbol = "TCS"
    target_expiry = "30DEC2025"
    
    print(f"\nSearching for {target_symbol} options with expiry {target_expiry}...")
    
    found_count = 0
    for instrument in data:
        if instrument.get("name") == target_symbol and instrument.get("instrumenttype") == "OPTSTK":
            # Check expiry if possible, or just print a few
            expiry = instrument.get("expiry")
            if expiry == target_expiry:
                print("\nFound Instrument:")
                print(json.dumps(instrument, indent=2))
                found_count += 1
                if found_count >= 5:
                    break
    
    print("\nSearching for specific symbols...")
    specific_symbols = ["TCS30DEC253200CE", "TCS30DEC253120CE"]
    
    for sym in specific_symbols:
        found = False
        for instrument in data:
            if instrument.get("symbol") == sym:
                print(f"\nFOUND {sym}:")
                print(json.dumps(instrument, indent=2))
                found = True
                break
        if not found:
            print(f"\nNOT FOUND {sym}")

if __name__ == "__main__":
    inspect_scrip_master()
