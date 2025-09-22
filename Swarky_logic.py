# swarky_logic.py
from __future__ import annotations
import re, logging, time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple, Iterable, Dict
from datetime import datetime

from swarky_io import IOOps

# ===== Regex =====
BASE_NAME = re.compile(r"D(\w)(\w)(\d{6})R(\d{2})S(\d{2})(\w)\.(tif|pdf)$", re.IGNORECASE)
ISS_BASENAME = re.compile(r"G(\d{4})([A-Za-z0-9]{4})([A-Za-z0-9]{6})ISSR(\d{2})S(\d{2})\.pdf$", re.IGNORECASE)

# ===== Config (solo paths, niente I/O dentro al dataclass) =====
@dataclass(frozen=True)
class Config:
    DIR_HPLOTTER: Path
    ARCHIVIO_DISEGNI: Path
    ERROR_DIR: Path
    PARI_REV_DIR: Path
    PLM_DIR: Path
    ARCHIVIO_STORICO: Path
    DIR_ISS: Path
    DIR_FIV_LOADING: Path
    DIR_HENGELO: Path
    DIR_PLM_ERROR: Path
    DIR_TABELLARI: Path
    LOG_DIR: Optional[Path] = None
    LOG_LEVEL: int = logging.INFO
    ACCEPT_PDF: bool = True
    LOG_PHASES: bool = True

# ===== Mappe & util =====
LOCATION_MAP = {
    ("M", "*"): ("costruttivi", "Costruttivi", "m", "DETAIL", "Italian"),
    ("K", "*"): ("bozzetti", "Bozzetti", "k", "Customer Drawings", "English"),
    ("F", "*"): ("fornitori", "Fornitori", "f", "Vendor Supplied Data", "English"),
    ("T", "*"): ("tenute_meccaniche", "T_meccaniche", "t", "Customer Drawings", "English"),
    ("E", "*"): ("sezioni", "Sezioni", "s", "Customer Drawings", "English"),
    ("S", "*"): ("sezioni", "Sezioni", "s", "Customer Drawings", "English"),
    ("N", "*"): ("marcianise", "Marcianise", "n", "DETAIL", "Italian"),
    ("P", "*"): ("preventivi", "Preventivi", "p", "Customer Drawings", "English"),
    ("*", "4"): ("pID_ELETTRICI", "Pid_Elettrici", "m", "Customer Drawings", "Italian"),
    ("*", "5"): ("piping", "Piping", "m", "Customer Drawings", "Italian"),
}
DEFAULT_LOCATION = ("unknown", "Unknown", "m", "Customer Drawings", "English")

def _docno_from_match(m: re.Match) -> str:
    return f"D{m.group(1)}{m.group(2)}{m.group(3)}"

def _parse_prefixed(names: tuple[str, ...]) -> list[tuple[str, str, str, str]]:
    out: list[tuple[str, str, str, str]] = []
    for nm in names:
        mm = BASE_NAME.fullmatch(nm)
        if mm:
            out.append((mm.group(4), nm, mm.group(6).upper(), mm.group(5)))
    return out

def size_from_letter(ch: str) -> str:
    return dict(A="A4",B="A3",C="A2",D="A1",E="A0").get(ch.upper(),"A4")

def uom_from_letter(ch: str) -> str:
    return dict(N="(Not applicable)",M="Metric",I="Inch",D="Dual").get(ch.upper(),"Metric")

def map_location(m: re.Match, cfg: Config) -> dict:
    first = m.group(3)[0]
    l2 = m.group(2).upper()
    loc = (
        LOCATION_MAP.get((l2, first))
        or LOCATION_MAP.get((l2, "*"))
        or LOCATION_MAP.get(("*", first))
        or DEFAULT_LOCATION
    )
    folder, log_name, subloc, doctype, lang = loc
    arch_tif_loc = m.group(1).upper() + subloc
    dir_tif_loc = cfg.ARCHIVIO_DISEGNI / folder / arch_tif_loc
    return dict(folder=folder, log_name=log_name, subloc=subloc, doctype=doctype, lang=lang,
                arch_tif_loc=arch_tif_loc, dir_tif_loc=dir_tif_loc)

