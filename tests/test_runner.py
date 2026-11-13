"""Regresi untuk keputusan runner yang mudah salah.

Jalankan dari root repo:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from runner import (
    OFFSET,
    WRITE_ALARM,
    ConfigError,
    FunctionalFailure,
    Incomplete,
    Lab,
    Sample,
    Step,
    Timing,
    load_suite,
    verify_manifest,
)

ROOT = Path(__file__).resolve().parent.parent


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.suite = load_suite(ROOT / "suites" / "tank-alarm-v1.yaml")
        self.document = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        self.path = ROOT / ".test-manifest.json"
        self.addCleanup(self.path.unlink, missing_ok=True)

    def write(self) -> None:
        self.path.write_text(json.dumps(self.document), encoding="utf-8")

    def test_manifest_lengkap_dianggap_cocok(self) -> None:
        self.write()
        report = verify_manifest(self.path, "A", self.suite, enabled=True)
        self.assertEqual(report["status"], "matched")
        self.assertTrue(report["match"])
        self.assertIsNotNone(report["selected"]["logic_sha256"])
        self.assertIsNotNone(report["selected"]["io_config_sha256"])
        self.assertIsNotNone(report["suite"])
        self.assertTrue(report["suite"]["match"])

    def test_artefak_kosong_ditolak(self) -> None:
        self.document["fixtures"]["A"]["artifacts"] = []
        self.write()
        with self.assertRaisesRegex(ConfigError, "artefak wajib"):
            verify_manifest(self.path, "A", self.suite, enabled=True)

    def test_artefak_wajib_tidak_lengkap_ditolak(self) -> None:
        artifacts = self.document["fixtures"]["A"]["artifacts"]
        self.document["fixtures"]["A"]["artifacts"] = [
            item for item in artifacts if item["kind"] != "io_config"
        ]
        self.write()
        with self.assertRaisesRegex(ConfigError, "io_config"):
            verify_manifest(self.path, "A", self.suite, enabled=True)

    def test_suite_wajib_ada(self) -> None:
        del self.document["suite"]
        self.write()
        with self.assertRaisesRegex(ConfigError, "entri suite wajib"):
            verify_manifest(self.path, "A", self.suite, enabled=True)


class NoConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_pesan_tanpa_request_modbus(self) -> None:
        timing = Timing(
            plc_scan_s=0.02,
            response_timeout_s=0.05,
            hold_window_s=0.01,
            min_alarm_samples=2,
            reset_reads=2,
        )
        lab = Lab("A", "127.0.0.1", 15020, 1, timing)
        base = lab.loop.time()
        with self.assertRaisesRegex(Incomplete, "Tidak ada request Modbus"):
            await lab.wait_reads(4, 0, 75, 1, 0, base)


class HoldWindowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        timing = Timing(
            plc_scan_s=0.02,
            response_timeout_s=1.0,
            hold_window_s=0.01,
            min_alarm_samples=2,
            reset_reads=2,
        )
        self.lab = Lab("A", "127.0.0.1", 1502, 1, timing)
        self.step = Step(
            name="Reset di 95%",
            inputs=((2, True),),
            alarm=True,
            hold=True,
        )

    async def test_alarm_off_sebelum_jendela_segar_gagal(self) -> None:
        base = self.lab.loop.time()
        applied_at = base
        input_read_at = base + 0.1
        self.lab.samples = [
            # PLC sempat menulis OFF setelah reset, sebelum pembacaan kedua
            # dikonfirmasi dan sebelum jendela sampel segar dimulai.
            Sample(base + 0.05, WRITE_ALARM, OFFSET, False),
            Sample(base + 0.13, WRITE_ALARM, OFFSET, True),
            Sample(base + 0.14, WRITE_ALARM, OFFSET, True),
        ]
        with self.assertRaises(FunctionalFailure):
            await self.lab.verify_alarm(
                self.step, 0, applied_at, input_read_at,
            )

    async def test_hold_stabil_lulus(self) -> None:
        base = self.lab.loop.time()
        applied_at = base
        input_read_at = base + 0.1
        self.lab.samples = [
            Sample(base + 0.05, WRITE_ALARM, OFFSET, True),
            Sample(base + 0.13, WRITE_ALARM, OFFSET, True),
            Sample(base + 0.14, WRITE_ALARM, OFFSET, True),
        ]
        await self.lab.verify_alarm(self.step, 0, applied_at, input_read_at)

    async def test_sampel_segar_kurang_tidak_selesai(self) -> None:
        base = self.lab.loop.time()
        applied_at = base
        input_read_at = base + 0.1
        self.lab.samples = [
            Sample(base + 0.05, WRITE_ALARM, OFFSET, True),
            Sample(base + 0.13, WRITE_ALARM, OFFSET, True),
        ]
        with self.assertRaises(Incomplete):
            await self.lab.verify_alarm(
                self.step, 0, applied_at, input_read_at,
            )


if __name__ == "__main__":
    unittest.main()
