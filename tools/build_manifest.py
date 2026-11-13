#!/usr/bin/env python3
"""Bangkitkan manifest.json dari paket fixture dan skenario uji.

Jalankan ulang skrip ini setiap kali berkas di fixtures/ atau suites/ berubah:

    python tools/build_manifest.py

Runner membandingkan SHA-256 berkas yang dipakai dengan nilai di manifest.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
SUITE = ROOT / "suites" / "tank-alarm-v1.yaml"

ARTIFACTS = (
    ("logic", "pous/programs/main.st"),
    ("io_config", "devices/remote/VORA_Virtual_IO.json"),
    ("project", "project.json"),
)

DESCRIPTIONS = {
    "A": "Baseline: ambang 80%, level dibaca dari register 0.",
    "B": "Pemetaan keliru: logika sama dengan A, level dibaca dari register 1 (tetap 75%).",
    "C": "Ambang 90%: checksum paket cocok, tetapi aktivasi di 80-85% gagal.",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    if not SUITE.is_file():
        raise SystemExit(f"Suite tidak ditemukan: {SUITE}")

    fixtures: dict[str, dict] = {}
    for name, description in DESCRIPTIONS.items():
        base = FIXTURES / name
        if not base.is_dir():
            raise SystemExit(f"Fixture tidak ditemukan: {base}")
        artifacts = []
        for kind, relative in ARTIFACTS:
            path = base / relative
            if not path.is_file():
                raise SystemExit(f"Artefak tidak ditemukan: {path}")
            artifacts.append({
                "kind": kind,
                "path": str(path.relative_to(ROOT)),
                "sha256": sha256_file(path),
            })
        fixtures[name] = {
            "description": description,
            "artifacts": artifacts,
        }

    manifest = {
        "schema_version": 1,
        "generated_by": "tools/build_manifest.py",
        "suite": {
            "path": str(SUITE.relative_to(ROOT)),
            "sha256": sha256_file(SUITE),
        },
        "fixtures": fixtures,
    }
    output = ROOT / "manifest.json"
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")

    logic_a = next(
        item["sha256"] for item in fixtures["A"]["artifacts"] if item["kind"] == "logic"
    )
    logic_b = next(
        item["sha256"] for item in fixtures["B"]["artifacts"] if item["kind"] == "logic"
    )
    print(f"Manifest ditulis ke {output}")
    print(f"Hash logika A == B: {logic_a == logic_b}")


if __name__ == "__main__":
    main()
