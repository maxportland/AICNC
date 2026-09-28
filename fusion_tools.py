"""
Fusion 360 tool libraries: read them, and turn their presets into feeds this machine can run.

Fusion exports a library as JSON ({"data": [tool, ...], "version": N}) or as a .tools file
(the same JSON, zipped). Each tool has its own "unit" ("inches" or "millimeters"). Everything
here is converted to millimetres.

The libraries are vendor catalogs, not the machine's tool rack: tool.tbl stays the record of
which tools are loaded, their numbers and measured lengths. A tool picked from a catalog is
*linked* to a tool number (tool_links.json in the config directory), which is how Milo knows
the real geometry and cutting data of the tools it plans with.

Vendor presets usually assume router spindle speeds (18,000+ rpm). They are rescaled to the
spindle's limits by keeping the chip load per tooth and recomputing the feed.

No Qt here, so it can be tested on its own.
"""

import json
import math
import os
import zipfile
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

IN = 25.4
LIBRARY_DIR = os.path.expanduser("~/linuxcnc/tool_libraries")
CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
LINKS_FILE = os.path.join(CONFIG_DIR, "tool_links.json")

# Fusion tool type -> the CAM IR tool type Milo writes (None: not usable for milling ops Milo plans)
CAM_TYPES = {
    "flat end mill": "endmill", "bull nose end mill": "endmill", "ball end mill": "ball_endmill",
    "face mill": "endmill", "slot mill": "endmill", "drill": "drill", "spot drill": "drill",
    "center drill": "drill", "chamfer mill": "endmill", "counter sink": "endmill",
    "radius mill": "endmill", "tapered mill": "endmill", "lollipop mill": "ball_endmill",
    "form mill": None, "thread mill": "endmill", "engrave": "endmill",
}


@dataclass
class Preset:
    """Cutting data for one material, in mm and mm/min"""
    name: str
    rpm: float
    feed: float                 # mm/min
    plunge_feed: float          # mm/min
    chip_load: float            # mm per tooth
    stepdown: Optional[float]   # mm
    stepover: Optional[float]   # mm
    ramp_angle: Optional[float] # degrees
    coolant: str = ""


@dataclass
class FusionTool:
    guid: str
    vendor: str
    product_id: str
    description: str
    type: str                   # Fusion's type ("flat end mill", ...)
    material: str               # tool material ("carbide", "hss", ...)
    diameter: float             # mm
    flutes: int
    flute_length: float         # mm (LCF)
    overall_length: float       # mm (OAL)
    shank_diameter: float       # mm (SFDM)
    corner_radius: float = 0.0  # mm (RE)
    taper_angle: float = 0.0    # degrees (TA)
    point_angle: float = 0.0    # degrees (SIG), for drills, chamfers, countersinks
    tip_diameter: float = 0.0   # mm, for chamfer/tapered tools
    unit: str = "millimeters"   # what the file used
    link: str = ""
    presets: List[Preset] = field(default_factory=list)
    source: str = ""            # library file name

    @property
    def cam_type(self) -> Optional[str]:
        return CAM_TYPES.get(self.type, "endmill")

    @property
    def diameter_label(self) -> str:
        if self.unit == "inches":
            return f"{self.diameter / IN:g}\" ({self.diameter:.2f} mm)"
        return f"{self.diameter:g} mm"

    def short_name(self, limit=48) -> str:
        """For a tool table comment: vendor product, size, flutes, type"""
        vendor = self.vendor.split()[0] if self.vendor else ""
        size = f"{self.diameter / IN:g}in" if self.unit == "inches" else f"{self.diameter:g}mm"
        text = " ".join(x for x in (vendor, self.product_id, size, f"{self.flutes}FL", self.type) if x)
        return text[:limit].replace(";", ",")

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "FusionTool":
        data = dict(data)
        data["presets"] = [Preset(**p) for p in data.get("presets", [])]
        return FusionTool(**data)


# --- reading ----------------------------------------------------------------------------------