# ===== UI-phase helpers (solo eventi log, la GUI li intercetta) =====
class _UIPhase:
    def __init__(self, label: str):
        self.label = label
        self.t0 = 0.0
    def __enter__(self):
        logging.info(self.label, extra={"ui": ("phase", self.label)})
        self.t0 = time.perf_counter()
        return self
    def __exit__(self, exc_type, exc, tb):
        elapsed_ms = int((time.perf_counter() - self.t0) * 1000)
        logging.info(f"{self.label} finita in {elapsed_ms} ms",
                     extra={"ui": ("phase_done", elapsed_ms)})
        return False

def ui_phase(label: str) -> _UIPhase:
    return _UIPhase(label)

# ===== Log rows (GUI + batch TXT) =====
def _now_ddmonYYYY() -> str: return datetime.now().strftime("%d.%b.%Y")
def _now_HHMMSS() -> str:   return datetime.now().strftime("%H:%M:%S")

def log_swarky(file_name: str, loc: str, process: str,
               archive_dwg: str = "", dest: str = ""):
    # Il file TXT verrà scritto da chi intercetta (es. FileHandler nel main)
    logging.info("processed %s", file_name,
                 extra={"ui": ("processed", file_name, process, archive_dwg, dest)})

def log_error(file_name: str, err: str, archive_dwg: str = ""):
    logging.error("anomaly %s", file_name, extra={"ui": ("anomaly", file_name, err)})

# ===== Pipeline (dipende solo da IOOps e Config) =====
def list_same_doc_prefisso(io: IOOps, dirp: Path, m: re.Match) -> list[tuple[str, str, str, str]]:
    docno = _docno_from_match(m)
    names_all = io.list_same_doc_prefisso(dirp, docno)
    if not names_all:
        return []
    names = tuple(nm for nm in names_all if nm.lower().endswith((".tif",".pdf")))
    return _parse_prefixed(names)

def write_edi_body_for_standard(*, m: re.Match, loc: dict, file_name: str) -> List[str]:
    document_no = f"D{m.group(1)}{m.group(2)}{m.group(3)}"
    rev = m.group(4); sheet = m.group(5)
    file_type = "Pdf" if Path(file_name).suffix.lower() == ".pdf" else "Tiff"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return [
        "[Database]",
        "ServerName=ORMDB33",
        "ProjectName=FPD Engineering",
        "[DatabaseFields]",
        f"DocumentNo={document_no}",
        f"DocumentRev={rev}",
        f"SheetNumber={sheet}",
        "Description=",
        f"ActualSize={size_from_letter(m.group(1))}",
        "PumpModel=(UNKNOWN)",
        "OEM=Flowserve",
        "PumpSize=",
        "OrderNumber=",
        "SerialNumber=",
        f"Document_Type={loc['doctype']}",
        "DrawingClass=COMMERCIAL",
        "DesignCenter=Desio, Italy",
        "OEMSite=Desio, Italy",
        "OEMDrawingNumber=",
        f"UOM={uom_from_letter(m.group(6))}",
        f"DWGLanguage={loc['lang']}",
        "CurrentRevision=Y",
        "EnteredBy=10150286",
        "Notes=",
        "NonEnglishDesc=",
        "SupersededBy=",
        "NumberOfStages=",
        "[DrawingInfo]",
        f"DocumentNo={document_no}",
        f"SheetNumber={sheet}",
        ("Document_Type=Detail" if loc["doctype"]=="DETAIL" else "Document_Type=Customer Drawings"),
        f"DocumentRev={rev}",
        f"FileName={file_name}",
        f"FileType={file_type}",
        f"Currentdate={now}",
    ]
