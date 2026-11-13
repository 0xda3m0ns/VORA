# Paket fixture VORA

Setiap folder adalah paket proyek OpenPLC untuk satu kondisi pemulihan:

| Folder | Logika `main.st` | Offset `TankLevel` (FC04) | Perilaku |
|---|---|---:|---|
| `A` | `LEVEL_PCT >= 80` | `0` | Baseline sesuai SOP |
| `B` | `LEVEL_PCT >= 80` (byte-identik dengan A) | `1` | Membaca register cadangan yang tetap 75% |
| `C` | `LEVEL_PCT >= 90` | `0` | Ambang berubah; aktivasi 80–85% gagal |

Isi tiap folder:

```text
<fixture>/
├── project.json
├── pous/programs/main.st
├── devices/configuration.json
├── devices/pin-mapping.json
└── devices/remote/VORA_Virtual_IO.json
```

`main.st` pada A dan B sengaja dibuat identik. Bukti hash-nya ada di
`manifest.json`: `logic_sha256` A dan B sama, sedangkan `io_config_sha256`
berbeda. Inilah inti demonstrasi B — checksum logika cocok, tetapi pemetaan
I/O berubah sehingga perilaku alarm salah.

## Memakai paket

1. Buka folder fixture yang sesuai di OpenPLC Editor, lalu **Build & Upload**
   ke Runtime dan pastikan status `RUNNING`.
2. Jalankan runner dengan flag yang sama:

   ```bash
   python runner.py --fixture A
   ```

3. Setelah mengubah berkas apa pun di folder ini atau di `suites/`, jalankan
   ulang pembuat manifest:

   ```bash
   python tools/build_manifest.py
   ```

Runner akan menolak melanjutkan bila hash berkas tidak cocok dengan manifest.
