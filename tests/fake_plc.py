#!/usr/bin/env python3
"""PLC virtual untuk menguji runner tanpa OpenPLC/Docker.

Skrip ini adalah *test harness*, bukan pengganti OpenPLC Runtime. Ia meniru
perilaku program pada fixture A/B/C: membaca level dan reset dari server
Modbus runner, menjalankan logika latch yang sama, lalu menulis coil alarm.

Contoh:
    python tests/fake_plc.py --fixture A
"""

from __future__ import annotations

import argparse
import logging
import time

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", choices=("A", "B", "C"), default="A")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1502)
    parser.add_argument("--unit", type=int, default=1)
    parser.add_argument(
        "--interval", type=float, default=0.02, help="Jeda polling dalam detik."
    )
    parser.add_argument(
        "--fault",
        choices=("none", "reset-glitch"),
        default="none",
        help="Suntikkan kesalahan untuk menguji deteksi runner.",
    )
    args = parser.parse_args()

    # Jangan tampilkan retry/dump frame internal PyModbus. Pesan dari harness
    # di bawah sudah cukup untuk menjelaskan status koneksi.
    pymodbus_logger = logging.getLogger("pymodbus")
    pymodbus_logger.setLevel(logging.CRITICAL + 1)
    pymodbus_logger.propagate = False

    level_offset = 1 if args.fixture == "B" else 0
    threshold = 90 if args.fixture == "C" else 80

    client = ModbusTcpClient(args.host, port=args.port)
    latched = False
    previous_reset = False
    waiting = False
    ever_connected = False
    print(
        f"Fake PLC {args.fixture}: level offset {level_offset}, "
        f"ambang {threshold}%, target {args.host}:{args.port} unit {args.unit}",
        flush=True,
    )

    try:
        while True:
            if not client.connected:
                if ever_connected:
                    print("Koneksi ke runner terputus; keluar.", flush=True)
                    break
                try:
                    connected = client.connect()
                except (OSError, ModbusException):
                    connected = False
                if not connected:
                    if not waiting:
                        print(
                            f"Menunggu runner di {args.host}:{args.port} ...",
                            flush=True,
                        )
                        waiting = True
                    time.sleep(0.2)
                    continue
                waiting = False
                ever_connected = True
                print(f"Terhubung ke runner {args.host}:{args.port}.", flush=True)

            try:
                level_response = client.read_input_registers(
                    level_offset,
                    count=1,
                    device_id=args.unit,
                )
                reset_response = client.read_discrete_inputs(
                    0,
                    count=1,
                    device_id=args.unit,
                )
                if level_response.isError() or reset_response.isError():
                    print("Respons Modbus error; keluar.", flush=True)
                    break

                level = level_response.registers[0]
                reset = bool(reset_response.bits[0])

                # Fault demo: alarm sempat padam satu siklus saat reset ditekan
                # di level tinggi, lalu menyala lagi. Runner harus menangkapnya.
                glitch = (
                    args.fault == "reset-glitch"
                    and reset
                    and not previous_reset
                    and level >= threshold
                )

                if level >= threshold:
                    latched = True
                elif reset and not previous_reset:
                    latched = False
                previous_reset = reset

                client.write_coil(0, False if glitch else latched, device_id=args.unit)
            except (ConnectionResetError, OSError, ModbusException) as exc:
                print(
                    f"Koneksi ke runner terputus ({type(exc).__name__}); keluar.",
                    flush=True,
                )
                break

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Dihentikan.", flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
