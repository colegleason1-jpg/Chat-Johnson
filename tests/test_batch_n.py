"""Batch N: a chat send never freezes behind a background job's provider lock; jobs step aside for a waiting chat."""
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import quota_registry as registry  # noqa: E402


def test_chat_acquire_is_bounded_and_flags_waiting_only_while_it_waits():
    registry.reset_for_tests()
    lock = threading.Lock()
    lock.acquire()  # a background job holds the provider
    seen = []
    watcher = threading.Thread(target=lambda: seen.append(registry.chat_waiting()) or time.sleep(0.05) or seen.append(registry.chat_waiting()))
    started = time.perf_counter()
    watcher.start()
    time.sleep(0.02)
    assert registry.acquire_for_chat(lock, timeout=0.3) is False
    elapsed = time.perf_counter() - started
    watcher.join()
    assert 0.25 <= elapsed < 2.0 and not registry.chat_waiting()  # the flag is cleared once the wait ends
    lock.release()
    assert registry.acquire_for_chat(lock, timeout=0.3) is True and not registry.chat_waiting()
    lock.release()


def test_jobs_yield_to_a_waiting_chat_and_the_chat_gets_the_lock_first():
    registry.reset_for_tests()
    lock = threading.Lock()
    order = []

    def job(name):
        with registry.job_lock(lock):
            order.append(name)
            time.sleep(0.15)

    # The chat starts waiting while job A holds the lock; job B, arriving later, yields to the chat.
    first = threading.Thread(target=job, args=("job-a",))
    first.start()
    time.sleep(0.03)

    def chat():
        assert registry.acquire_for_chat(lock, timeout=5.0)
        order.append("chat")
        time.sleep(0.05)
        lock.release()

    chatter = threading.Thread(target=chat)
    chatter.start()
    time.sleep(0.03)
    second = threading.Thread(target=job, args=("job-b",))
    second.start()
    for thread in (first, chatter, second):
        thread.join(timeout=5.0)
    assert order[0] == "job-a" and order.index("chat") < order.index("job-b")


def test_yield_to_chat_returns_when_nothing_waits_and_is_bounded_when_something_does():
    registry.reset_for_tests()
    assert registry.yield_to_chat(max_wait=1.0) == 0.0
    registry._CHAT_WAITING.set()
    try:
        waited = registry.yield_to_chat(max_wait=0.4, step=0.1)
        assert 0.3 <= waited <= 0.6  # bounded: a job never starves behind a chat that never sends
    finally:
        registry._CHAT_WAITING.clear()
