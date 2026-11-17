"""How many grams a model will take to print, before slicing it (the slicer then gives the exact figure).

A print isn't solid: it's walls (about 3 lines thick, ~1.2 mm) all over the surface, plus sparse infill inside. So:
    grams ≈ density × (surface × wall  +  infill × (volume − surface × wall))
The volume needs a closed surface. Downloaded models often have small gaps ("not watertight"), so then: small holes
are patched on a copy, and failing that the model is voxelised (filled in as small cubes) to measure what's inside.
"""
import logging

log = logging.getLogger("batsy.printweight")
DENSITY = {"pla": 1.24, "petg": 1.27, "abs": 1.04, "asa": 1.07, "tpu": 1.21, "nylon": 1.14}
WALL_MM = 1.2      # 3 perimeters of 0.4 mm (top/bottom skins are about the same thickness)
INFILL = 0.15      # a typical sparse infill


def solid_volume_cm3(mesh) -> tuple[float, str]:
    """(cm³ enclosed, how it was measured: 'exact', 'patched' or 'approx')."""
    if mesh.is_watertight:
        return abs(float(mesh.volume)) / 1000, "exact"
    try:
        m = mesh.copy()
        m.merge_vertices()
        m.fill_holes()
        if m.is_watertight:
            return abs(float(m.volume)) / 1000, "patched"
    except Exception:
        pass
    try:  # cubes about 1/64 of the model's size (at least 0.4 mm): fills in the inside even with gaps in the skin
        pitch = max(0.5, float(max(mesh.extents)) / 64)
        vox = mesh.voxelized(pitch).fill()
        return float(vox.filled_count) * pitch ** 3 / 1000, "approx"
    except Exception as e:
        log.info("couldn't voxelise: %s", e)
        return 0.0, "unknown"


def estimate(mesh, material: str = "pla", infill: float = INFILL) -> dict:
    """{print_g, solid_g, volume_cm3, how}: the grams to print it (walls + infill), and if it were solid."""
    vol, how = solid_volume_cm3(mesh)
    area_cm2 = float(mesh.area) / 100
    shell = min(vol, area_cm2 * WALL_MM / 10)  # cm² × cm = cm³
    dens = DENSITY.get(material.lower(), 1.24)
    if vol <= 0:  # nothing measurable inside: at least the walls
        shell = area_cm2 * WALL_MM / 10
        return {"print_g": round(shell * dens, 1), "solid_g": None, "volume_cm3": None, "how": how,
                "material": material}
    return {"print_g": round((shell + infill * (vol - shell)) * dens, 1), "solid_g": round(vol * dens, 1),
            "volume_cm3": round(vol, 2), "how": how, "material": material}


def said(e: dict) -> str:
    """'About 24 grams of PLA to print (walls and 15% infill); 61 grams if it were solid.'"""
    if not e or not e.get("print_g"):
        return "I couldn't estimate its weight."
    s = f"About {e['print_g']:g} grams of {e['material'].upper()} to print, with walls and 15 percent infill"
    if e.get("solid_g"):
        s += f"; {e['solid_g']:g} grams if it were solid"
    return s + (". The surface has gaps, so that's approximate." if e["how"] == "approx" else ".")
