"""VORA — Verification of Restored Automation.

Runner menyediakan perangkat I/O virtual melalui Modbus TCP, memberi stimulus
level dan reset, lalu menilai keluaran alarm yang ditulis PLC hasil pemulihan.
Skenario dan ekspektasi dibaca dari berkas YAML yang disetujui, bukan dari kode.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pymodbus
import yaml
from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import ModbusTcpServer

RUNNER_VERSION = "0.2.0"
REPORT_SCHEMA_VERSION = 3

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 1502
DEFAULT_UNIT = 1
DEFAULT_SUITE = Path("suites/tank-alarm-v1.yaml")
DEFAULT_MANIFEST = Path("manifest.json")

# Alamat pada perangkat virtual (0-based, sama dengan offset Remote Device).
OFFSET = 0
RESERVE_OFFSET = 1
NORMAL_LEVEL = 75

READ_RESET = 2
READ_LEVEL = 4
WRITE_ALARM = 5

STATUSES = ("PASS", "FAIL", "BLOCKED", "INCOMPLETE")

DEFAULT_TIMING = {
    "plc_scan_ms": 20,
    "response_timeout_ms": 3000,
    "hold_window_ms": 1000,
    "min_alarm_samples": 2,
    "reset_reads": 2,
}


class ConfigError(Exception):
    """Suite, manifest, atau argumen tidak valid (exit code 2)."""


class Incomplete(Exception):
    """Pengamatan tidak cukup atau komunikasi gagal (exit code 2)."""


class FunctionalFailure(Exception):
    """Perilaku PLC berbeda dari ekspektasi (exit code 1)."""


@dataclass(frozen=True)
class Step:
    name: str
    inputs: tuple[tuple[int, int | bool], ...]
    alarm: bool | None = None
    hold: bool = False


@dataclass(frozen=True)
class Case:
    id: str
    name: str
    steps: tuple[Step, ...]
    prerequisites: tuple[Step, ...] = ()
    continue_after_failure: bool = False


@dataclass(frozen=True)
class Timing:
    plc_scan_s: float
    response_timeout_s: float
    hold_window_s: float
    min_alarm_samples: int
    reset_reads: int

    def as_report(self) -> dict[str, Any]:
        return {
            "plc_scan_ms": round(self.plc_scan_s * 1000, 3),
            "response_timeout_ms": round(self.response_timeout_s * 1000, 3),
            "hold_window_ms": round(self.hold_window_s * 1000, 3),
            "min_alarm_samples": self.min_alarm_samples,
            "reset_reads": self.reset_reads,
        }


@dataclass(frozen=True)
class Suite:
    id: str
    version: int
    source: str
    description: str | None
    timing: Timing
    cases: tuple[Case, ...]
    path: Path
    sha256: str


@dataclass(frozen=True)
class Sample:
    at: float
    function_code: int
    offset: int
    value: int | bool | None
    exception: int | None = None


# ---------------------------------------------------------------------------
# Validasi YAML
# ---------------------------------------------------------------------------


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where} harus berupa mapping.")
    return value


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} harus berupa teks yang tidak kosong.")
    return value.strip()


def _integer(value: Any, where: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where} harus berupa bilangan bulat.")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{where} harus di antara {minimum} dan {maximum}.")
    return value


def _boolean(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where} harus berupa true atau false.")
    return value


def _parse_timing(value: Any) -> Timing:
    raw = _mapping(value or {}, "timing")
    plc_scan_ms = _integer(
        raw.get("plc_scan_ms", DEFAULT_TIMING["plc_scan_ms"]),
        "timing.plc_scan_ms", 1, 1000,
    )
    timeout_ms = _integer(
        raw.get("response_timeout_ms", DEFAULT_TIMING["response_timeout_ms"]),
        "timing.response_timeout_ms", 100, 60000,
    )
    hold_ms = _integer(
        raw.get("hold_window_ms", DEFAULT_TIMING["hold_window_ms"]),
        "timing.hold_window_ms", 0, 60000,
    )
    min_samples = _integer(
        raw.get("min_alarm_samples", DEFAULT_TIMING["min_alarm_samples"]),
        "timing.min_alarm_samples", 1, 100,
    )
    reset_reads = _integer(
        raw.get("reset_reads", DEFAULT_TIMING["reset_reads"]),
        "timing.reset_reads", 1, 20,
    )
    return Timing(
        plc_scan_s=plc_scan_ms / 1000,
        response_timeout_s=timeout_ms / 1000,
        hold_window_s=hold_ms / 1000,
        min_alarm_samples=min_samples,
        reset_reads=reset_reads,
    )


def _parse_step(value: Any, where: str, index: int) -> Step:
    raw = _mapping(value, where)
    unknown = set(raw) - {"name", "level_pct", "reset", "expect_alarm", "mode", "description"}
    if unknown:
        raise ConfigError(f"{where} memuat kunci tidak dikenal: {sorted(unknown)}.")

    inputs: list[tuple[int, int | bool]] = []
    if "level_pct" in raw:
        inputs.append((READ_LEVEL, _integer(raw["level_pct"], f"{where}.level_pct", 0, 100)))
    if "reset" in raw:
        inputs.append((READ_RESET, _boolean(raw["reset"], f"{where}.reset")))
    if not inputs:
        raise ConfigError(f"{where} harus menetapkan minimal level_pct atau reset.")

    alarm = raw.get("expect_alarm")
    if alarm is not None:
        alarm = _boolean(alarm, f"{where}.expect_alarm")

    mode = _string(raw.get("mode", "reach"), f"{where}.mode")
    if mode not in ("reach", "hold"):
        raise ConfigError(f"{where}.mode harus 'reach' atau 'hold'.")
    if mode == "hold" and alarm is None:
        raise ConfigError(f"{where} memakai mode hold tanpa expect_alarm.")

    name = _string(raw.get("name", f"Langkah {index}"), f"{where}.name")
    return Step(name=name, inputs=tuple(inputs), alarm=alarm, hold=mode == "hold")


def _parse_steps(value: Any, where: str) -> tuple[Step, ...]:
    if value is None:
        raise ConfigError(f"{where} tidak boleh kosong.")
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where} harus berupa daftar langkah yang tidak kosong.")
    return tuple(
        _parse_step(item, f"{where}[{index}]", index)
        for index, item in enumerate(value, 1)
    )


def load_suite(path: Path) -> Suite:
    if not path.is_file():
        raise ConfigError(f"Berkas suite tidak ditemukan: {path}")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML tidak valid pada {path}: {exc}") from exc

    root = _mapping(document, "suite")
    unknown = {
        key for key in root
        if key not in {"suite_id", "version", "source", "description", "timing", "cases"}
        and not key.startswith("x-")
    }
    if unknown:
        raise ConfigError(f"suite memuat kunci tidak dikenal: {sorted(unknown)}.")

    suite_id = _string(root.get("suite_id"), "suite.suite_id")
    version = _integer(root.get("version"), "suite.version", 1, 1_000_000)
    source = _string(root.get("source"), "suite.source")
    description = root.get("description")
    if description is not None:
        description = _string(description, "suite.description")
    timing = _parse_timing(root.get("timing"))

    raw_cases = root.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ConfigError("suite.cases harus berupa daftar skenario yang tidak kosong.")

    cases: list[Case] = []
    seen_ids: set[str] = set()
    for index, raw_case in enumerate(raw_cases, 1):
        where = f"suite.cases[{index}]"
        item = _mapping(raw_case, where)
        unknown = set(item) - {
            "id", "name", "steps", "prerequisites", "continue_after_failure", "description",
        }
        if unknown:
            raise ConfigError(f"{where} memuat kunci tidak dikenal: {sorted(unknown)}.")

        case_id = _string(item.get("id"), f"{where}.id")
        if case_id in seen_ids:
            raise ConfigError(f"ID skenario duplikat: {case_id}")
        seen_ids.add(case_id)

        case_name = _string(item.get("name"), f"{where}.name")
        prerequisites = (
            _parse_steps(item["prerequisites"], f"{where}.prerequisites")
            if "prerequisites" in item else ()
        )
        steps = _parse_steps(item.get("steps"), f"{where}.steps")
        continue_after_failure = item.get("continue_after_failure", False)
        if not isinstance(continue_after_failure, bool):
            raise ConfigError(f"{where}.continue_after_failure harus berupa true atau false.")

        cases.append(Case(
            id=case_id,
            name=case_name,
            steps=steps,
            prerequisites=prerequisites,
            continue_after_failure=continue_after_failure,
        ))

    return Suite(
        id=suite_id,
        version=version,
        source=source,
        description=description,
        timing=timing,
        cases=tuple(cases),
        path=path,
        sha256=sha256_file(path),
    )


# ---------------------------------------------------------------------------
# Manifest dan hash artefak
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value.lower())


def _manifest_fixture_summary(entry: dict[str, Any]) -> dict[str, str | None]:
    summary: dict[str, str | None] = {"logic_sha256": None, "io_config_sha256": None}
    for artifact in entry.get("artifacts", []):
        kind = artifact.get("kind")
        if kind == "logic":
            summary["logic_sha256"] = artifact.get("sha256")
        elif kind == "io_config":
            summary["io_config_sha256"] = artifact.get("sha256")
    return summary


REQUIRED_ARTIFACT_KINDS = ("logic", "io_config", "project")


def verify_manifest(
    path: Path,
    fixture: str,
    suite: Suite,
    enabled: bool,
) -> dict[str, Any]:
    """Bandingkan artefak yang dipakai dengan manifest SHA-256."""
    report: dict[str, Any] = {
        "enabled": enabled,
        "path": str(path),
        "status": "unavailable",
        "match": None,
        "sha256": None,
        "reason": None,
        "selected": None,
        "suite": None,
        "fixtures": {},
        "items": [],
    }
    if not enabled:
        report["reason"] = "Verifikasi manifest dimatikan lewat --no-manifest."
        return report
    if not path.is_file():
        report["reason"] = f"Manifest tidak ditemukan: {path}. Jalankan tools/build_manifest.py."
        return report

    report["sha256"] = sha256_file(path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Manifest tidak dapat dibaca: {path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ConfigError(f"Manifest {path} harus memakai schema_version 1.")

    fixtures = document.get("fixtures")
    if not isinstance(fixtures, dict) or not fixtures:
        raise ConfigError(f"Manifest {path} tidak memuat daftar fixtures.")

    for name, entry in fixtures.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("artifacts"), list):
            raise ConfigError(f"Manifest fixture {name} tidak memuat artifacts.")
        kinds: set[str] = set()
        for artifact in entry["artifacts"]:
            if not isinstance(artifact, dict):
                raise ConfigError(f"Manifest fixture {name} memuat artefak tidak valid.")
            kinds.add(_string(artifact.get("kind"), f"manifest.fixtures.{name}.kind"))
            _string(artifact.get("path"), f"manifest.fixtures.{name}.path")
            if not _is_sha256(artifact.get("sha256")):
                raise ConfigError(f"Manifest fixture {name} memuat sha256 tidak valid.")

        # Manifest kosong tidak boleh dianggap cocok: tanpa artefak wajib,
        # tidak ada satu pun berkas yang benar-benar diperiksa.
        missing = set(REQUIRED_ARTIFACT_KINDS) - kinds
        if not entry["artifacts"] or missing:
            detail = sorted(missing) if missing else sorted(REQUIRED_ARTIFACT_KINDS)
            raise ConfigError(
                f"Manifest fixture {name} tidak memuat artefak wajib: {detail}. "
                "Jalankan tools/build_manifest.py."
            )
        report["fixtures"][name] = _manifest_fixture_summary(entry)

    selected = fixtures.get(fixture)
    if not isinstance(selected, dict):
        raise ConfigError(f"Manifest {path} tidak memuat fixture {fixture}.")

    all_match = True
    for artifact in selected["artifacts"]:
        kind = _string(artifact.get("kind"), f"manifest.fixtures.{fixture}.kind")
        relative = _string(artifact.get("path"), f"manifest.fixtures.{fixture}.path")
        expected = artifact["sha256"].lower()
        target = (path.parent / relative).resolve()
        actual = sha256_file(target) if target.is_file() else None
        match = actual == expected
        all_match = all_match and match
        report["items"].append({
            "kind": kind,
            "path": relative,
            "expected_sha256": expected,
            "actual_sha256": actual,
            "match": match,
        })

    suite_entry = document.get("suite")
    if not isinstance(suite_entry, dict):
        raise ConfigError(
            "Manifest tidak memuat entri suite wajib. Jalankan tools/build_manifest.py."
        )
    expected = suite_entry.get("sha256")
    relative = suite_entry.get("path")
    if not _is_sha256(expected) or not isinstance(relative, str):
        raise ConfigError("Manifest memuat entri suite yang tidak valid.")
    suite_match = (
        expected.lower() == suite.sha256
        and (path.parent / relative).resolve() == suite.path.resolve()
    )
    all_match = all_match and suite_match
    report["suite"] = {
        "path": relative,
        "expected_sha256": expected.lower(),
        "actual_sha256": suite.sha256,
        "match": suite_match,
    }

    selected_summary = _manifest_fixture_summary(selected)
    selected_summary["items"] = report["items"]
    report["selected"] = selected_summary
    report["match"] = all_match
    report["status"] = "matched" if all_match else "mismatch"
    if not all_match:
        report["reason"] = "Artefak yang dipakai tidak cocok dengan manifest."
    return report


# ---------------------------------------------------------------------------
# Eksekusi
# ---------------------------------------------------------------------------


class Lab:
    def __init__(
        self,
        fixture: str,
        host: str,
        port: int,
        unit: int,
        timing: Timing,
    ) -> None:
        self.loop = asyncio.get_running_loop()
        self.origin = self.loop.time()
        self.fixture = fixture
        self.host = host
        self.port = port
        self.unit = unit
        self.timing = timing
        self.level_offset = RESERVE_OFFSET if fixture == "B" else OFFSET
        self.samples: list[Sample] = []
        self.pending: dict[tuple[int, int, int], int] = {}
        self.device = ModbusDeviceContext(
            ir=ModbusSequentialDataBlock(1, [NORMAL_LEVEL, NORMAL_LEVEL]),
            di=ModbusSequentialDataBlock(1, [False]),
            co=ModbusSequentialDataBlock(1, [False]),
        )

    def trace(self, sending: bool, pdu):
        function_code = pdu.function_code & 0x7F
        if pdu.dev_id != self.unit or function_code not in (
            READ_RESET, READ_LEVEL, WRITE_ALARM,
        ):
            return pdu

        key = (pdu.dev_id, pdu.transaction_id, function_code)
        if not sending:
            self.pending[key] = pdu.address
            return pdu

        offset = self.pending.pop(key, None)
        if offset is None:
            return pdu
        if pdu.isError():
            sample = Sample(self.loop.time(), function_code, offset, None, pdu.exception_code)
        else:
            values = pdu.registers if function_code == READ_LEVEL else pdu.bits
            if not values:
                return pdu
            sample = Sample(self.loop.time(), function_code, offset, values[0])
        self.samples.append(sample)
        return pdu

    def evidence(self, sample: Sample) -> dict[str, Any]:
        entry = asdict(sample)
        entry["elapsed_ms"] = round((entry.pop("at") - self.origin) * 1000, 1)
        return entry

    async def wait_reads(
        self,
        function_code: int,
        offset: int,
        expected: int | bool,
        count: int,
        start_index: int,
        since: float,
    ) -> Sample:
        deadline = self.loop.time() + self.timing.response_timeout_s
        last_exception: int | None = None
        while self.loop.time() < deadline:
            matches: list[Sample] = []
            for sample in self.samples[start_index:]:
                if sample.function_code != function_code or sample.offset != offset:
                    continue
                if sample.exception is not None:
                    last_exception = sample.exception
                    continue
                if sample.at >= since and sample.value == expected:
                    matches.append(sample)
            if len(matches) >= count:
                return matches[count - 1]
            await asyncio.sleep(0.02)

        detail = f"FC{function_code} offset {offset} belum membaca {expected!r} " \
                 f"sebanyak {count} kali."
        if last_exception is not None:
            detail += f" Exception Modbus terakhir: {last_exception}."
        received = [
            sample for sample in self.samples[start_index:]
            if sample.exception is None
        ]
        if not received:
            detail += (
                f" Tidak ada request Modbus yang diterima di "
                f"{self.host}:{self.port} (unit {self.unit}); pastikan PLC "
                f"atau fake PLC berjalan dan terhubung."
            )
        raise Incomplete(detail)

    def alarm_samples(self, start_index: int, since: float) -> list[Sample]:
        return [
            sample for sample in self.samples[start_index:]
            if sample.function_code == WRITE_ALARM
            and sample.offset == OFFSET
            and sample.exception is None
            and sample.at >= since
        ]

    async def verify_alarm(
        self,
        step: Step,
        start_index: int,
        applied_at: float,
        input_read_at: float,
    ) -> None:
        since = input_read_at + self.timing.plc_scan_s
        if step.hold:
            await asyncio.sleep(max(0.0, since + self.timing.hold_window_s - self.loop.time()))

            # Langkah hold menyatakan kondisi yang harus bertahan. Periksa
            # seluruh jejak sejak stimulus diterapkan, termasuk transisi
            # sebelum pembacaan input terkonfirmasi, supaya alarm yang sempat
            # padam di celah itu tidak lolos.
            observed = self.alarm_samples(start_index, applied_at)
            deviations = [sample for sample in observed if sample.value != step.alarm]
            if deviations:
                first = deviations[0]
                elapsed = round((first.at - self.origin) * 1000, 1)
                raise FunctionalFailure(
                    f"Alarm menyimpang dari {step.alarm!r} pada {elapsed} ms "
                    f"(nilai {first.value!r}) selama langkah hold."
                )

            # Tetap syaratkan sampel segar setelah PLC membaca input.
            recent = self.alarm_samples(start_index, since)
            if len(recent) < self.timing.min_alarm_samples:
                raise Incomplete("Sampel FC05 segar kurang selama jendela uji.")
            return

        deadline = self.loop.time() + self.timing.response_timeout_s
        recent: list[Sample] = []
        while self.loop.time() < deadline:
            recent = self.alarm_samples(start_index, since)
            minimum = self.timing.min_alarm_samples
            if len(recent) >= minimum and all(
                sample.value == step.alarm for sample in recent[-minimum:]
            ):
                return
            await asyncio.sleep(0.02)

        if len(recent) < self.timing.min_alarm_samples:
            raise Incomplete("Sampel FC05 segar kurang sebelum tenggat.")
        raise FunctionalFailure(f"Alarm tidak mencapai {step.alarm!r} sebelum tenggat.")

    async def run_step(self, step: Step, result: dict[str, Any]) -> None:
        start_index = len(self.samples)
        applied_at = self.loop.time()
        result["executed"] = True
        result["stimulus_at_ms"] = round((applied_at - self.origin) * 1000, 1)
        try:
            for function_code, value in step.inputs:
                response = self.device.setValues(function_code, OFFSET, [value])
                if response is not None:
                    raise Incomplete(
                        f"Penulisan FC{function_code} offset {OFFSET} ditolak: {response}."
                    )

            input_read_at = applied_at
            for function_code, value in step.inputs:
                if function_code == READ_LEVEL:
                    read_offset = self.level_offset
                    expected = NORMAL_LEVEL if read_offset == RESERVE_OFFSET else value
                    count = 1
                else:
                    read_offset = OFFSET
                    expected = value
                    count = self.timing.reset_reads
                sample = await self.wait_reads(
                    function_code, read_offset, expected, count, start_index, applied_at,
                )
                result["observed_inputs"].append(self.evidence(sample))
                input_read_at = max(input_read_at, sample.at)

            if step.alarm is not None:
                await self.verify_alarm(step, start_index, applied_at, input_read_at)
            result["status"] = "PASS"
        except (Incomplete, FunctionalFailure) as exc:
            result["status"] = "INCOMPLETE" if isinstance(exc, Incomplete) else "FAIL"
            result["reason"] = str(exc)
        finally:
            result["samples"] = [
                self.evidence(sample) for sample in self.samples[start_index:]
            ]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _step_record(case_id: str, step_id: str, phase: str, step: Step) -> dict[str, Any]:
    return {
        "id": f"{case_id}.{step_id}",
        "name": step.name,
        "phase": phase,
        "inputs": [
            {"function_code": function_code, "offset": OFFSET, "value": value}
            for function_code, value in step.inputs
        ],
        "observed_inputs": [],
        "expected_alarm": step.alarm,
        "mode": "hold" if step.hold else "reach",
        "status": "BLOCKED",
        "executed": False,
        "stimulus_at_ms": None,
        "reason": None,
        "samples": [],
    }


def case_record(case: Case) -> dict[str, Any]:
    steps = [
        _step_record(case.id, f"p{index}", "prerequisite", step)
        for index, step in enumerate(case.prerequisites, 1)
    ]
    steps.extend(
        _step_record(case.id, str(index), "test", step)
        for index, step in enumerate(case.steps, 1)
    )
    return {
        "id": case.id,
        "name": case.name,
        "status": "BLOCKED",
        "executed": False,
        "reason": None,
        "steps": steps,
    }


async def run_case(lab: Lab, case: Case, record: dict[str, Any]) -> str:
    record["status"] = "INCOMPLETE"
    record["executed"] = True
    case_status = "PASS"
    plan = list(zip(case.prerequisites + case.steps, record["steps"]))

    for step, result in plan:
        result["status"] = "INCOMPLETE"
        await lab.run_step(step, result)
        if result["status"] == "PASS":
            continue

        case_status = result["status"]
        if case_status == "FAIL" and result["phase"] == "prerequisite":
            case_status = "BLOCKED"
        reason = f"{result['id']}: {result['reason']}"
        if record["reason"] is None:
            record["reason"] = reason
        if case_status != "FAIL" or not case.continue_after_failure:
            break

    record["status"] = case_status
    return case_status


def new_report(args: argparse.Namespace, run_id: str) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "run_id": run_id,
        "fixture": args.fixture,
        "fixture_selection": "operator",
        "status": "INCOMPLETE",
        "reason": None,
        "started_at": utc_now(),
        "finished_at": None,
        "versions": {
            "runner": RUNNER_VERSION,
            "pymodbus": pymodbus.__version__,
            "editor": args.editor_version,
            "runtime": args.runtime_version,
        },
        "modbus": {
            "host": args.host,
            "port": args.port,
            "unit": args.unit,
            "level_stimulus_offset": OFFSET,
            "reserve_level_offset": RESERVE_OFFSET,
            "expected_level_read_offset": RESERVE_OFFSET if args.fixture == "B" else OFFSET,
            "reserve_level": NORMAL_LEVEL,
        },
        "timing": None,
        "suite": None,
        "artifacts": None,
        "cases": [],
        "coverage": None,
        "summary": None,
    }


def summarize(report: dict[str, Any]) -> None:
    cases = report["cases"]
    steps = [step for case in cases for step in case["steps"]]
    by_status = {status: 0 for status in STATUSES}
    for step in steps:
        by_status[step["status"]] = by_status.get(step["status"], 0) + 1
    report["summary"] = {
        status: sum(case["status"] == status for case in cases)
        for status in STATUSES
    }
    report["coverage"] = {
        "cases_total": len(cases),
        "cases_executed": sum(bool(case["executed"]) for case in cases),
        "steps_total": len(steps),
        "steps_executed": sum(bool(step["executed"]) for step in steps),
        "steps_by_status": by_status,
    }


def write_report(run_dir: Path, report: dict[str, Any]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "report.json"
    temporary = report_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(report_path)
    return report_path


async def execute(args: argparse.Namespace) -> int:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{args.fixture}-{timestamp}-{uuid4().hex[:8]}"
    run_dir = args.report_dir / run_id
    report = new_report(args, run_id)
    label = f"[Fixture {args.fixture}]"
    server: ModbusTcpServer | None = None

    try:
        suite = load_suite(args.suite)
        report["suite"] = {
            "suite_id": suite.id,
            "version": suite.version,
            "source": suite.source,
            "description": suite.description,
            "path": str(suite.path),
            "sha256": suite.sha256,
        }
        report["timing"] = suite.timing.as_report()
        report["cases"] = [case_record(case) for case in suite.cases]

        artifacts = verify_manifest(args.manifest, args.fixture, suite, not args.no_manifest)
        report["artifacts"] = artifacts
        for item in artifacts["items"]:
            state = "cocok" if item["match"] else "TIDAK cocok"
            print(f"{label} Artefak {item['kind']}: {state} ({item['path']})")
        if artifacts["status"] == "unavailable":
            print(f"{label} Peringatan: {artifacts['reason']}")
        elif artifacts["status"] == "mismatch":
            raise ConfigError(artifacts["reason"] or "Artefak tidak cocok dengan manifest.")

        lab = Lab(
            fixture=args.fixture,
            host=args.host,
            port=args.port,
            unit=args.unit,
            timing=suite.timing,
        )
        server = ModbusTcpServer(
            ModbusServerContext(devices={args.unit: lab.device}, single=False),
            address=(args.host, args.port),
            trace_pdu=lab.trace,
        )
        await server.serve_forever(background=True)
        print(f"{label} Modbus I/O siap di {args.host}:{args.port} (unit {args.unit}).")

        if args.auto_start:
            print(f"{label} Mode otomatis: mulai tanpa menunggu Enter.")
        else:
            await asyncio.to_thread(
                input,
                f"{label} Jalankan proyek di Runtime, lalu tekan Enter: ",
            )

        for case, current in zip(suite.cases, report["cases"]):
            status = await run_case(lab, case, current)
            detail = current["reason"] or case.name
            print(f"{label} {status} {case.id}: {detail}")
            if status == "INCOMPLETE":
                report["reason"] = current["reason"]
                return 2

        if any(case["status"] != "PASS" for case in report["cases"]):
            report["status"] = "FAIL"
            return 1

        report["status"] = "PASS"
        return 0

    except ConfigError as exc:
        report["status"] = "INCOMPLETE"
        report["reason"] = str(exc)
        print(f"{label} INCOMPLETE: {exc}")
        return 2
    except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001
        # Semua gangguan alat/I/O dipetakan ke status "Uji tidak selesai".
        reason = str(exc) or type(exc).__name__
        report["status"] = "INCOMPLETE"
        report["reason"] = reason
        print(f"{label} INCOMPLETE: {reason}")
        return 2
    finally:
        report["finished_at"] = utc_now()
        for case in report["cases"]:
            if case["status"] == "BLOCKED" and not case.get("reason"):
                case["reason"] = "Suite berhenti sebelum skenario ini dieksekusi."
        summarize(report)
        if report["status"] != "INCOMPLETE" and any(
            case["status"] == "INCOMPLETE" for case in report["cases"]
        ):
            report["status"] = "INCOMPLETE"
        report_path = write_report(run_dir, report)
        counts = ", ".join(
            f"{status}={count}" for status, count in (report["summary"] or {}).items()
        )
        print(f"{label} Hasil: {report['status']} ({counts})")
        print(f"{label} Laporan: {report_path}")
        if server is not None:
            try:
                await server.shutdown()
            except Exception as exc:  # noqa: BLE001
                print(f"{label} Peringatan saat menutup server: {exc}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--fixture", choices=("A", "B", "C"), default="A",
                        help="Fixture lab yang dijalankan operator.")
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE,
                        help="Berkas YAML skenario yang sudah disetujui.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST,
                        help="Manifest SHA-256 artefak yang dipakai.")
    parser.add_argument("--no-manifest", action="store_true",
                        help="Lewati verifikasi manifest (hasil hash tidak dicatat).")
    parser.add_argument("--report-dir", type=Path, default=Path("runs"),
                        help="Direktori keluaran laporan.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Alamat server Modbus.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port server Modbus.")
    parser.add_argument("--unit", type=int, default=DEFAULT_UNIT, help="Unit ID Modbus.")
    parser.add_argument("--editor-version", default="not-recorded",
                        help="Versi OpenPLC Editor yang dipakai.")
    parser.add_argument("--runtime-version", default="not-recorded",
                        help="Versi OpenPLC Runtime yang dipakai.")
    parser.add_argument("--auto-start", action="store_true",
                        help="Mulai pengujian tanpa menunggu Enter (untuk otomasi).")
    parser.add_argument("--version", action="version",
                        version=f"VORA runner {RUNNER_VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    # Kompatibilitas dengan bentuk perintah pada rancangan: `vora run ...`.
    if arguments and arguments[0] == "run":
        arguments = arguments[1:]
    args = build_parser().parse_args(arguments)
    return asyncio.run(execute(args))


if __name__ == "__main__":
    raise SystemExit(main())
