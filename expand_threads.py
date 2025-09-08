#!/usr/bin/env python3
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
from decimal import Decimal, getcontext
import os
import math

# === CONFIGURATION ===

INPUT_FILE = "ISOMetricprofile.txt"              # Base Fusion XML to clone/augment
OUTPUT_FILE = "3D_Printable_ThreadLibrary_MultiFit.txt"

THREAD_ANGLE = Decimal("90.0")                   # 45° printable profile

# Size/pitch ranges
SIZE_START = Decimal("5.0")
SIZE_END   = Decimal("30.0")
SIZE_STEP  = Decimal("1.0")

PITCH_START = Decimal("0.5")
PITCH_END   = Decimal("3.5")
PITCH_STEP  = Decimal("0.1")

# Fit types (tune for printer/material)
# min_clearance is radial (difference in diameters)
FIT_TYPES = {
    "Functional": {
        "min_clearance": Decimal("0.20"),
        "es": Decimal("0.15"),   # external upper deviation (shifts external down)
        "Td": Decimal("0.20")    # tolerance width proxy
    },
    "Loose": {
        "min_clearance": Decimal("0.30"),
        "es": Decimal("0.10"),
        "Td": Decimal("0.25")
    }
}

# Heuristic build limit to avoid ultra-tight helices that tend to fail in Fusion
MIN_PITCH_TO_DIAM_RATIO = Decimal("0.01")  # allow smaller pitches than before

# Numeric stability for Decimal ops
getcontext().prec = 12

# === GEOMETRY HELPERS ===

def thread_height(pitch: Decimal, angle_deg: Decimal) -> Decimal:
    # H = 0.5 * P * cot(theta/2)
    return Decimal("0.5") * pitch * Decimal(str(1 / math.tan(math.radians(float(angle_deg) / 2.0))))

def h3(pitch: Decimal, angle_deg: Decimal) -> Decimal:
    # h3 = 5/8 * H
    return Decimal("0.625") * thread_height(pitch, angle_deg)

def crest_flat(pitch: Decimal, angle_deg: Decimal) -> Decimal:
    # Clamp crest flat so it doesn't exceed half the thread height
    cf = pitch / Decimal("8")
    H = thread_height(pitch, angle_deg)
    return cf if cf <= H / Decimal("2") else H / Decimal("2")

def root_flat(pitch: Decimal) -> Decimal:
    return pitch / Decimal("4")

def frange(start: Decimal, stop: Decimal, step: Decimal):
    cur = start
    while cur <= stop + Decimal("0.0001"):
        yield cur.quantize(Decimal("0.001"))
        cur += step

# === TOLERANCE CALCULATOR (scaled EI to help small pitches) ===
# External:
#   major_max = size - es
#   pitch_max = size - es  (proxy for tolerance band top)
# Internal:
#   Choose EI so that internal_minor >= external_major + clearance
#   EI_required = es + clearance - 2*h3
#   Also scale EI with pitch to keep small threads printable:
#   EI_scaled = 0.05 + 0.5 * pitch  (tunable)
#   EI = max(EI_required, EI_scaled)
# We carry TD/Td as metadata; geometry uses top values to keep XML simple.

def calculate_tolerances(size: Decimal, pitch: Decimal, fit: dict) -> dict:
    h3_val = h3(pitch, THREAD_ANGLE)
    es = fit["es"]
    Td = fit["Td"]
    clearance = fit["min_clearance"]

    EI_required = es + clearance - Decimal("2") * h3_val
    EI_scaled = Decimal("0.05") + (Decimal("0.5") * pitch)
    EI = EI_required if EI_required >= EI_scaled else EI_scaled

    return {
        "external": {"es": es, "Td": Td},
        "internal": {"EI": EI, "TD": Td}
    }

# === BUILDABILITY CHECKS FOR EXTERNAL ===

def external_buildable(size: Decimal, pitch: Decimal, tol: dict, fit: dict) -> bool:
    # Helix tightness check
    if size > 0 and pitch < size * MIN_PITCH_TO_DIAM_RATIO:
        return False

    # Clearance check: internal minor >= external major + min_clearance
    h3_val = h3(pitch, THREAD_ANGLE)
    minor_dia_nom = size - Decimal("2") * h3_val
    external_major_max = size - tol["external"]["es"]
    internal_minor_min = minor_dia_nom + tol["internal"]["EI"]
    if internal_minor_min < external_major_max + fit["min_clearance"]:
        return False

    # Crest flat sanity check
    H = thread_height(pitch, THREAD_ANGLE)
    if crest_flat(pitch, THREAD_ANGLE) > H / Decimal("2"):
        return False

    return True

# === THREAD XML BUILDERS ===

