#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Costruisce l'installer winget di Ugo: ugo.exe + zip + manifest.

Uso:
    python packaging/make_winget_zip.py [--version X.Y.Z] [--tag vX.Y.Z]
                                        [--url ALTERNATIVA] [--force]

Cosa fa:
  1. compila boot/ugo_bootstrap.cs in dist/ugo/ugo.exe con il csc.exe del
     .NET Framework (presente di serie su Windows; serve solo per compilare,
     l'exe girato non richiede nulla oltre Windows stesso);
  2. crea dist/ugo-<ver>-winget.zip (ugo.exe), zip DETERMINISTICO
     (timestamp fissi) perche' l'hash sia riproducibile;
  3. calcola lo SHA256 e aggiorna i manifest in packaging/winget/
     (PackageVersion, InstallerUrl, InstallerSha256, ReleaseDate);
  4. crea dist/ugo-<ver>-winget-manifests.zip: i 3 manifest pronti da dare
     in pasto a `winget install --manifest`.

Il workflow di release (.github/workflows/release.yml) lancia questo script
a ogni tag v* e allega entrambi gli zip alla GitHub Release, cosi' i manifest
nel repo restano allineati all'ultima release.
"""
import argparse
import glob
import hashlib
import re
import subprocess
import sys
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
PKG = ROOT / "packaging" / "winget"
BOOT_CS = ROOT / "boot" / "ugo_bootstrap.cs"
MANIFESTS = ("Ugo.Agent.yaml", "Ugo.Agent.defaultLocale.yaml", "Ugo.Agent.installer.yaml")
VER_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.M)
# timestamp fisso -> zip e hash riproducibili (indipendenti da quando buildi)
FIXED_TIME = (2026, 1, 1, 0, 0, 0)


def version_from_pyproject() -> str:
    m = VER_RE.search((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    if not m:
        sys.exit("[XX] versione non trovata nel pyproject.toml")
    return m.group(1)


def find_csc() -> str | None:
    pats = [r"C:\Windows\Microsoft.NET\Framework64\v4*\csc.exe",
            r"C:\Windows\Microsoft.NET\Framework\v4*\csc.exe"]
    for pat in pats:
        hits = sorted(glob.glob(pat), reverse=True)
        if hits:
            return hits[0]
    return None


def build_exe(force: bool) -> Path:
    out_dir = DIST / "ugo"
    exe = out_dir / "ugo.exe"
    if exe.exists() and not force:
        print(f"[OK] ugo.exe gia' presente: {exe}")
        return exe
    csc = find_csc()
    if csc is None:
        if exe.exists():
            print("[!!] csc.exe non trovato: riuso l'exe precompilato")
            return exe
        sys.exit("[XX] csc.exe non trovato e nessun dist/ugo/ugo.exe: "
                 "compila boot/ugo_bootstrap.cs su Windows prima di impacchettare")
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.check_call([csc, "/nologo", "/target:winexe", "/platform:anycpu",
                           "/out:" + str(exe), str(BOOT_CS)])
    print(f"[OK] compilato: {exe}")
    return exe


def _add(zf: zipfile.ZipFile, name: str, data: bytes) -> None:
    zi = zipfile.ZipInfo(name, date_time=FIXED_TIME)
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.create_system = 0            # FAT: niente permessi unix nell'zip
    zi.external_attr = 0o644 << 16
    zf.writestr(zi, data)


def build_zip(exe: Path, ver: str) -> Path:
    zip_path = DIST / f"ugo-{ver}-winget.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        _add(zf, "ugo.exe", exe.read_bytes())
        cfg = exe.parent / "ugo.exe.config"
        if cfg.exists():
            _add(zf, "ugo.exe.config", cfg.read_bytes())
    print(f"[OK] zip installer: {zip_path}")
    return zip_path


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def fill_manifests(ver: str, tag: str, sha: str, release_date: str,
                   url_override: str | None) -> None:
    url = url_override or (f"https://github.com/giovannimazza/ugo-local-ai-agent"
                           f"/releases/download/{tag}/ugo-{ver}-winget.zip")
    for name in MANIFESTS:
        p = PKG / name
        t = p.read_text(encoding="utf-8")
        t = re.sub(r"(?m)^PackageVersion:.*$", f"PackageVersion: {ver}", t)
        if name.endswith("installer.yaml"):
            # (\s*) preserva l'indentazione: i campi dentro Installers sono indentati
            t = re.sub(r"(?m)^(\s*)InstallerUrl:.*$", "\\1InstallerUrl: " + url, t)
            t = re.sub(r"(?m)^(\s*)InstallerSha256:.*$", "\\1InstallerSha256: " + sha, t)
            t = re.sub(r"(?m)^(\s*)ReleaseDate:.*$", '\\1ReleaseDate: "' + release_date + '"', t)
        p.write_text(t, encoding="utf-8", newline="\n")
        print(f"[OK] manifest allineato: {p}")


def build_manifest_zip(ver: str) -> Path:
    z = DIST / f"ugo-{ver}-winget-manifests.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for name in MANIFESTS:
            _add(zf, name, (PKG / name).read_bytes())
    print(f"[OK] bundle manifest: {z}")
    return z


def main() -> int:
    ap = argparse.ArgumentParser(description="Build installer winget di Ugo")
    ap.add_argument("--version", help="versione (default: pyproject.toml)")
    ap.add_argument("--tag", help="tag GitHub della release (default: v<versione>)")
    ap.add_argument("--url", help="InstallerUrl alternativa (test locali)")
    ap.add_argument("--force", action="store_true", help="ricompila ugo.exe")
    args = ap.parse_args()

    ver = args.version or version_from_pyproject()
    tag = args.tag or f"v{ver}"
    print(f"==> Ugo winget: versione {ver}, tag {tag}")

    exe = build_exe(args.force)
    zip_path = build_zip(exe, ver)
    sha = sha256_of(zip_path)
    print(f"[OK] SHA256: {sha}")
    fill_manifests(ver, tag, sha, date.today().isoformat(), args.url)
    build_manifest_zip(ver)

    print(f"""
Fatto. Per provare in locale (una tantum):
  winget settings --enable LocalManifestFiles
  winget install --manifest packaging\\winget\\Ugo.Agent.installer.yaml \\
      -e Ugo.Agent --accept-source-agreements
In produzione l'URL dell'installer punta all'asset della GitHub Release {tag}.""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
