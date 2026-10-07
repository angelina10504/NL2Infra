import time
from contextlib import contextmanager

@contextmanager
def track_time(metrics: dict, key: str):
    start = time.time()
    try:
        yield
    finally:
        elapsed = time.time() - start
        metrics[key] = metrics.get(key, 0.0) + elapsed
        metrics["total_time"] = metrics.get("total_time", 0.0) + elapsed
