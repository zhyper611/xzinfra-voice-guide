from showroom_guide.knowledge_outbox import KnowledgeOutbox, OutboxState


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_pending_entry_survives_reopen(tmp_path):
    path = tmp_path / "knowledge.sqlite3"
    clock = Clock()
    first = KnowledgeOutbox(path, clock=clock)

    entry = first.enqueue("总装车间采用柔性生产线。")
    first.close()

    reopened = KnowledgeOutbox(path, clock=clock)
    stored = reopened.list_due()

    assert len(stored) == 1
    assert stored[0].id == entry.id
    assert stored[0].content == "总装车间采用柔性生产线。"
    assert stored[0].filename.endswith(".md")
    assert stored[0].state is OutboxState.PENDING


def test_uploaded_entry_is_retained_until_remote_processing_succeeds(tmp_path):
    clock = Clock()
    outbox = KnowledgeOutbox(tmp_path / "knowledge.sqlite3", clock=clock)
    entry = outbox.enqueue("知识正文")

    outbox.mark_uploaded(entry.id, retry_after_seconds=30)
    assert outbox.count() == 1
    assert outbox.list_due() == []

    clock.now += 30
    uploaded = outbox.list_due()[0]
    assert uploaded.state is OutboxState.UPLOADED

    outbox.delete(entry.id)
    assert outbox.count() == 0


def test_failure_increments_attempts_and_applies_backoff(tmp_path):
    clock = Clock()
    outbox = KnowledgeOutbox(tmp_path / "knowledge.sqlite3", clock=clock)
    entry = outbox.enqueue("知识正文")

    outbox.mark_failed(entry.id, "network unavailable", retry_after_seconds=60)

    assert outbox.list_due() == []
    clock.now += 60
    failed = outbox.list_due()[0]
    assert failed.attempts == 1
    assert failed.last_error == "network unavailable"
