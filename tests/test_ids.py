from __future__ import annotations

import unittest
import uuid

try:
    from floati.ids import uuid7_hex
except ModuleNotFoundError:
    uuid7_hex = None


class Uuid7Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(uuid7_hex, "protocol identifiers must use UUIDv7")

    def test_uuid_has_version_seven_and_rfc_variant(self) -> None:
        values = [uuid.UUID(hex=uuid7_hex()) for _ in range(100)]
        self.assertTrue(all(value.version == 7 for value in values))
        self.assertTrue(all(value.variant == uuid.RFC_4122 for value in values))
        self.assertEqual(100, len({value.hex for value in values}))

    def test_injected_millisecond_timestamp_is_encoded(self) -> None:
        timestamp_ms = 1_785_500_000_123
        value = uuid.UUID(hex=uuid7_hex(timestamp_ms=timestamp_ms, random_bits=0))
        self.assertEqual(timestamp_ms, value.int >> 80)
        self.assertEqual(7, value.version)


if __name__ == "__main__":
    unittest.main()
