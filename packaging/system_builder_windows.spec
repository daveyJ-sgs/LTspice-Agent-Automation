from pathlib import Path


project_root = Path(SPECPATH).parent
data_files = [
    (project_root / "system_builder_static", "system_builder_static"),
    (project_root / "examples/mixed_signal_daq.ltstudy.json", "examples"),
    (project_root / "examples/mixed_signal_daq.ltopt.json", "examples"),
    (project_root / "examples/mixed_signal_daq_ac.cir", "examples"),
    (project_root / "examples/mixed_signal_daq_transient.cir", "examples"),
    (project_root / "examples/mixed_signal_daq.asc", "examples"),
    (
        project_root / "docs/images/mixed-signal-daq-schematic.png",
        "docs/images",
    ),
]

analysis = Analysis(
    [str(project_root / "system_builder_windows.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[(str(source), destination) for source, destination in data_files],
    hiddenimports=[
        "examples.mixed_signal_daq_study",
        "examples.optimize_mixed_signal_daq",
        "mcp_server",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
python_archive = PYZ(analysis.pure)

executable = EXE(
    python_archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="LTspice-System-Builder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

bundle = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LTspice-System-Builder",
)
