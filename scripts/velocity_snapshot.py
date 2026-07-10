import sys
sys.path.insert(0, '/Users/jaimin/Documents/Claude/Projects/trading-platform')

from app.signals.velocity_tracker import save_daily_signals
from app.utils.current_user import get_current_user_id

user_id = get_current_user_id()
result  = save_daily_signals(user_id)
print(f"[Velocity] Snapshot complete: {result}")
