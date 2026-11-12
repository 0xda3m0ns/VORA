import asyncio

from pymodbus.datastore import (
        ModbusDeviceContext,
        ModbusSequentialDataBlock,
        ModbusServerContext,
        )
from pymodbus.server import ModbusTcpServer

RESPONSE_TIMEOUT = 5.0


async def main():
    device = ModbusDeviceContext(
            ir=ModbusSequentialDataBlock(1, [75, 75]),
            di=ModbusSequentialDataBlock(1, [False]),
            co=ModbusSequentialDataBlock(1, [False]),
            )
    loop = asyncio.get_running_loop()
    alarm_sample = None

    last_alarm_off_at = float("-inf")
    received_functions = set()
    
    samples = {}

    def trace_pdu(sending, pdu):
        nonlocal alarm_sample, last_alarm_off_at
        if not sending:
            received_functions.add(pdu.function_code)
        print(
            "TX" if sending else "RX",
            f"FC={pdu.function_code} unit={pdu.dev_id} address={pdu.address}",
            f"bits={pdu.bits} registers={pdu.registers}",
            f"exception={getattr(pdu, 'exception_code', None)}",
            flush=True,
        )

        if sending and pdu.function_code == 5 and pdu.dev_id == 1 and pdu.address == 0:
            alarm_sample = (loop.time(), bool(device.getValues(1, 0, 1)[0]))
            if not alarm_sample[1]:
                last_alarm_off_at = alarm_sample[0]

        if sending and not pdu.isError():
            fc = pdu.function_code
            if fc == 4 and pdu.registers:
                samples[fc] = (loop.time(), pdu.registers[0])
            elif fc in (2, 5) and pdu.bits:
                samples[fc] = (loop.time(), pdu.bits[0])

        return pdu

    async def wait_sample(fc, expected, after, timeout=3.0):
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            sample = samples.get(fc)
            if sample and sample[0] > after and sample[1] == expected:
                return sample[0]
            await asyncio.sleep(0.05)
        raise SystemExit(f"FAIL: FC{fc} belum mengembalikan {expected!r}")

    async def wait_for_alarm(expected, since):
        deadline = loop.time() + RESPONSE_TIMEOUT
        while True:
            if alarm_sample and alarm_sample[0] > since and alarm_sample[1] == expected:
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.05)

    server = ModbusTcpServer(
            ModbusServerContext(devices={1: device}, single=False),
            address=("127.0.0.1", 1502),
            trace_pdu=trace_pdu,
            )

    await server.serve_forever(background=True)
    try:
        print("Modbus I/O siap di 127.0.0.1:1502, unit=1, level=75%", flush=True)
        await asyncio.to_thread(input, "Start PLC di Editor, lalu tekan enter: ")

        await wait_sample(4, 75, float("-inf"))
        await wait_sample(2, False, float("-inf"))

        changed_at = loop.time()
        device.setValues(2, 0, [True])
        reset_read_at = await wait_sample(2, True, changed_at)
        await wait_sample(5, False, reset_read_at)

        changed_at = loop.time()
        device.setValues(2, 0, [False])
        await wait_sample(2, False, changed_at)

        print("Menunggu keluaran OFF dari PLC pada 75%...", flush=True)

        if not await wait_for_alarm(False, float("-inf")):
            if not received_functions:
                raise SystemExit(
                    "Tidak ada request Modbus dari PLC. Periksa Remote Device "
                    "VORA_Virtual_IO: 127.0.0.1:1502, unit=1, dan dukungan "
                    "Remote I/O pada versi/target simulator yang digunakan."
                )
            if alarm_sample is None:
                raise SystemExit(
                    f"Request diterima: FC={sorted(received_functions)}. "
                    "Belum ada write FC5 yang berhasil ke coil 0, unit=1. "
                    "Periksa mapping TankAlarm -> %QX0.0 dan exception di log TX."
                )
            raise SystemExit(
                "PLC mengirim ON pada level 75%. ALARM memakai latch: "
                "restart program PLC atau beri pulsa RESET saat level <80%, "
                "lalu ulangi smoke test."
            )

        await wait_sample(4, 75, float("-inf"), timeout=RESPONSE_TIMEOUT)

        changed_at = loop.time()
        device.setValues(4, 0, [80])
        level_read_at = await wait_sample(4, 80, changed_at)
        await wait_sample(5, True, level_read_at)
        print("PASS: alarm menyala tepat pada 80%")

        started_at = loop.time()
        device.setValues(4, 0, [85])
        print("Level dinaikkan ke 85%; menunggu keluaran ON...", flush=True)

        level_read_at = await wait_sample(4, 85, started_at, timeout=RESPONSE_TIMEOUT)


        if await wait_for_alarm(True, level_read_at):
            print("PASS: alarm tetap ON pada 85%")
            
            changed_at = loop.time()
            device.setValues(4, 0, [95])
            level_read_at = await wait_sample(4, 95, changed_at)
            await wait_sample(5, True, level_read_at)

            reset_started_at = loop.time()
            device.setValues(2, 0, [True])
            await wait_sample(2, True, reset_started_at)
            await asyncio.sleep(0.6)
            await wait_sample(5, True, loop.time())

            released_at = loop.time()
            device.setValues(2, 0, [False])
            reset_read_at = await wait_sample(2, False, released_at)
            await wait_sample(5, True, reset_read_at)

            if last_alarm_off_at >= reset_started_at:
                raise SystemExit("FAIL: PLC sempat menulis alarm OFF saat uji reset pada 95%")

            print("PASS: alarm tetap ON saat reset ditekan dan dilepas pada 95%")

            changed_at = loop.time()
            device.setValues(4, 0, [75])
            await wait_sample(4, 75, changed_at)
            await asyncio.sleep(0.6)
            await wait_sample(5, True, loop.time())
            print("PASS: alarm masih ON setelah level turun ke 75%")

            changed_at = loop.time()
            device.setValues(2, 0, [True])
            reset_read_at = await wait_sample(2, True, changed_at)
            await wait_sample(5, False, reset_read_at)
            print("PASS: reset pada 75% memadamkan alarm")
            
            changed_at = loop.time()
            device.setValues(2, 0, [False])
            release_read_at = await wait_sample(2, False, changed_at)
            await wait_sample(5, False, release_read_at)
            print("PASS: alarm tetap OFF setelah reset dilepas")

            return

        raise SystemExit("Belum menerima keluaran ON. Periksa log dan mapping PLC.")
    finally:
        await server.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
