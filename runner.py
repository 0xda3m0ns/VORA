import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pymodbus
from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import ModbusTcpServer


HOST = "127.0.0.1"
PORT = 1502
UNIT = 1
OFFSET = 0
RESERVE_OFFSET = 1
NORMAL_LEVEL = 75
READ_RESET = 2
READ_LEVEL = 4
WRITE_ALARM = 5
TIMEOUT = 5.0
PLC_SCAN = 0.020
HOLD_WINDOW = 1.0
MIN_SAMPLES = 2


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


PREPARE_NORMAL = (
    Step("Baca level 75% dan reset lepas", ((READ_LEVEL, NORMAL_LEVEL), (READ_RESET, False))),
    Step("Reset persiapan di 75%", ((READ_RESET, True),), False),
    Step("Lepas reset persiapan", ((READ_RESET, False),), False, hold=True),
)
PREPARE_HIGH = PREPARE_NORMAL + (
    Step("Aktifkan alarm di 95%", ((READ_LEVEL, 95),), True),
)

CASES = (
    Case("S01", "Kondisi normal pada 75% setelah persiapan", PREPARE_NORMAL),
    Case(
        "S02", "Aktivasi alarm",
        (Step("Naik ke 80%", ((READ_LEVEL, 80),), True),
         Step("Naik ke 85%", ((READ_LEVEL, 85),), True)),
        prerequisites=PREPARE_NORMAL,
        continue_after_failure=True,
    ),
    Case(
        "S03", "Penahanan alarm setelah level turun",
        (Step("Turun ke 75%", ((READ_LEVEL, NORMAL_LEVEL),), True, hold=True),),
        prerequisites=PREPARE_HIGH,
    ),
    Case(
        "S04", "Reset saat level tinggi",
        (Step("Tekan reset di 95%", ((READ_RESET, True),), True, hold=True),
         Step("Lepas reset di 95%", ((READ_RESET, False),), True, hold=True)),
        prerequisites=PREPARE_HIGH,
    ),
    Case(
        "S05", "Reset saat level rendah",
        (Step("Tekan reset di 75%", ((READ_RESET, True),), False),
         Step("Lepas reset di 75%", ((READ_RESET, False),), False, hold=True)),
        prerequisites=PREPARE_HIGH + (
            Step("Turun ke 75% dengan alarm aktif", ((READ_LEVEL, NORMAL_LEVEL),), True, hold=True),
        ),
    ),
)


@dataclass(frozen=True)
class Sample:
    at: float
    function_code: int
    offset: int
    value: int | bool | None
    exception: int | None = None


class Incomplete(Exception):
    pass


class FunctionalFailure(Exception):
    pass


