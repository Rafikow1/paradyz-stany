"""Pomocnicze kroki workflow: pakowanie wyników zadania, decyzja o drugiej rundzie, odtworzenie raportu.

  python -m utils.pack_result wynik                 # output/ + data/ -> wynik/ (zaszyfrowane)
  python -m utils.pack_result needs-retry <folder>  # wypisuje retry=1, jeśli są produkty do ponowienia
  python -m utils.pack_result restore <folder> <output>
"""
import os
import shutil
import sys
import zipfile
from pathlib import Path

FAILED = {"ZABLOKOWANY", "ZMIANA_UKŁADU_STRONY", "BŁĄD", "BŁĄD_SIECI", "NIE_SPRAWDZONO"}


def pack(out_dir: Path):
    from utils.publish import encrypt_bytes
    pw = os.environ["DATA_PASSWORD"]
    out_dir.mkdir(exist_ok=True)
    for f in Path("output").glob("stany_magazynowe_2*.xlsx"):
        (out_dir / (f.name + ".enc")).write_bytes(encrypt_bytes(f.read_bytes(), pw))
    for name in ("url_map.json", "lm_wspolrzedne_miast.json"):
        if (Path("data") / name).exists():
            shutil.copy(Path("data") / name, out_dir / name)
    with zipfile.ZipFile("dziennik.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for base in (Path("logs"), Path("data/debug")):
            for f in base.rglob("*") if base.exists() else []:
                if f.is_file():
                    z.write(f)
    (out_dir / "dziennik.zip.enc").write_bytes(encrypt_bytes(Path("dziennik.zip").read_bytes(), pw))


def _decrypted_reports(folder: Path):
    import io

    from utils.publish import decrypt_bytes
    for f in sorted(folder.rglob("stany_magazynowe_2*.xlsx.enc")):
        yield f, decrypt_bytes(f.read_bytes(), os.environ["DATA_PASSWORD"])


def needs_retry(folder: Path) -> str:
    import io

    import pandas as pd
    if not folder.exists():
        return "retry=1"            # brak wyników = zadanie padło całkiem
    reps = list(_decrypted_reports(folder))
    if not reps:
        return "retry=1"
    df = pd.read_excel(io.BytesIO(reps[-1][1]), sheet_name="Stany")
    bad = df[df["Status"].isin(FAILED)]["EAN"].nunique()
    print(f"Do ponowienia: {bad} produktów", file=sys.stderr)
    return "retry=1" if bad else "retry=0"


def restore(folder: Path, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    for f, data in _decrypted_reports(folder):
        (out / f.name[:-4]).write_bytes(data)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "wynik":
        pack(Path("wynik"))
    elif cmd == "needs-retry":
        print(needs_retry(Path(sys.argv[2])))
    elif cmd == "restore":
        restore(Path(sys.argv[2]), Path(sys.argv[3]))
