"""Packaged desktop lifetime: the last browser page owns the local service."""
import threading
import time


class BrowserLifetime:
    def __init__(self, jobs, shutdown, *, clock=time.monotonic, grace=5, timeout=180):
        self.jobs, self.shutdown, self.clock = jobs, shutdown, clock
        self.grace, self.timeout = grace, timeout
        self.clients, self.seen, self.empty_since = {}, False, None
        self.stopping, self.done = False, False
        self.lock = threading.RLock()

    def update(self, identifier, action):
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 80 or action not in ('open', 'close'):
            raise ValueError('浏览器会话无效')
        with self.lock:
            if self.done:
                raise ValueError('服务正在退出，请重新打开平台')
            self.seen = True
            if action == 'open':
                self.clients[identifier] = self.clock()
                self.empty_since = None
                self.stopping = False
            else:
                self.clients.pop(identifier, None)
                if not self.clients and self.empty_since is None:
                    self.empty_since = self.clock()

    def tick(self):
        with self.lock:
            stamp = self.clock()
            self.clients = {key: last for key, last in self.clients.items() if stamp - last < self.timeout}
            if self.clients or not self.seen or self.done:
                return False
            if self.empty_since is None:
                self.empty_since = stamp
            if stamp - self.empty_since < self.grace:
                return False
            if not self.stopping:
                self.stopping = True
                for task in self.jobs.active():
                    self.jobs.stop(task['id'])
            if self.jobs.active():
                return False
            self.jobs.close()
            self.done = True
        self.shutdown()
        return True

    def watch(self):
        while not self.done:
            time.sleep(.5)
            self.tick()
