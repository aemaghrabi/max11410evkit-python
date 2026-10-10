import threading

from max11410_evkit.board.evkit import EvKitBoard
from tests.fakes import FakeEvKitTransport


def test_two_threads_never_see_each_others_replies():
    t = FakeEvKitTransport(command_latency_s=0.0005)
    b = EvKitBoard(t, timeout=1.0)
    b.open()
    errors = []

    def worker(tag: int):
        try:
            for i in range(150):
                data = bytes([tag, i & 0xFF]) * 4
                got = b.echo(data)
                if got != data:
                    errors.append((tag, i, got))
        except Exception as exc:  # pragma: no cover - surfaced by the assert
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(k,)) for k in (0xA0, 0xB0)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    b.close()
    assert errors == []
    assert len([p for p in t.received if p[0] == 0x01]) == 300
