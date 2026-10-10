"""Test doubles.

The emulated board itself lives in the package (``max11410_evkit.transport.fake``) so the
CLI's ``--fake`` flag and a GUI demo mode can use it from an installed wheel; it is
re-exported here together with test-only helpers.
"""

from max11410_evkit.transport.fake import PART_ID, FakeAdc, FakeEvKitTransport
from tests.fakes.fake_hid import FakeHidModule, SpyTransport

__all__ = ["PART_ID", "FakeAdc", "FakeEvKitTransport", "FakeHidModule", "SpyTransport"]
