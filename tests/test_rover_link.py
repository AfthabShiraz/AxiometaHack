"""RoverLink start/close behavior."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from hub.rover_link import RoverLink


class RoverLinkStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_does_not_send_stop(self):
        link = RoverLink(
            {
                "transport": "udp",
                "host": "127.0.0.1",
                "udp_port": 5007,
            }
        )
        writes: list[bytes] = []

        async def capture_write(data: bytes):
            writes.append(data)

        with patch.object(link, "_write", side_effect=capture_write):
            await link.start()

        self.assertEqual(writes, [], "start() must not send S (stop)")
        self.assertTrue(link.connected)
        await link.close()

    async def test_close_sends_stop(self):
        link = RoverLink(
            {
                "transport": "udp",
                "host": "127.0.0.1",
                "udp_port": 5007,
            }
        )
        writes: list[bytes] = []

        async def capture_write(data: bytes):
            writes.append(data)

        await link.start()
        with patch.object(link, "_write", side_effect=capture_write):
            await link.close()

        self.assertIn(b"S\n", writes)


if __name__ == "__main__":
    unittest.main()
