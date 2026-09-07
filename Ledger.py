import time

class FreeTierLedger:
    def __init__(self):
        self.providers = {
            "Gemini": {"rpm_limit": 10, "requests_this_minute": 0, "window_start": time.time(), "active": True},
            "Groq": {"rpm_limit": 30, "requests_this_minute": 0, "window_start": time.time(), "active": True},
            "NVIDIA_NIM": {"rpm_limit": 40, "requests_this_minute": 0, "window_start": time.time(), "active": True}
        }

    def _reset_windows(self):
        now = time.time()
        for name, data in self.providers.items():
            if now - data["window_start"] >= 60:
                data["requests_this_minute"] = 0
                data["window_start"] = now
                data["active"] = True

    def is_available(self, provider_name):
        self._reset_windows()
        p = self.providers.get(provider_name)
        return p and p["active"] and p["requests_this_minute"] < p["rpm_limit"]

    def log_request(self, provider_name):
        if provider_name in self.providers:
            self.providers[provider_name]["requests_this_minute"] += 1
