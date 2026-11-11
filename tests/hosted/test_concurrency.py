"""Exercise PostgreSQL row locks or serialized SQLite development transactions."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from fastapi import HTTPException
from fastapi.testclient import TestClient

from asklit.hosted.models import Job
from asklit.hosted.security import reserve_calls
from asklit.hosted.worker import claim
from test_api import new_bot, publish


def test_quota_reservations_are_atomic(hosted):
    settings, store = hosted[:2]
    settings.daily_calls = 3
    barrier = Barrier(8)

    def reserve(_):
        barrier.wait()
        try:
            with store.transaction() as db:
                reserve_calls(db, "school", settings)
            return True
        except HTTPException as exc:
            assert exc.status_code == 429
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(8))) == 3


def test_workers_claim_a_job_only_once(hosted):
    bot = new_bot(hosted)
    store = hosted[1]
    with store.transaction() as db:
        db.add(Job(bot_id=bot["id"], kind="evaluate", payload={}))
    barrier = Barrier(6)

    def work(_):
        barrier.wait()
        job = claim(store)
        return job.id if job else None

    with ThreadPoolExecutor(max_workers=6) as pool:
        claimed = [id for id in pool.map(work, range(6)) if id]
    assert len(claimed) == 1


def test_concurrent_duplicate_chat_only_calls_model_once(hosted):
    bot = new_bot(hosted)
    publish(hosted, bot)
    conversation = (
        hosted[3].post(f"/api/public/bots/{bot['id']}/conversations", json={}).json()
    )
    barrier = Barrier(5)

    def send(_):
        with TestClient(hosted[3].app) as client:
            barrier.wait()
            return client.post(
                f"/api/public/conversations/{conversation['id']}/messages",
                json={"message": "Help me", "request_id": "duplicate"},
                headers={"Authorization": "Bearer " + conversation["token"]},
            ).status_code

    with ThreadPoolExecutor(max_workers=5) as pool:
        statuses = list(pool.map(send, range(5)))
    assert set(statuses) <= {200, 409} and 200 in statuses
    assert len(hosted[5]) == 1
