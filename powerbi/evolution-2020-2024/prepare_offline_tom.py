"""Extract existing TOM libraries for offline metadata parsing, never run MCP/app.

Reads the standard .NET single-file bundle manifest. All output stays in this
folder's .offline-tom/. Nothing in generated/ or the installation is modified.
"""

import argparse
import io
from pathlib import Path
import shutil
import struct
import zlib

from generate import ROOT, TABLES


SIGNATURE = bytes.fromhex("8b1202b96a612038727b930214d7a03213f5b9e6efae3318ee3b2dce24b36aae")
LIBRARIES = {
    "Microsoft.AnalysisServices.Core.dll",
    "Microsoft.AnalysisServices.dll",
    "Microsoft.AnalysisServices.Tabular.dll",
    "Microsoft.AnalysisServices.Tabular.Json.dll",
    "Microsoft.AnalysisServices.Runtime.Core.dll",
    "Microsoft.AnalysisServices.Runtime.Windows.dll",
    "Newtonsoft.Json.dll",
}


def read_string(stream):
    length = 0
    for shift in range(0, 35, 7):
        chunk = stream.read(1)
        if not chunk:
            raise ValueError("Truncated bundle string")
        byte = chunk[0]
        length |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return stream.read(length).decode("utf-8")
    raise ValueError("Invalid bundle string length")


def extract(bundle):
    blob = Path(bundle).read_bytes()
    marker = blob.find(SIGNATURE)
    if marker < 8:
        raise ValueError("Not a recognized .NET single-file bundle")
    offset = struct.unpack_from("<q", blob, marker - 8)[0]
    if not 0 < offset < len(blob):
        raise ValueError("Invalid bundle header offset")
    stream = io.BytesIO(blob)
    stream.seek(offset)
    major, minor, count = struct.unpack("<IIi", stream.read(12))
    if major not in (2, 6) or not 0 < count < 10000:
        raise ValueError(f"Unsupported bundle format {major}.{minor}")
    read_string(stream)
    stream.read(40)  # deps/runtime-config locations and flags (v2+).
    target = ROOT / ".offline-tom" / "assemblies"
    target.mkdir(parents=True, exist_ok=True)
    extracted = []
    for _ in range(count):
        file_offset, size = struct.unpack("<qq", stream.read(16))
        compressed = struct.unpack("<q", stream.read(8))[0] if major >= 6 else 0
        stream.read(1)  # Bundle file type.
        relative = read_string(stream)
        name = Path(relative).name
        if name not in LIBRARIES:
            continue
        length = compressed or size
        if not 0 <= file_offset <= file_offset + length <= len(blob):
            raise ValueError(f"Invalid bundle entry {relative}")
        payload = blob[file_offset:file_offset + length]
        if compressed:
            payload = zlib.decompress(payload, -15)
        if len(payload) != size:
            raise ValueError(f"Unexpected extracted size {relative}")
        (target / name).write_bytes(payload)
        extracted.append(name)
    if "Microsoft.AnalysisServices.Tabular.dll" not in extracted:
        raise ValueError("Bundle has no accessible TOM Tabular library")
    print("Extracted offline only:", ", ".join(extracted))
    return target


def prepare_model():
    target = ROOT / ".offline-tom" / "model"
    tables = target / "tables"
    tables.mkdir(parents=True, exist_ok=True)
    (target / "database.tmdl").write_text("database OfflineEvolutionValidation\n\tcompatibilityLevel: 1604\n", encoding="utf-8")
    (target / "model.tmdl").write_text("model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n\n" + "".join("ref table " + name + "\n" for name in [*TABLES, "DimStation"]), encoding="utf-8")
    for table in TABLES:
        shutil.copyfile(ROOT / "generated" / (table + ".tmdl"), tables / (table + ".tmdl"))
    shutil.copyfile(ROOT / "generated" / "relationships.tmdl", target / "relationships.tmdl")
    (tables / "DimStation.tmdl").write_text("/// Offline reference stub only; NEVER deploy this file.\ntable DimStation\n\tcolumn code\n\t\tdataType: string\n\t\tisKey\n\t\tsourceColumn: code\n", encoding="utf-8")
    print("Offline model with a read-only DimStation reference stub:", target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True, help="Existing modeling installation's single-file exe, read but NEVER executed")
    args = parser.parse_args()
    print("Assembly directory:", extract(args.bundle))
    prepare_model()