def _num(value, default=0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _preset(raw: dict, scale: float, flutes: int) -> Optional[Preset]:
    rpm = _num(raw.get("n"))
    feed = _num(raw.get("v_f")) * scale
    if rpm <= 0 or feed <= 0:
        return None
    chip = _num(raw.get("f_z")) * scale
    if chip <= 0 and flutes:
        chip = feed / (rpm * flutes)
    use_down = raw.get("use-stepdown", True)
    use_over = raw.get("use-stepover", True)
    stepdown = _num(raw.get("stepdown")) * scale if use_down and raw.get("stepdown") else None
    stepover = _num(raw.get("stepover")) * scale if use_over and raw.get("stepover") else None
    return Preset(
        name=str(raw.get("name") or "Default").strip(),
        rpm=rpm, feed=feed,
        plunge_feed=_num(raw.get("v_f_plunge"), feed / 3) * scale if raw.get("v_f_plunge") else feed / 3,
        chip_load=chip, stepdown=stepdown, stepover=stepover,
        ramp_angle=_num(raw.get("ramp-angle")) or None,
        coolant=str(raw.get("tool-coolant") or ""),
    )


def parse_tool(raw: dict, source: str = "") -> Optional[FusionTool]:
    """One Fusion tool entry -> FusionTool in mm, or None for holders, probes and broken entries"""
    kind = str(raw.get("type") or "").lower()
    if not kind or kind in ("holder", "probe") or "turning" in kind:
        return None
    geometry = raw.get("geometry") or {}
    scale = IN if str(raw.get("unit", "")).lower().startswith("inch") else 1.0
    diameter = _num(geometry.get("DC")) * scale
    if diameter <= 0:
        return None
    flutes = int(_num(geometry.get("NOF"), 1)) or 1
    presets = []
    for p in (raw.get("start-values") or {}).get("presets") or []:
        preset = _preset(p, scale, flutes)
        if preset is not None:
            presets.append(preset)
    return FusionTool(
        guid=str(raw.get("guid") or ""),
        vendor=str(raw.get("vendor") or "").strip(),
        product_id=str(raw.get("product-id") or "").strip(),
        description=" ".join(str(raw.get("description") or "").split()),
        type=kind,
        material=str(raw.get("BMC") or ""),
        diameter=diameter,
        flutes=flutes,
        flute_length=_num(geometry.get("LCF")) * scale,
        overall_length=_num(geometry.get("OAL")) * scale,
        shank_diameter=_num(geometry.get("SFDM")) * scale,
        corner_radius=_num(geometry.get("RE")) * scale,
        taper_angle=_num(geometry.get("TA")),
        point_angle=_num(geometry.get("SIG")),
        tip_diameter=_num(geometry.get("tip-diameter")) * scale,
        unit="inches" if scale != 1.0 else "millimeters",
        link=str(raw.get("product-link") or ""),
        presets=presets,
        source=source,
    )


def load_library(path: str) -> List[FusionTool]:
    """Read a Fusion .json or .tools library. Raises ValueError if it isn't one."""
    name = os.path.basename(path)
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                inner = next((n for n in archive.namelist() if n.lower().endswith(".json")), None)
                if inner is None:
                    raise ValueError(f"{name} has no JSON inside")
                data = json.loads(archive.read(inner).decode("utf-8"))
        else:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile) as e:
        raise ValueError(f"Can't read {name}: {e}")
    entries = data.get("data") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ValueError(f"{name} isn't a Fusion tool library")
    tools = [t for t in (parse_tool(e, name) for e in entries if isinstance(e, dict)) if t is not None]
    if not tools:
        raise ValueError(f"{name} has no milling tools")
    return tools


def find_library_files(roots=None) -> List[str]:
    """Candidate library files on the Desktop, Downloads, the library folder and USB sticks"""
    home = os.path.expanduser("~")
    roots = roots or [os.path.join(home, "Desktop"), os.path.join(home, "Downloads"), LIBRARY_DIR]
    for media in ("/media", os.path.join("/media", os.path.basename(home))):
        if os.path.isdir(media):
            roots += [os.path.join(media, d) for d in os.listdir(media)]
    found = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root) if os.path.isdir(root) else []:
            if dirpath.count(os.sep) - root.count(os.sep) >= 2:
                dirnames[:] = []
            for name in filenames:
                if name.lower().endswith((".json", ".tools")):
                    found.append(os.path.join(dirpath, name))
    return sorted(set(found))


def installed_libraries() -> List[str]:
    if not os.path.isdir(LIBRARY_DIR):
        return []
    return sorted(os.path.join(LIBRARY_DIR, n) for n in os.listdir(LIBRARY_DIR)
                  if n.lower().endswith((".json", ".tools")))


def install_library(path: str) -> str:
    """Check the file is a library and copy it into LIBRARY_DIR; returns the installed path"""
    load_library(path)
    os.makedirs(LIBRARY_DIR, exist_ok=True)
    target = os.path.join(LIBRARY_DIR, os.path.basename(path))
    if os.path.abspath(target) != os.path.abspath(path):
        with open(path, "rb") as src, open(target, "wb") as dst:
            dst.write(src.read())
    return target


# --- feeds for this machine ------------------------------------------------------------------

@dataclass
class ScaledPreset:
    name: str
    rpm: float
    feed: float
    plunge_feed: float
    chip_load: float
    stepdown: Optional[float]
    stepover: Optional[float]
    vendor_rpm: float
    vendor_feed: float
    limited: bool  # the vendor's speed was outside the spindle's range


