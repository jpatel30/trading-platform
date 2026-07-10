import sys
sys.path.insert(0, '/Users/jaimin/Documents/Claude/Projects/trading-platform')

from app.learning.nightly_loop import run_nightly_loop
from app.utils.current_user import get_current_user_id
from app.broker.factory import get_broker

user_id = get_current_user_id()
try:
    positions = get_broker(user_id).get_positions() or []
except Exception:
    positions = []

result = run_nightly_loop(user_id, positions)
print(f"[Nightly] Learning complete: ran={result.get('ran')}")
