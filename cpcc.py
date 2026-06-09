#!/usr/bin/env python3
DESCRIPTION = """
Generate compile_commands.json from Eclipse .cproject / .project.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

PROGRAM_NAME = "cpcc.py"

ARM_PREFIX = os.environ.get("ARM_PREFIX", "arm-none-eabi-")

CPU_FLAGS = [
    "-mcpu=cortex-m33",
    "-mthumb",
    "-mcmse",
    "-mfloat-abi=hard",
    "-mfpu=fpv5-sp-d16",
]

C_EXTS   = {".c"}
ASM_EXTS = {".s", ".S"}

# Set by main() after --project-dir is resolved.
PROJECT_DIR:  Path
ECLIPSE_DIR:  Path
CPROJECT:     Path
PROJECT_FILE: Path


def _resolve(raw: str) -> str:
    """Expand Eclipse path variables and return a normalised absolute path."""
    s = raw.strip().strip('"')
    s = s.replace("${ProjDirPath}", str(ECLIPSE_DIR))
    s = s.replace("PARENT-1-PROJECT_LOC", str(PROJECT_DIR))
    return str(Path(s).resolve())


def get_version_flags() -> list[str]:
    version_sh   = PROJECT_DIR / "version.sh"
    version_file = PROJECT_DIR / "version"
    try:
        result = subprocess.run(
            ["bash", str(version_sh), str(version_file), "FW_VERSION"],
            capture_output=True, text=True, cwd=str(PROJECT_DIR),
        )
        return result.stdout.strip().split()
    except Exception as exc:
        print(f"[warn] could not run version.sh: {exc}", file=sys.stderr)
        return []


def _find_configuration(root: ET.Element, config_name: str) -> ET.Element | None:
    """Return the <configuration name=config_name> element inside cdtBuildSystem."""
    settings_mod = root.find('storageModule[@moduleId="org.eclipse.cdt.core.settings"]')
    if settings_mod is None:
        return None
    for ccfg in settings_mod:
        bsm = ccfg.find('storageModule[@moduleId="cdtBuildSystem"]')
        if bsm is None:
            continue
        cfg = bsm.find("configuration")
        if cfg is not None and cfg.get("name") == config_name:
            return cfg
    return None


def parse_cproject(config_name: str = "Debug") -> dict:
    """Extract compiler settings from the named Eclipse configuration."""
    tree = ET.parse(CPROJECT)
    root = tree.getroot()

    cfg = _find_configuration(root, config_name)
    if cfg is None:
        sys.exit(f"[error] configuration '{config_name}' not found in .cproject")

    include_paths:     list[str] = []
    defines:           list[str] = []
    other_c_flags:     list[str] = []
    forced_includes:   list[str] = []
    asm_include_paths: list[str] = []
    asm_defines:       list[str] = []

    for tool in cfg.iter("tool"):
        tool_id = tool.get("id", "")

        if "tool.c.compiler" in tool_id:
            for opt in tool.iter("option"):
                oid  = opt.get("id", "")
                vtyp = opt.get("valueType", "")

                if "include.paths" in oid and vtyp == "includePath":
                    for v in opt.iter("listOptionValue"):
                        include_paths.append(_resolve(v.get("value", "")))

                elif "defs" in oid and vtyp == "definedSymbols":
                    for v in opt.iter("listOptionValue"):
                        defines.append(v.get("value", ""))

                elif "other" in oid and vtyp == "string":
                    raw = re.sub(r"\$\(shell[^)]*\)", "", opt.get("value", "")).strip()
                    other_c_flags.extend(raw.split())

                elif "include.files" in oid and vtyp == "includeFiles":
                    for v in opt.iter("listOptionValue"):
                        forced_includes.append(_resolve(v.get("value", "")))

        elif "tool.assembler" in tool_id:
            for opt in tool.iter("option"):
                oid  = opt.get("id", "")
                vtyp = opt.get("valueType", "")

                if "include.paths" in oid and vtyp == "includePath":
                    for v in opt.iter("listOptionValue"):
                        asm_include_paths.append(_resolve(v.get("value", "")))

                elif "defs" in oid and vtyp == "definedSymbols":
                    for v in opt.iter("listOptionValue"):
                        asm_defines.append(v.get("value", ""))

    return {
        "include_paths":     include_paths,
        "defines":           defines,
        "other_c_flags":     other_c_flags,
        "forced_includes":   forced_includes,
        "asm_include_paths": asm_include_paths,
        "asm_defines":       asm_defines,
    }


def get_source_files() -> list[Path]:
    """Return source files declared as linked resources (type=1) in .project."""
    tree = ET.parse(PROJECT_FILE)
    root = tree.getroot()

    files: list[Path] = []
    for link in root.iter("link"):
        if link.findtext("type", "") != "1":
            continue
        uri = link.findtext("locationURI", "")
        if not uri:
            continue
        p = Path(_resolve(uri))
        if p.suffix in C_EXTS | ASM_EXTS:
            files.append(p)
    return files


def c_entry(src: Path, settings: dict, version_flags: list[str]) -> dict:
    args = [f"{ARM_PREFIX}gcc"]
    args += CPU_FLAGS
    args += [
        "-O0", "-g3",
        "-fmessage-length=0", "-fsigned-char",
        "-ffunction-sections", "-fdata-sections",
    ]
    args += settings["other_c_flags"]
    args += version_flags
    for d in settings["defines"]:
        args.append(f"-D{d}")
    for inc in settings["include_paths"]:
        args.append(f"-I{inc}")
    for fi in settings["forced_includes"]:
        args += ["-include", fi]
    args += ["-c", str(src)]
    return {"directory": str(PROJECT_DIR), "file": str(src), "arguments": args}


def asm_entry(src: Path, settings: dict) -> dict:
    args = [f"{ARM_PREFIX}gcc"]
    args += CPU_FLAGS
    args += ["-x", "assembler-with-cpp", "-g3"]
    for d in settings["asm_defines"]:
        args.append(f"-D{d}")
    for inc in settings["asm_include_paths"]:
        args.append(f"-I{inc}")
    args += ["-c", str(src)]
    return {"directory": str(PROJECT_DIR), "file": str(src), "arguments": args}


def main() -> None:
    global PROJECT_DIR, ECLIPSE_DIR, CPROJECT, PROJECT_FILE

    parser = argparse.ArgumentParser(
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "project_dir",
        help="Project root directory (the one containing the eclipse/ folder)",
    )
    parser.add_argument(
        "--config", "-c", default="Debug",
        help="Eclipse configuration to use (default: Debug)",
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help="Output path (default: <project-dir>/compile_commands.json)",
    )
    args = parser.parse_args()

    PROJECT_DIR  = Path(args.project_dir).resolve()
    ECLIPSE_DIR  = PROJECT_DIR / "eclipse"
    CPROJECT     = ECLIPSE_DIR / ".cproject"
    PROJECT_FILE = ECLIPSE_DIR / ".project"

    output_path = Path(args.output) if args.output else PROJECT_DIR / "compile_commands.json"

    if not CPROJECT.exists():
        sys.exit(
            f"[error] {CPROJECT} not found.\n"
            f"Make sure the path points to the project root (the folder containing eclipse/)."
        )

    print(f"Project root: {PROJECT_DIR}")
    print(f"Parsing .cproject [{args.config}] ...")
    settings = parse_cproject(args.config)
    print(f"  {len(settings['include_paths'])} include paths, "
          f"{len(settings['defines'])} defines")

    print("Running version.sh ...")
    version_flags = get_version_flags()
    if version_flags:
        print(f"  {' '.join(version_flags)}")

    print("Parsing .project for source files ...")
    sources = get_source_files()
    print(f"  found {len(sources)} source file(s)")

    entries = []
    for src in sources:
        if src.suffix in C_EXTS:
            entries.append(c_entry(src, settings, version_flags))
        elif src.suffix in ASM_EXTS:
            entries.append(asm_entry(src, settings))

    output_path.write_text(json.dumps(entries, indent=2))
    print(f"Written {len(entries)} entries -> {output_path}")


if __name__ == "__main__":
    main()