class Lab:
    def __init__(self, fixture):
        self.loop = asyncio.get_running_loop()
        self.origin = self.loop.time()
        self.level_offset = RESERVE_OFFSET if fixture == "B" else OFFSET
        self.samples: list[Sample] = []
        self.pending: dict[tuple[int, int, int], int] = {}
        self.device = ModbusDeviceContext(
            ir=ModbusSequentialDataBlock(1, [NORMAL_LEVEL, NORMAL_LEVEL]),
            di=ModbusSequentialDataBlock(1, [False]),
            co=ModbusSequentialDataBlock(1, [False]),
        )

    def trace(self, sending, pdu):
        fc = pdu.function_code & 0x7F
        if pdu.dev_id != UNIT or fc not in (READ_RESET, READ_LEVEL, WRITE_ALARM):
            return pdu

        key = (pdu.dev_id, pdu.transaction_id, fc)
        if not sending:
            self.pending[key] = pdu.address
            return pdu

        offset = self.pending.pop(key, None)
        if offset is None:
            return pdu
        if pdu.isError():
            sample = Sample(self.loop.time(), fc, offset, None, pdu.exception_code)
        else:
            values = pdu.registers if fc == READ_LEVEL else pdu.bits
            if not values:
                return pdu
            sample = Sample(self.loop.time(), fc, offset, values[0])
        self.samples.append(sample)
        return pdu

    def evidence(self, sample):
        entry = asdict(sample)
        entry["elapsed_ms"] = round((entry.pop("at") - self.origin) * 1000, 1)
        return entry

    async def wait_read(self, fc, offset, expected, start_index):
        deadline = self.loop.time() + TIMEOUT
        while self.loop.time() < deadline:
            for sample in self.samples[start_index:]:
                if (
                    sample.function_code == fc
                    and sample.offset == offset
                    and sample.exception is None
                    and sample.value == expected
                ):
                    return sample
            await asyncio.sleep(0.02)
        raise Incomplete(f"FC{fc} offset {offset} belum membaca {expected!r}.")

    def alarm_samples(self, start_index, since):
        return [
            sample for sample in self.samples[start_index:]
            if sample.function_code == WRITE_ALARM
            and sample.offset == OFFSET
            and sample.exception is None
            and sample.at >= since
        ]

    async def verify_alarm(self, step, start_index, input_read_at, applied_at):
        since = input_read_at + PLC_SCAN
        if step.hold:
            await asyncio.sleep(max(0.0, since + HOLD_WINDOW - self.loop.time()))
            recent = self.alarm_samples(start_index, since)
            if len(recent) < MIN_SAMPLES:
                raise Incomplete("Sampel FC05 segar kurang selama jendela uji.")
            # Langkah hold dimulai dengan alarm pada nilai yang diharapkan.
            # Periksa juga sampel yang masuk sebelum pembacaan input baru.
            if any(sample.value != step.alarm for sample in self.alarm_samples(
                start_index, applied_at
            )):
                raise FunctionalFailure(f"Alarm menyimpang dari {step.alarm!r} selama uji.")
            return

        deadline = self.loop.time() + TIMEOUT
        while self.loop.time() < deadline:
            recent = self.alarm_samples(start_index, since)
            if len(recent) >= MIN_SAMPLES and all(
                sample.value == step.alarm for sample in recent[-MIN_SAMPLES:]
            ):
                return
            await asyncio.sleep(0.02)
        if len(self.alarm_samples(start_index, since)) < MIN_SAMPLES:
            raise Incomplete("Sampel FC05 segar kurang sebelum tenggat.")
        raise FunctionalFailure(f"Alarm tidak mencapai {step.alarm!r} sebelum tenggat.")

    async def run_step(self, step, result):
        start_index = len(self.samples)
        applied_at = self.loop.time()
        try:
            for fc, value in step.inputs:
                self.device.setValues(fc, OFFSET, [value])
            input_read_at = applied_at
            for fc, value in step.inputs:
                read_offset = self.level_offset if fc == READ_LEVEL else OFFSET
                expected_read = NORMAL_LEVEL if read_offset == RESERVE_OFFSET else value
                sample = await self.wait_read(fc, read_offset, expected_read, start_index)
                result["observed_inputs"].append(self.evidence(sample))
                input_read_at = max(input_read_at, sample.at)
            if step.alarm is not None:
                await self.verify_alarm(step, start_index, input_read_at, applied_at)
            result["status"] = "PASS"
        except (Incomplete, FunctionalFailure) as exc:
            result["status"] = "INCOMPLETE" if isinstance(exc, Incomplete) else "FAIL"
            result["reason"] = str(exc)
        finally:
            result["samples"] = [self.evidence(sample) for sample in self.samples[start_index:]]


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def case_record(case):
    steps = [
        {
            "id": f"{case.id}.{index}",
            "name": step.name,
            "phase": "prerequisite" if index <= len(case.prerequisites) else "test",
            "inputs": [
                {"function_code": fc, "offset": OFFSET, "value": value}
                for fc, value in step.inputs
            ],
            "observed_inputs": [],
            "expected_alarm": step.alarm,
            "mode": "hold" if step.hold else "reach",
            "status": "BLOCKED",
            "samples": [],
        }
        for index, step in enumerate(case.prerequisites + case.steps, 1)
    ]
    return {"id": case.id, "name": case.name, "status": "BLOCKED", "steps": steps}


