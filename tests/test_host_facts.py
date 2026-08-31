from __future__ import annotations

import unittest
from pathlib import Path

from floati import __version__


class _Platform:
    @staticmethod
    def system() -> str:
        return "TestOS"

    @staticmethod
    def release() -> str:
        return "1.2.3"

    @staticmethod
    def machine() -> str:
        return "test-machine"

    @staticmethod
    def python_implementation() -> str:
        return "TestPython"

    @staticmethod
    def python_version() -> str:
        return "9.8.7"


class _PartlyUnavailablePlatform(_Platform):
    @staticmethod
    def machine() -> str:
        raise OSError("fixture unavailable")


class HostFactsTests(unittest.TestCase):
    def test_host_facts_are_deterministic_json_safe_testimony(self) -> None:
        from floati.host_facts import collect_host_facts

        self.assertEqual(
            {
                "schema_version": 0,
                "status": "ok",
                "facts": {
                    "disk_free_bytes": 123456,
                    "floati_version": __version__,
                    "install_path": "/opt/floati",
                    "machine": "test-machine",
                    "operating_system": "TestOS",
                    "operating_system_release": "1.2.3",
                    "python_implementation": "TestPython",
                    "python_version": "9.8.7",
                },
            },
            collect_host_facts(
                install_path=Path("/opt/floati"),
                platform_source=_Platform,
                disk_usage=lambda _path: (999999, 1, 123456),
            ),
        )

    def test_unavailable_fact_is_typed_and_does_not_hide_other_facts(self) -> None:
        from floati.host_facts import collect_host_facts

        artifact = collect_host_facts(
            install_path=Path("/opt/floati"),
            platform_source=_PartlyUnavailablePlatform,
            disk_usage=lambda _path: (999999, 1, 123456),
        )

        self.assertEqual("degraded", artifact["status"])
        self.assertEqual(
            {"status": "unavailable", "reason_code": "host_fact_unavailable"},
            artifact["facts"]["machine"],
        )
        self.assertEqual("TestOS", artifact["facts"]["operating_system"])


if __name__ == "__main__":
    unittest.main()
