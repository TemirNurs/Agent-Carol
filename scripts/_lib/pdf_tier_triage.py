#!/usr/bin/env python3
r"""pdf_tier_triage.py — STEP 0 of every takeoff: classify the INPUT'S fidelity.

Takeoffs swing because the engine measures a degraded PICTURE without knowing how
degraded it is. A construction drawing is born as a Revit/IFC/DWG database (exact
quantities); what we receive is a lossy export on a fidelity ladder. This triages
the received file into a TIER so the rest of the system routes correctly and never
promotes a degraded source to GOLD:

  T0_IFC / T0_CAD  — model / CAD source (.ifc/.dwg/.dxf): query exact quantities.
  T1_LAYERED       — vector PDF whose OCG layers include real A-WALL/A-DOOR/A-GLAZ:
                     isolate the arch layers (clean geometry).  GOLD-eligible.
  T2_FLATTENED     — vector PDF, layers stripped (Microsoft Print-to-PDF / plot /
                     portal re-process). Vectors survive, unlabeled.  Cap SILVER.
  T3_RASTER        — pure image (react-pdf / scan): no vectors, no text.  Cap
                     SILVER/REJECT + request a better file.

Pure PyMuPDF (+ optional pikepdf fallback). No new dependency, no model needed.
Classifies PER SHEET so a set can be T1 on the plans and T3 on one rasterized one.
"""
from __future__ import annotations
import os

try:
    import fitz
except Exception:
    fitz = None
try:
    from . import ncs_layers as NCS
except ImportError:
    import ncs_layers as NCS

MODEL_EXT = {".ifc": "T0_IFC", ".ifczip": "T0_IFC", ".dwg": "T0_CAD", ".dxf": "T0_CAD",
             ".dwfx": "T0_CAD", ".dwf": "T0_CAD", ".rvt": "T0_MODEL"}
FLATTEN_PRODUCERS = ("microsoft", "print to pdf", "react-pdf", "snagit", "image", "scan")


def _ocg_names(doc) -> list:
    names = []
    try:
        ocgs = doc.get_ocgs() or {}
        names = [(v.get("name") or "") for v in ocgs.values()]
    except Exception:
        pass
    return [n for n in names if n]


def _page_tier(page, ocg_arch: bool, producer: str) -> dict:
    n_vec = len(page.get_drawings())
    txt = (page.get_text() or "").strip()
    n_txt = len(txt)
    n_img = len(page.get_images())
    prod = producer.lower()
    raster = (n_txt < 20 and n_vec < 5 and n_img >= 1)
    if raster:
        tier = "T3_RASTER"
    elif ocg_arch:
        tier = "T1_LAYERED"
    elif n_vec >= 50:
        tier = "T2_FLATTENED"
    elif n_img >= 1 and n_vec < 50:
        tier = "T3_RASTER"
    else:
        tier = "T2_FLATTENED"
    return {"tier": tier, "n_vectors": n_vec, "n_textchars": n_txt, "n_images": n_img}


def triage(path: str, sample_pages: int = 12) -> dict:
    ext = os.path.splitext(path)[1].lower()
    if ext in MODEL_EXT:
        return {"tier": MODEL_EXT[ext], "path": os.path.basename(path), "ext": ext,
                "recommendation": "STRUCTURED SOURCE — read exact quantities "
                                  "(IfcOpenShell for .ifc, ezdxf for .dxf/.dwg→DXF). GOLD, no measurement."}
    if ext in (".bcf", ".bcfzip"):
        return {"tier": "SIGNAL_MODEL_EXISTS", "path": os.path.basename(path),
                "recommendation": "BCF present → a BIM model EXISTS. Request the IFC from the GC."}
    if ext != ".pdf" or fitz is None:
        return {"tier": "UNKNOWN", "path": os.path.basename(path), "ext": ext}

    doc = fitz.open(path)
    md = doc.metadata or {}
    producer = f"{md.get('producer','')} {md.get('creator','')}".strip()
    ocg = _ocg_names(doc)
    ocg_sum = NCS.summarize(ocg)
    ocg_arch = ocg_sum["arch_layers_present"]
    n = doc.page_count
    # sample evenly across the set (key paint sheets are usually mid-set)
    idxs = list(range(n)) if n <= sample_pages else \
        sorted(set([0] + [int(i * (n - 1) / (sample_pages - 1)) for i in range(sample_pages)]))
    per_sheet = []
    counts = {}
    for i in idxs:
        pt = _page_tier(doc[i], ocg_arch, producer)
        pt["page"] = i + 1
        per_sheet.append(pt)
        counts[pt["tier"]] = counts.get(pt["tier"], 0) + 1
    doc.close()

    # document tier = best architectural tier among the sampled sheets (the paint sheets
    # are what matter); but if the producer screams flatten and no arch layers, cap at T2.
    order = ["T1_LAYERED", "T2_FLATTENED", "T3_RASTER"]
    present = [t for t in order if counts.get(t)]
    doc_tier = present[0] if present else "T2_FLATTENED"
    if doc_tier == "T1_LAYERED" and not ocg_arch:
        doc_tier = "T2_FLATTENED"
    flattened_by_producer = any(k in producer.lower() for k in FLATTEN_PRODUCERS)

    rec = {
        "T1_LAYERED": "Layered vector PDF — isolate A-WALL/A-DOOR/A-GLAZ OCG layers (M-LAYER). GOLD-eligible.",
        "T2_FLATTENED": ("Flattened vector PDF (layers stripped) — multi-measure M-VEC/M-DIM/M-EXT + read "
                         "schedule tables. CAP at SILVER. For a high-value bid, REQUEST a layered/vector "
                         "PDF or the CAD/IFC from the GC."),
        "T3_RASTER": ("Rasterized PDF (no vectors/text) — OCR the scale bar + dimension strings only; "
                      "CAP SILVER/REJECT. REQUEST a vector PDF or the model from the GC before trusting a number."),
    }[doc_tier]

    return {"tier": doc_tier, "path": os.path.basename(path), "producer": producer or "?",
            "flattened_by_producer": flattened_by_producer,
            "ocg_layer_count": len(ocg), "arch_layers_present": ocg_arch,
            "ocg_roles": ocg_sum["roles"], "wall_layers": ocg_sum["wall_layers"],
            "n_pages": n, "tier_counts": counts, "per_sheet": per_sheet,
            "recommendation": rec,
            "confidence_cap": {"T1_LAYERED": "GOLD", "T2_FLATTENED": "SILVER",
                               "T3_RASTER": "SILVER"}.get(doc_tier, "SILVER")}


if __name__ == "__main__":
    import sys, json
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for p in sys.argv[1:]:
        t = triage(p)
        print(json.dumps({k: t[k] for k in t if k != "per_sheet"}, indent=1, default=str))