async def run_case(lab, case, record):
    record["status"] = "INCOMPLETE"
    case_status = "PASS"
    for step, result in zip(case.prerequisites + case.steps, record["steps"]):
        result["status"] = "INCOMPLETE"
        await lab.run_step(step, result)
        if result["status"] == "PASS":
            continue

        case_status = result["status"]
        if case_status == "FAIL" and result["phase"] == "prerequisite":
            case_status = "BLOCKED"
        reason = f"{result['id']}: {result['reason']}"
        if case_status == "INCOMPLETE":
            record["reason"] = reason
        else:
            record.setdefault("reason", reason)
        if case_status != "FAIL" or not case.continue_after_failure:
            break

    record["status"] = case_status
    return case_status


async def main(fixture="A"):
    lab = Lab(fixture)
    label = f"[Fixture {fixture}]"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{fixture}-{timestamp}-{uuid4().hex[:8]}"
    run_dir = Path("runs") / run_id
    run_dir.mkdir(parents=True)
    results = [case_record(case) for case in CASES]
    report = {
        "schema_version": 2, "run_id": run_id, "fixture": fixture,
        "fixture_selection": "operator", "started_at": utc_now(),
        "pymodbus_version": pymodbus.__version__,
        "modbus": {"host": HOST, "port": PORT, "unit": UNIT,
                   "level_stimulus_offset": OFFSET,
                   "expected_level_read_offset": lab.level_offset},
        "timing": {"response_timeout_s": TIMEOUT, "hold_s": HOLD_WINDOW,
                   "plc_scan_s": PLC_SCAN, "min_alarm_samples": MIN_SAMPLES},
        "status": "INCOMPLETE", "cases": results,
    }

    server = None
    current = None
    try:
        server = ModbusTcpServer(
            ModbusServerContext(devices={UNIT: lab.device}, single=False),
            address=(HOST, PORT),
            trace_pdu=lab.trace,
        )
        await server.serve_forever(background=True)
        print(f"{label} Modbus I/O siap di {HOST}:{PORT}.")
        await asyncio.to_thread(input, f"{label} Jalankan proyek di Runtime, lalu Enter: ")

        for case, current in zip(CASES, results):
            status = await run_case(lab, case, current)
            print(f"{label} {status} {case.id}: {current.get('reason', case.name)}")
            if status == "INCOMPLETE":
                report["reason"] = current["reason"]
                return 2

        if any(case["status"] != "PASS" for case in results):
            report["status"] = "FAIL"
            return 1

        report["status"] = "PASS"
        return 0
    except (Exception, asyncio.CancelledError) as exc:
        reason = str(exc) or type(exc).__name__
        report["reason"] = reason
        if current and current["status"] == "INCOMPLETE":
            current["reason"] = reason
        print(f"{label} INCOMPLETE: {reason}")
        return 2
    finally:
        report["finished_at"] = utc_now()
        report["summary"] = {
            status: sum(case["status"] == status for case in results)
            for status in ("PASS", "FAIL", "BLOCKED", "INCOMPLETE")
        }
        for case in results:
            if case["status"] == "BLOCKED":
                case.setdefault("reason", "Suite berhenti sebelum skenario ini dijalankan.")
        report_path = run_dir / "report.json"
        temporary = report_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        temporary.replace(report_path)
        counts = ", ".join(f"{status}={count}" for status, count in report["summary"].items())
        print(f"{label} Hasil: {report['status']} ({counts})")
        print(f"{label} Laporan: {report_path}")
        if server is not None:
            await server.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", choices=("A", "B", "C"), default="A",
                        help="Fixture lab yang dijalankan operator (default A).")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.fixture)))
