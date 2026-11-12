# VORA — Verification of Restored Automation

![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Docker Compose](https://img.shields.io/badge/Docker-Compose-2496ED?style=for-the-badge&logo=docker&logoColor=white)
![Linux](https://img.shields.io/badge/Linux-Lab-FCC624?style=for-the-badge&logo=linux&logoColor=black)
![OpenPLC](https://img.shields.io/badge/OpenPLC-Editor%20%2B%20Runtime-184E77?style=for-the-badge)
![Modbus TCP](https://img.shields.io/badge/Modbus-TCP-286A80?style=for-the-badge)
![Structured Text](https://img.shields.io/badge/IEC%2061131--3-Structured%20Text-57606A?style=for-the-badge)

Dalam sistem otomasi industri, pemulihan program PLC dari backup merupakan salah satu tahapan pemulihan setelah insiden siber. Namun, proses tersebut perlu dilengkapi dengan verifikasi fungsional untuk memastikan bahwa logika kontrol dan alarm masih beroperasi sesuai spesifikasi. VORA (Verification of Restored Automation) menyediakan mekanisme pengujian otomatis berbasis simulasi dengan memberikan input berupa perubahan level tangki dan sinyal reset. Respons PLC kemudian diamati dan dievaluasi berdasarkan kondisi yang diharapkan pada setiap skenario pengujian. Hasil evaluasi disusun dalam laporan terstruktur yang memuat status pengujian serta respons alarm sebagai bukti verifikasi fungsional.

## Cara kerja

Runner Python menyediakan level tangki dan tombol reset virtual melalui server Modbus TCP di `127.0.0.1:1502`. OpenPLC Runtime membaca kedua input tersebut, menjalankan program PLC, lalu menulis status alarm kembali ke runner.

```text
Runner Python                       OpenPLC Runtime
(server Modbus TCP)                  (klien Modbus)
─────────────────────────           ─────────────────────────
Level tangki virtual    ── FC04 →   Baca level
Tombol reset virtual    ── FC02 →   Baca reset
Status alarm            ←─ FC05 ─   Jalankan logika alarm
Catat sampel dan hasil
```

Setelah teknisi mengunggah proyek PLC melalui OpenPLC Editor dan program berstatus `RUNNING`, runner menguji kondisi normal, aktivasi alarm, penahanan alarm setelah level turun, reset saat level tinggi, dan reset saat level rendah. Setiap langkah dicatat dalam laporan JSON.

Pengujian menggunakan tiga *fixture* yang dimuat bergantian ke Runtime yang sama:

| Fixture | Konfigurasi PLC | Yang diuji |
|---|---|---|
| A — Baseline | Level dibaca dari offset `0`; alarm aktif mulai `80%` | Respons yang diharapkan |
| B — Pemetaan sensor berbeda | Level dibaca dari offset `1`, sedangkan runner mengubah nilai di offset `0` | Kesalahan pemetaan input |
| C — Ambang berbeda | Level dibaca dari offset `0`; alarm baru aktif mulai `90%` | Perubahan logika alarm |

Flag `--fixture` pada runner memilih ekspektasi uji dan label laporan. Proyek PLC yang sesuai tetap harus diunggah melalui Editor; runner tidak mengganti program yang sedang berjalan.

VORA menguji fungsi alarm dalam simulasi. Pemulihan program dari backup dan pemeriksaan keamanan dilakukan terpisah oleh tim teknis.

## Tech stack

| Komponen | Teknologi | Peran |
|---|---|---|
| Program PLC | IEC 61131-3 Structured Text; OpenPLC Editor 4.3.4 | Logika alarm dan pemetaan I/O |
| Runtime PLC | OpenPLC Runtime v4.3.2 | Menjalankan proyek yang diunggah dari Editor |
| Runner | Python 3.12, `asyncio`, PyModbus 3.11.4 | Menyediakan I/O virtual dan memeriksa respons PLC |
| Komunikasi | Modbus TCP; FC02, FC04, FC05 | Membaca reset dan level, serta menulis status alarm |
| Lingkungan | Linux, Docker Engine dan Compose, uv | Menjalankan Runtime dan menyiapkan Python |
| Laporan | JSON | Menyimpan hasil dan sampel komunikasi tiap langkah |

Panduan ini menggunakan satu mesin Linux untuk Editor, Runtime Docker, dan runner. Pengujian dilakukan dengan perangkat virtual, tanpa sensor atau aktuator fisik.

## Dependensi

| Dependensi | Versi atau kebutuhan |
|---|---|
| OpenPLC Editor | **4.3.4**; gunakan AppImage Linux x64 pada mesin x86-64. [Rilis resmi](https://github.com/Autonomy-Logic/openplc-editor/releases/tag/v4.3.4) |
| OpenPLC Runtime | Image **v4.3.2**. [Registry resmi](https://github.com/orgs/Autonomy-Logic/packages/container/package/openplc-runtime) |
| Docker Engine dan Compose | Docker Engine berjalan dan perintah `docker compose version` tersedia. [Instalasi Docker Engine](https://docs.docker.com/engine/install/) |
| Python dan uv | Panduan memakai **Python 3.12**, yang dapat disiapkan lewat uv. [Instalasi uv](https://docs.astral.sh/uv/getting-started/installation/) |
| PyModbus | **3.11.4**; perintah runner di bawah memasangnya lewat uv. [Menjalankan skrip dengan uv](https://docs.astral.sh/uv/guides/scripts/) |
| Runner | [runner.py](runner.py) di folder VORA, dengan dukungan `--fixture A`, `B`, dan `C` |

Runtime v4.3.2 pada setup ini meminta Editor 4.3.3 atau lebih baru. Jika Editor menampilkan pesan versi saat terhubung, periksa versi AppImage yang sedang terbuka.

## 1. Siapkan Runtime dan Python

Berkas [compose.yaml](compose.yaml) di folder VORA berisi konfigurasi berikut:

```yaml
services:
  vora-openplc:
    image: ghcr.io/autonomy-logic/openplc-runtime:v4.3.2
    network_mode: host
    volumes:
      - vora-plc:/var/run/runtime

volumes:
  vora-plc:
    external: true
    name: vora-plc
```

Jalankan perintah berikut dari folder VORA. Pada mesin baru, volume eksternal perlu dibuat sekali. Lihat [dokumentasi volume Docker Compose](https://docs.docker.com/reference/compose-file/volumes/).

```bash
docker volume create vora-plc
docker compose config -q
docker compose up -d
docker compose ps
uv python install 3.12
```

Tunggu hingga service `vora-openplc` berjalan. Jika port `8443` sudah dipakai container OpenPLC lain, hentikan container tersebut sebelum menjalankan service Compose. Konfigurasi `network_mode: host` memungkinkan Runtime mengakses runner melalui loopback mesin Linux. Lihat [dokumentasi Docker host networking](https://docs.docker.com/engine/network/drivers/host/).

| Jalur | Alamat |
|---|---|
| OpenPLC Editor → Runtime | `127.0.0.1:8443` |
| OpenPLC Runtime → runner Modbus TCP | `127.0.0.1:1502` |

## 2. Buat fixture A di OpenPLC Editor

**Catatan reproduksi:** Berkas proyek OpenPLC di root repo bukan fixture A siap pakai. Saat ini `devices/remote/VORA_Virtual_IO.json` memakai offset level `1` seperti B, sementara `pous/programs/main.st` memakai ambang `>= 90` seperti C. Untuk mereproduksi hasil pada tabel, buat `VORA_A` mengikuti bagian ini, lalu salin dan ubah menjadi `VORA_B` dan `VORA_C` sesuai bagian 3. Saat menjalankan pengujian, unggah proyek yang cocok dengan pilihan `--fixture`.

### Buat proyek dan pilih target

1. Unduh AppImage dari tautan rilis di atas. Di terminal, beri izin eksekusi dengan `chmod +x ./nama-file.AppImage`, lalu jalankan `./nama-file.AppImage`. Ganti `nama-file.AppImage` dengan nama file yang diunduh. Buat **PLC Project** bernama `VORA_A`.
2. Buat program **Structured Text (ST)** bernama `main`. Atur task cyclic ke **`T#20ms`**. Jika task dan program instance dibuat terpisah, tugaskan instance `main` ke task tersebut.
3. Di **Device → Configuration → Board Settings**, pilih **OpenPLC Runtime v4**, isi IP `127.0.0.1`, lalu klik **Connect**. Ikuti dialog akun Runtime jika muncul. Status `EMPTY` pada container baru berarti belum ada proyek yang diunggah. Lihat [integrasi Editor dan Runtime](https://github.com/Autonomy-Logic/openplc-runtime/blob/main/docs/EDITOR_INTEGRATION.md).

### Atur Modbus Remote Device

Klik **`+` biru** di *project tree*, pilih **Remote Device → Modbus**, lalu beri nama `VORA_Virtual_IO`. Pilih **TCP/IP** dengan IP **`127.0.0.1`**, port **`1502`**, **Slave ID `1`**, dan **Timeout `1000 ms`**. Lihat [konfigurasi Modbus Remote Device](https://edge.autonomylogic.com/docs/openplc-editor/communication/modbus/client/).

Klik **`+`** di bagian **IO Tag Mapping** dan tambahkan tiga grup berikut. Gunakan **Cycle Time `200 ms`** pada setiap grup yang menyediakan kolom tersebut.

| Nama grup | Function Code | Offset remote | Length | Address IEC pada setup lab |
|---|---|---:|---:|---|
| `TankLevel` | **FC04** — Read Input Registers | `0` | `1` | `%IW0` |
| `TankReset` | **FC02** — Read Discrete Inputs | `0` | `1` | `%IX0.0` |
| `TankAlarm` | **FC05** — Write Single Coil | `0` | `1` | `%QX0.0` |

**Offset** adalah alamat register atau bit pada server Modbus Python. **Address** adalah alamat I/O lokal yang diberikan Editor. Buka rincian setiap grup untuk melihat alamat tag, lalu gunakan alamat tersebut pada kolom **Location** di program.

### Isi variabel dan logika alarm

Di tabel **Variables** pada `main`, deklarasikan semua variabel sebagai **Local**.

| Name | Type | Location | Initial Value |
|---|---|---|---|
| `LEVEL_PCT` | `INT` | Address dari `TankLevel`, biasanya `%IW0` | Kosong |
| `RESET` | `BOOL` | Address dari `TankReset`, biasanya `%IX0.0` | Kosong |
| `ALARM` | `BOOL` | Address dari `TankAlarm`, biasanya `%QX0.0` | Kosong |
| `PREV_RESET` | `BOOL` | Kosong | `FALSE` |
| `LATCHED` | `BOOL` | Kosong | `FALSE` |

Masukkan kode berikut di area **kode ST** pada `main`, lalu simpan.

```iecst
IF LEVEL_PCT >= 80 THEN
    LATCHED := TRUE;
ELSIF RESET AND NOT PREV_RESET THEN
    LATCHED := FALSE;
END_IF;

PREV_RESET := RESET;
ALARM := LATCHED;
```

Alarm menyala saat level mencapai `80%` dan tetap menyala ketika level turun. Pulsa reset memadamkannya jika level sudah di bawah ambang. Saat level masih tinggi, kondisi aktivasi membuat alarm tetap menyala.

## 3. Siapkan fixture B dan C

Buat dua salinan proyek A dengan nama `VORA_B` dan `VORA_C`. Simpan di folder proyek terpisah agar A dapat diunggah kembali.

| Pengaturan | A — Baseline | B — Pemetaan sensor berbeda | C — Ambang berbeda |
|---|---:|---:|---:|
| `TankLevel` → **Offset** FC04 | `0` | **`1`** | `0` |
| Ambang di kode ST | `>= 80` | `>= 80` | **`>= 90`** |
| `TankReset` FC02 dan `TankAlarm` FC05 | Offset `0` | Offset `0` | Offset `0` |

Untuk **B**, edit baris grup `TankLevel` di **Remote Devices → VORA_Virtual_IO → IO Tag Mapping**. Atur Offset ke `1`, Function Code ke FC04, dan Length ke `1`. Pastikan **Location** `LEVEL_PCT` masih cocok dengan **Address** grup, yang dapat tetap `%IW0`. Runner memberikan stimulus di offset `0` dan menyimpan nilai tetap `75` di offset `1`.

Untuk **C**, pertahankan FC04 offset `0`. Di `main`, ganti kondisi menjadi `IF LEVEL_PCT >= 90 THEN`. PLC akan membaca level `80` dan `85`, tetapi alarm tetap mati pada kedua nilai tersebut. Pada `95%`, alarm dapat menyala untuk uji penahanan dan reset.

Sebelum mengunggah proyek, periksa offset `TankLevel` dan ambang di kode ST. Keduanya harus sesuai dengan fixture yang akan diuji.

## 4. Jalankan tiap fixture

1. Buka proyek yang akan diuji di Editor dan pilih target **OpenPLC Runtime v4**. Lepas semua **Force** dari debugger.
2. Hentikan program PLC yang sedang berjalan melalui Editor. Biarkan container Runtime tetap hidup.
3. Dari terminal di folder VORA, jalankan **satu** perintah sesuai proyek yang dibuka. Hentikan `smoke.py` terlebih dahulu jika masih memakai port `1502`.

   ```bash
   uv run --no-project --python 3.12 --with "pymodbus==3.11.4" python runner.py --fixture A
   uv run --no-project --python 3.12 --with "pymodbus==3.11.4" python runner.py --fixture B
   uv run --no-project --python 3.12 --with "pymodbus==3.11.4" python runner.py --fixture C
   ```

4. Saat runner menunggu Enter, lakukan **Build & Upload** pada proyek yang sesuai dari Editor. Jika status setelah upload `STOPPED`, klik **Start**.
5. Setelah PLC berstatus **RUNNING**, tekan Enter di terminal. Ulangi langkah-langkah ini untuk fixture berikutnya.

Runner mulai menguji setelah Enter ditekan. Pastikan proyek yang berjalan sesuai dengan flag `--fixture` pada perintah.

## 5. Baca hasil uji

Dengan konfigurasi proyek yang dijelaskan di atas, hasil demonstrasi yang diharapkan adalah:

| Skenario | A | B | C |
|---|---|---|---|
| S01 · Kondisi normal setelah reset persiapan | PASS | PASS | PASS |
| S02 · Aktivasi alarm di 80% dan 85% | PASS | FAIL | FAIL |
| S03 · Alarm tertahan setelah level turun | PASS | BLOCKED | PASS |
| S04 · Reset di 95% | PASS | BLOCKED | PASS |
| S05 · Reset di 75% | PASS | BLOCKED | PASS |
| **Ringkasan** | **5 PASS** | **1 PASS, 1 FAIL, 3 BLOCKED** | **4 PASS, 1 FAIL** |

Pada **B**, PLC membaca offset `1` yang tetap bernilai `75`, bukan nilai stimulus di offset `0`. S02 gagal, dan alarm juga tidak dapat diaktifkan pada persiapan `95%`; karena itu S03–S05 berstatus `BLOCKED`.

Pada **C**, PLC membaca nilai `80` dan `85` dari offset `0`, tetapi alarm tetap mati karena ambangnya `90`. S02 gagal. Alarm dapat menyala pada `95%`, sehingga S03–S05 tetap diuji.

Terminal menampilkan label seperti `[Fixture B]` dan lokasi laporan. Setiap run menulis `runs/<fixture>-<timestamp>-<id>/report.json`. Untuk menelusuri S02, periksa `inputs` (stimulus), `observed_inputs` (offset dan nilai yang dibaca PLC), dan `samples` (jejak komunikasi, termasuk alarm FC05).

| Status | Arti |
|---|---|
| `PASS` | Respons memenuhi hasil yang diharapkan |
| `FAIL` | Respons berbeda dari hasil yang diharapkan |
| `BLOCKED` | Prasyarat skenario gagal |
| `INCOMPLETE` | Pengamatan yang diperlukan tidak terkumpul atau eksekusi terhenti |

Runner keluar dengan kode `0` jika semua skenario lulus, `1` jika ada kegagalan fungsi, dan `2` jika eksekusi tidak selesai. Kode `1` pada B dan C sesuai dengan hasil demonstrasi.

## Troubleshooting

| Gejala | Yang perlu diperiksa |
|---|---|
| Editor menunjukkan `PLC: EMPTY` atau log `No libplc_*.so file found` | Lakukan **Build & Upload** pada proyek yang akan diuji. Jika status setelah upload `STOPPED`, klik **Start**. |
| Runner gagal memakai port `1502` | Hentikan `smoke.py` atau runner lain yang masih berjalan. |
| `INCOMPLETE` karena belum ada pembacaan FC04 | Pastikan PLC `RUNNING`, target **Runtime v4**, dan Remote Device mengarah ke `127.0.0.1:1502` dengan Slave ID `1`. Periksa juga proyek yang diunggah. |
| B mencatat offset `0` | Periksa **Offset pada baris grup `TankLevel`** di proyek B, simpan, lalu unggah ulang. |
| C mendapat 5 `PASS` dan alarm menyala di `80%` | Periksa bahwa kode ST proyek C memakai `>= 90`, lepas Force, lalu unggah ulang. |
| Hasil tidak sesuai dengan fixture yang dipilih | Periksa offset `TankLevel`, ambang di kode ST, dan proyek yang sedang berjalan di Runtime. |
| `volumes must be a mapping` | Pastikan `volumes:` bagian bawah sejajar dengan `services:` dan `vora-plc:` menjorok di bawahnya. Jalankan `docker compose config -q`. |

Untuk melihat log Runtime, jalankan `docker compose logs --tail 50 vora-openplc` dari folder VORA.
