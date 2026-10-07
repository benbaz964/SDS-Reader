# PyInstaller build spec for the SDS Reader desktop app.
#
#   pyinstaller --noconfirm --clean build/sds_reader.spec
#
# Produces two variants from one analysis:
#   dist/SDS-Reader.exe          single self-contained file (simplest to download)
#   dist/SDS-Reader/             portable folder (zip it) - for PCs whose policy
#                                blocks programs that unpack themselves into %TEMP%
# Neither needs Python installed on the target machine.

import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ICON = os.path.join(ROOT, "build", "icon.ico")

a = Analysis(
    [os.path.join(ROOT, "sds_reader_app.py")],
    pathex=[ROOT],
    datas=[
        (os.path.join(ROOT, "sds_reader", "templates"), os.path.join("sds_reader", "templates")),
        (ICON, "build"),
    ],
    hiddenimports=[],
    excludes=["matplotlib", "numpy", "pandas", "scipy", "IPython", "pytest", "unittest", "pydoc_data"],
    noarchive=False,
)
pyz = PYZ(a.pure)

common = dict(
    debug=False,
    strip=False,
    upx=False,          # UPX-packed exes trip antivirus far more often
    console=False,
    icon=ICON,
    version=os.path.join(ROOT, "build", "version_info.txt"),
)

onefile = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="SDS-Reader",
              runtime_tmpdir=None, **common)

onedir_exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="SDS-Reader", **common)
COLLECT(onedir_exe, a.binaries, a.datas, strip=False, upx=False, name="SDS-Reader")