def scale_preset(preset: Preset, flutes: int, min_rpm: float, max_rpm: float) -> ScaledPreset:
    """
    The vendor preset at a speed this spindle can run: same chip load per tooth, feed
    recomputed for the clamped speed (a router preset copied as-is would feed several times
    too fast for a slower spindle). Plunge feed scales by the same ratio.
    """
    rpm = min(max(preset.rpm, min_rpm), max_rpm) if max_rpm else preset.rpm
    ratio = rpm / preset.rpm if preset.rpm else 1.0
    feed = preset.chip_load * max(1, flutes) * rpm if preset.chip_load > 0 else preset.feed * ratio
    return ScaledPreset(
        name=preset.name, rpm=rpm, feed=feed, plunge_feed=preset.plunge_feed * ratio,
        chip_load=preset.chip_load, stepdown=preset.stepdown, stepover=preset.stepover,
        vendor_rpm=preset.rpm, vendor_feed=preset.feed, limited=abs(ratio - 1.0) > 1e-6,
    )


# --- links: tool number -> catalog tool -------------------------------------------------------

def load_links(path: str = LINKS_FILE) -> Dict[int, FusionTool]:
    try:
        with open(path) as f:
            data = json.load(f)
        return {int(k): FusionTool.from_dict(v) for k, v in data.items()}
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def save_links(links: Dict[int, FusionTool], path: str = LINKS_FILE):
    with open(path, "w") as f:
        json.dump({str(k): v.to_dict() for k, v in sorted(links.items())}, f, indent=1)


def link_tool(number: int, tool: FusionTool, path: str = LINKS_FILE):
    links = load_links(path)
    links[int(number)] = tool
    save_links(links, path)


def unlink_tool(number: int, path: str = LINKS_FILE):
    links = load_links(path)
    if links.pop(int(number), None) is not None:
        save_links(links, path)


# --- tool.tbl ---------------------------------------------------------------------------------

def write_tool_entry(table_path: str, number: int, diameter_mm: float, comment: str,
                     machine_units: str = "mm") -> bool:
    """
    Add or update one tool in a LinuxCNC tool table. An existing entry keeps its pocket and
    measured length (Z); only the diameter and comment change. Returns True if it was new.
    """
    diameter = diameter_mm if machine_units == "mm" else diameter_mm / IN
    comment = comment.replace(";", ",").replace("\n", " ").strip()
    lines = []
    if os.path.exists(table_path):
        with open(table_path) as f:
            lines = f.read().splitlines()
    new = True
    for i, line in enumerate(lines):
        data, _, _old = line.partition(";")
        words = data.split()
        if words and words[0].upper() == f"T{int(number)}":
            kept = [w for w in words if w[0].upper() not in ("D", "T")]
            lines[i] = " ".join([f"T{int(number)}"] + kept[:1] + [f"D{diameter:.4f}"] + kept[1:]) + f" ;{comment}"
            new = False
            break
    if new:
        lines.append(f"T{int(number)} P{int(number)} D{diameter:.4f} Z0.000 ;{comment}")
    tmp = table_path + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, table_path)
    return new


# --- for Milo ---------------------------------------------------------------------------------

def describe_for_prompt(number: int, tool: FusionTool, min_rpm: float, max_rpm: float,
                        units: str = "mm") -> str:
    """Geometry and cutting data of a linked tool, for the CAM system prompt (machine units)"""
    k = 1.0 if units == "mm" else 1 / IN
    u = units
    parts = [f"catalog: {tool.vendor} {tool.product_id} {tool.type}".strip(),
             f"{tool.flutes} flutes", f"flute length {tool.flute_length * k:.3g} {u}",
             f"overall length {tool.overall_length * k:.3g} {u}"]
    if tool.corner_radius:
        parts.append(f"corner radius {tool.corner_radius * k:.3g} {u}")
    if tool.point_angle:
        parts.append(f"point angle {tool.point_angle:g} deg")
    if tool.taper_angle:
        parts.append(f"taper angle {tool.taper_angle:g} deg")
    text = f"  T{number} geometry: " + ", ".join(parts) + "\n"
    if tool.presets:
        text += (f"  T{number} cutting data (vendor chip load, rescaled to this spindle's "
                 f"{min_rpm:g}-{max_rpm:g} rpm; use these unless told otherwise):\n")
        for preset in tool.presets[:8]:
            s = scale_preset(preset, tool.flutes, min_rpm, max_rpm)
            line = (f"    - {s.name}: rpm {s.rpm:.0f}, feed {s.feed * k:.4g} {u}/min, "
                    f"plunge {s.plunge_feed * k:.4g} {u}/min")
            if s.stepdown:
                line += f", stepdown {s.stepdown * k:.3g} {u}"
            if s.stepover:
                line += f", stepover {s.stepover * k:.3g} {u}"
            text += line + "\n"
    text += (f"  T{number} limit: never cut deeper than {tool.flute_length * k:.3g} {u} "
             f"(its flute length) below top_z in one operation.\n")
    return text
