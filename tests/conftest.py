import pytest

from max11410_evkit.board.evkit import EvKitBoard
from tests.fakes import FakeEvKitTransport


@pytest.fixture
def fake():
    t = FakeEvKitTransport(drdy_timeout_s=0.05)
    t.open()
    yield t
    t.close()


@pytest.fixture
def board(fake):
    b = EvKitBoard(fake, timeout=0.5, drdy_timeout=1.0)
    yield b
    b.close()
