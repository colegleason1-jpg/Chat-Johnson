class TaskAwareRouter:
    def __init__(self, ledger):
        self.ledger = ledger

    def select_model_for_task(self, task_type):
        preferences = {
            "massive_context": ["Gemini", "NVIDIA_NIM"],
            "speed_coding": ["Groq", "NVIDIA_NIM"],
            "deep_reasoning": ["NVIDIA_NIM", "Gemini"]
        }
        candidates = preferences.get(task_type, ["Groq", "Gemini"])
        for model in candidates:
            if self.ledger.is_available(model):
                return model
        return None
