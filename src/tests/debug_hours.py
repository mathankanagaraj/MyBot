from core.angelone.utils import is_market_open
from datetime import datetime
import pytz

print(f"UTC Now: {datetime.utcnow()}")
print(f"Is Market Open? {is_market_open()}")

ET = pytz.timezone("America/New_York")
now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
now_et = now_utc.astimezone(ET)
print(f"ET Now: {now_et}")
print(f"Weekday: {now_et.weekday()}")
print(f"Time: {now_et.time()}")