def make_thread(gender: str, size: Decimal, pitch: Decimal, tol: dict, fit_name: str) -> ET.Element:
    H = thread_height(pitch, THREAD_ANGLE)
    h3_val = h3(pitch, THREAD_ANGLE)

    pitch_dia_nom = (size - Decimal("0.75") * H).quantize(Decimal("0.000"))
    minor_dia_nom = (size - Decimal("2") * h3_val).quantize(Decimal("0.000"))

    if gender == "external":
        es = tol["external"]["es"]
        # We publish the top-of-zone (max) values in the XML fields
        major_max = (size - es).quantize(Decimal("0.000"))
        pitch_max = (size - es).quantize(Decimal("0.000"))
        minor = minor_dia_nom  # geometry-based for modeled shape
        flat_val = crest_flat(pitch, THREAD_ANGLE).quantize(Decimal("0.000"))
        flat_tag = "CrestFlat"
        maj_out = major_max
        p_out = pitch_max
    else:
        EI = tol["internal"]["EI"]
        TD = tol["internal"]["TD"]
        major_min = (size + EI).quantize(Decimal("0.000"))
        major_max = (major_min + TD).quantize(Decimal("0.000"))
        pitch_max = (major_min + TD).quantize(Decimal("0.000"))
        minor = (minor_dia_nom + EI).quantize(Decimal("0.000"))
        flat_val = root_flat(pitch).quantize(Decimal("0.000"))
        flat_tag = "RootFlat"
        maj_out = major_max
        p_out = pitch_max

    t = ET.Element("Thread")
    ET.SubElement(t, "Gender").text = gender
    ET.SubElement(t, "Class").text = fit_name
    ET.SubElement(t, "MajorDia").text = f"{maj_out:.3f}"
    ET.SubElement(t, "PitchDia").text = f"{p_out:.3f}"
    ET.SubElement(t, "MinorDia").text = f"{minor:.3f}"
    ET.SubElement(t, flat_tag).text = f"{flat_val:.3f}"
    return t

def make_designation(size: Decimal, pitch: Decimal) -> ET.Element:
    size_str = f"{size:.3f}".rstrip("0").rstrip(".")
    pitch_str = f"{pitch:.3f}".rstrip("0").rstrip(".")
    label = f"M{size_str}x{pitch_str}"

    des = ET.Element("Designation")
    ET.SubElement(des, "ThreadDesignation").text = label
    ET.SubElement(des, "CTD").text = label
    ET.SubElement(des, "Pitch").text = pitch_str

    for fit_name, fit in FIT_TYPES.items():
        tol = calculate_tolerances(size, pitch, fit)

        # External-first policy: if external fails, skip the pair (as requested)
        if not external_buildable(size, pitch, tol, fit):
            # Optional: uncomment for debug logging
            # print(f"Skip M{size}x{pitch} [{fit_name}] - external failed checks")
            continue

        ext_thread = make_thread("external", size, pitch, tol, fit_name)
        int_thread = make_thread("internal", size, pitch, tol, fit_name)

        des.append(ext_thread)
        des.append(int_thread)

    return des

def make_thread_size(size: Decimal) -> ET.Element:
    ts = ET.Element("ThreadSize")
    ET.SubElement(ts, "Size").text = f"{size:.3f}".rstrip("0").rstrip(".")
    for pitch in frange(PITCH_START, PITCH_END, PITCH_STEP):
        des = make_designation(size, pitch)
        # Only append designation if it contains at least one valid thread pair
        if list(des.findall("Thread")):
            ts.append(des)
    return ts

# === XML WRITER (tabs + newlines) ===

def serialize_elem(elem: ET.Element, level: int = 0) -> str:
    indent = "\t" * level
    tag = elem.tag
    text = (elem.text or "").strip()
    children = list(elem)

    if not children:
        return f"{indent}<{tag}>{escape(text)}</{tag}>" if text else f"{indent}<{tag}></{tag}>"

    lines = [f"{indent}<{tag}>"]
    if text:
        lines.append(f"{indent}\t{escape(text)}")
    for child in children:
        lines.append(serialize_elem(child, level + 1))
    lines.append(f"{indent}</{tag}>")
    return "\n".join(lines)

def write_xml_with_tabs(root: ET.Element, output_file: str):
    xml_header = "<?xml version='1.0' encoding='UTF-8'?>\n"
    body = serialize_elem(root, 0)
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(xml_header + body)

# === MAIN ===

def main():
    tree = ET.parse(INPUT_FILE)
    root = tree.getroot()

    # Reset key metadata
    for tag in ("Name", "CustomName", "Angle", "Unit", "SortOrder"):
        node = root.find(tag)
        if node is not None:
            root.remove(node)

    ET.SubElement(root, "Name").text = "3D Printable Metric Profile (Multi-Fit, Safe External)"
    ET.SubElement(root, "CustomName").text = "Threads — Functional & Loose (skip unbuildable pairs)"
    ET.SubElement(root, "Unit").text = "mm"
    ET.SubElement(root, "Angle").text = f"{THREAD_ANGLE:.1f}"
    ET.SubElement(root, "SortOrder").text = "3"

    # Remove existing sizes
    for ts in list(root.findall("ThreadSize")):
        root.remove(ts)

    # Build sizes
    any_sizes = False
    for size in frange(SIZE_START, SIZE_END, SIZE_STEP):
        ts_elem = make_thread_size(size)
        if list(ts_elem.findall("Designation")):
            root.append(ts_elem)
            any_sizes = True

    write_xml_with_tabs(root, OUTPUT_FILE)
    print(f"✅ Wrote: {os.path.abspath(OUTPUT_FILE)}")
    if not any_sizes:
        print("⚠️ No valid thread pairs generated. Consider loosening fit settings or angle/pitch limits.")

if __name__ == "__main__":
    main()
