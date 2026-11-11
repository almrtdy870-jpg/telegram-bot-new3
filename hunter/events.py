"""In-memory event and error logs shown on the dashboard."""
import collections
import threading
import time


class Log:
    def __init__(self, size):
        self.items = collections.deque(maxlen=size)
        self.lock = threading.Lock()
        self.count = 0

    def add(self, text, level="info"):
        with self.lock:
            self.items.append((time.time(), level, text))
            self.count += 1

    def last(self, n):
        with self.lock:
            return list(self.items)[-n:]


events = Log(40)
errors = Log(60)


def event(text, level="info"):
    events.add(text, level)


def error(text):
    errors.add(text, "error")
