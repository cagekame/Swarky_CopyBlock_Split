#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import sys, re, time, logging, json, os, shutil, glob
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple

# Solo Windows
if sys.platform != "win32":
    raise RuntimeError("Questo programma è supportato solo su Windows.")

# ---- CONFIG DATACLASS ----------------------------------------------------------------

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

    @staticmethod
    def from_json(d: Dict[str, Any]) -> "Config":
        p = d.get("paths", {})
        def P(key: str, default: Optional[str]=None) -> Path:
            val = p.get(key, default)
            if val is None:
                raise KeyError(f"Config mancante: paths.{key}")
            return Path(val)
        log_dir = p.get("log_dir")
        return Config(
            DIR_HPLOTTER=P("hplotter"),
            ARCHIVIO_DISEGNI=P("archivio"),
            ERROR_DIR=P("error_dir"),
            PARI_REV_DIR=P("pari_rev"),
            PLM_DIR=P("plm"),
            ARCHIVIO_STORICO=P("storico"),
            DIR_ISS=P("iss"),
            DIR_FIV_LOADING=P("fiv"),
            DIR_HENGELO=P("heng"),
            DIR_PLM_ERROR=P("error_plm"),
            DIR_TABELLARI=P("tab"),
            LOG_DIR=Path(log_dir) if log_dir else None,
            LOG_LEVEL=logging.INFO,
            ACCEPT_PDF=bool(d.get("ACCEPT_PDF", True)),
            LOG_PHASES=bool(d.get("LOG_PHASES", True)),
        )

# ---- REGEX / PREFISSO DOCNO ----------------------------------------------------------

BASE_NAME = re.compile(r"D(\w)(\w)(\d{6})R(\d{2})S(\d{2})(\w)\.(tif|pdf)$", re.IGNORECASE)
ISS_BASENAME = re.compile(r"G(\d{4})([A-Za-z0-9]{4})([A-Za-z0-9]{6})ISSR(\d{2})S(\d{2})\.pdf$", re.IGNORECASE)
ISS_BASENAME_2 = re.compile(r"^(?P<docno>.+?)R(?P<rev>\d{2})S(?P<sheet>\d{2})\.(?P<ext>pdf)$", re.IGNORECASE)

def _docno_from_match(m: re.Match) -> str:
    return f"D{m.group(1)}{m.group(2)}{m.group(3)}"

def _parse_prefixed(names: tuple[str, ...]) -> list[tuple[str, str, str, str]]:

    out: list[tuple[str, str, str, str]] = []
    for nm in names:
        mm = BASE_NAME.fullmatch(nm)
        if mm:
            out.append((mm.group(4), nm, mm.group(6).upper(), mm.group(5)))
    return out

def _iter_docno_files(dirp: Path, docno: str):
    pattern = str(dirp / f"{docno}*")
    for full_path in glob.iglob(pattern):
        nm = os.path.basename(full_path)
        if nm.lower().endswith((".tif", ".pdf")):
            yield nm

def _list_same_doc_prefisso(dirp: Path, m: re.Match) -> list[tuple[str, str, str, str]]:
    docno = _docno_from_match(m)
    names = tuple(_iter_docno_files(dirp, docno))
    if not names:
        return []
    return _parse_prefixed(names)

def _list_nonstandard_same_doc(dirp: Path, m: re.Match) -> list[str]:
    docno = _docno_from_match(m)
    names = list(_iter_docno_files(dirp, docno))
    if not names:
        return []
    return [nm for nm in names if BASE_NAME.fullmatch(nm) is None]

# ---- LOGGING -------------------------------------------------------------------------

_FILE_LOG_BUF: list[str] = []

def month_tag() -> str:
    return datetime.now().strftime("%b.%Y")

def setup_logging(cfg: Config):
    log_dir = cfg.LOG_DIR or cfg.DIR_HPLOTTER
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"Swarky_{month_tag()}.log"

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)

    class _PhaseFilter(logging.Filter):
        def __init__(self, enable_phases: bool):
            super().__init__()
            self.enable_phases = enable_phases
        def filter(self, record: logging.LogRecord) -> bool:
            ui = getattr(record, "ui", None)
            if not self.enable_phases and ui:
                return False
            return True

    fh.addFilter(_PhaseFilter(cfg.LOG_PHASES))

    root = logging.getLogger()
    root.setLevel(cfg.LOG_LEVEL)

    # mantieni altri handler (es. GUI), sostituisci solo il FileHandler
    new_handlers = [h for h in root.handlers if not isinstance(h, logging.FileHandler)]
    new_handlers.append(fh)
    root.handlers = new_handlers

    logging.debug("Log file: %s", log_file)

def _append_filelog_line(line: str) -> None:
    _FILE_LOG_BUF.append(line)

def _flush_file_log(cfg: Config) -> None:
    if not _FILE_LOG_BUF:
        return
    log_path = (cfg.LOG_DIR or cfg.DIR_HPLOTTER) / f"Swarky_{month_tag()}.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write("\n".join(_FILE_LOG_BUF) + "\n")
    finally:
        _FILE_LOG_BUF.clear()

# ---- FS UTILS (cross-volume) ---------------------------------------------------------

def _copy_file(src: Path, dst: Path) -> None:

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)

def copy_to(src: Path, dst_dir: Path) -> None:

    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    _copy_file(src, dst)

def move_to(src: Path, dst_dir: Path) -> None:

    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    _copy_file(src, dst)
    try:
        src.unlink()
    except FileNotFoundError:
        pass

def move_to_storico_safe(src: Path, dst_dir: Path) -> tuple[bool, int]:

    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name

    if dst.exists():
        # già in Storico
        return (False, 0)

    try:
        _copy_file(src, dst)
        try:
            src.unlink()
        except Exception:
            pass
        return (True, 1)
    except Exception:
        logging.exception("Storico safe move fallito %s → %s", src, dst)
        return (False, 8)

def write_lines(p: Path, lines: List[str]):
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

# ---- MAPPATURE, VALIDAZIONI E LOG WRITERS --------------------------------------------

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

def map_location(m: re.Match, cfg: Config) -> dict:
    first = m.group(3)[0]          # prima cifra delle 6
    l2 = m.group(2).upper()        # lettera location

    loc = (
        LOCATION_MAP.get((l2, first))
        or LOCATION_MAP.get(("*", first))
        or LOCATION_MAP.get((l2, "*"))
        or DEFAULT_LOCATION
    )

    folder, log_name, subloc, doctype, lang = loc
    arch_tif_loc = m.group(1).upper() + subloc
    dir_tif_loc = cfg.ARCHIVIO_DISEGNI / folder / arch_tif_loc
    return dict(folder=folder, log_name=log_name, subloc=subloc, doctype=doctype, lang=lang,
                arch_tif_loc=arch_tif_loc, dir_tif_loc=dir_tif_loc)

def size_from_letter(ch: str) -> str:
    return dict(A="A4", B="A3", C="A2", D="A1", E="A0").get(ch.upper(), "A4")

def uom_from_letter(ch: str) -> str:
    return dict(N="(Not applicable)", M="Metric", I="Inch", D="Dual").get(ch.upper(), "Metric")

# ---- ORIENTAMENTO TIFF ---------------------------------------------------------------

def _tiff_read_size_vfast(path: Path) -> Optional[Tuple[int,int]]:
    import struct
    try:
        with open(path, 'rb') as f:
            hdr = f.read(8)
            if len(hdr) < 8:
                return None
            endian = hdr[:2]
            if endian == b'II':
                u16 = lambda b: struct.unpack('<H', b)[0]
                u32 = lambda b: struct.unpack('<I', b)[0]
            elif endian == b'MM':
                u16 = lambda b: struct.unpack('>H', b)[0]
                u32 = lambda b: struct.unpack('>I', b)[0]
            else:
                return None
            if u16(hdr[2:4]) != 42:
                return None
            ifd_off = u32(hdr[4:8])
            f.seek(ifd_off)
            nbytes = f.read(2)
            if len(nbytes) < 2:
                return None
            n = u16(nbytes)
            TAG_W, TAG_H = 256, 257
            TYPE_SIZES = {1:1,2:1,3:2,4:4,5:8,7:1,9:4,10:8}
            w = h = None
            for _ in range(n):
                ent = f.read(12)
                if len(ent) < 12:
                    break
                tag = u16(ent[0:2]); typ = u16(ent[2:4]); cnt = u32(ent[4:8]); val = ent[8:12]
                unit = TYPE_SIZES.get(typ)
                if not unit:
                    continue
                datasz = unit * cnt
                if datasz <= 4:
                    if typ == 3: v = u16(val[0:2])
                    elif typ == 4: v = u32(val)
                    else: continue
                else:
                    off = u32(val); cur = f.tell()
                    f.seek(off); raw = f.read(unit); f.seek(cur)
                    if typ == 3: v = u16(raw)
                    elif typ == 4: v = u32(raw)
                    else: continue
                if tag == TAG_W: w = v
                elif tag == TAG_H: h = v
                if w is not None and h is not None:
                    return (w, h)
    except Exception:
        return None
    return None

def check_orientation_ok(tif_path: Path) -> bool:
    if tif_path.suffix.lower() == ".pdf":
        return True
    wh = _tiff_read_size_vfast(tif_path)
    if wh is None:
        return True
    w, h = wh
    return w > h

# ---- LOG WRAPPERS / UI PHASE --------------------------------------------------------

def _now_ddmonYYYY() -> str:
    return datetime.now().strftime("%d.%b.%Y")
def _now_HHMMSS() -> str:
    return datetime.now().strftime("%H:%M:%S")

def log_swarky(cfg: Config, file_name: str, loc: str, process: str,
               archive_dwg: str = "", dest: str = ""):
    line = f"{_now_ddmonYYYY()} # {_now_HHMMSS()} # {file_name}\t# {loc}\t# {process}\t# {archive_dwg}"
    _append_filelog_line(line)  # TXT batch
    logging.info("processed %s", file_name,
                 extra={"ui": ("processed", file_name, process, archive_dwg, dest)})

def log_error(cfg: Config, file_name: str, err: str, archive_dwg: str = ""):
    line = f"{_now_ddmonYYYY()} # {_now_HHMMSS()} # {file_name}\t# ERRORE\t# {err}\t# {archive_dwg}"
    _append_filelog_line(line)  # TXT batch
    logging.error("anomaly %s", file_name,
                  extra={"ui": ("anomaly", file_name, err)})

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
                     extra={"ui": ("phase_done", self.label, elapsed_ms)})
        return False

def ui_phase(label: str) -> _UIPhase:
    return _UIPhase(label)

# ---- EDI WRITER ----------------------------------------------------------------------

def _edi_body(
    *,
    document_no: str,
    rev: str,
    sheet: str,
    description: str,
    actual_size: str,
    uom: str,
    doctype: str,
    lang: str,
    file_name: str,
    file_type: str,
    order_number: str = "",
    now: Optional[str] = None
) -> List[str]:
    now = now or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = [
        "[Database]",
        "ServerName=ORMDB33",
        "ProjectName=FPD Engineering",
        "[DatabaseFields]",
        f"DocumentNo={document_no}",
        f"DocumentRev={rev}",
        f"SheetNumber={sheet}",
        f"Description={description}",
        f"ActualSize={actual_size}",
        "PumpModel=(UNKNOWN)",
        "OEM=Flowserve",
        "PumpSize=",
        f"OrderNumber={order_number}",
        "SerialNumber=",
        f"Document_Type={doctype}",
        "DrawingClass=COMMERCIAL",
        "DesignCenter=Desio, Italy",
        "OEMSite=Desio, Italy",
        "OEMDrawingNumber=",
        f"UOM={uom}",
        f"DWGLanguage={lang}",
        "CurrentRevision=Y",
        "EnteredBy=10150286",
        "Notes=",
        "NonEnglishDesc=",
        "SupersededBy=",
        "NumberOfStages=",
        "[DrawingInfo]",
        f"DocumentNo={document_no}",
        f"SheetNumber={sheet}",
        ("Document_Type=Detail" if doctype == "DETAIL" else "Document_Type=Customer Drawings"),
        f"DocumentRev={rev}",
        f"FileName={file_name}",
        f"FileType={file_type}",
        f"Currentdate={now}",
    ]
    return header

def write_edi(
    file_name: str,
    out_dir: Path,
    *,
    m: Optional[re.Match] = None,
    iss_match: Optional[re.Match] = None,
    loc: Optional[dict] = None,
    iss_style: bool = False,
    document_no_override: Optional[str] = None,
    order_number: Optional[str] = None,
    rev_override: Optional[str] = None,
    sheet_override: Optional[str] = None,
) -> None:

    edi = out_dir / (Path(file_name).stem + ".DESEDI")
    if edi.exists():
        return
    if iss_match is not None:
        g1 = iss_match.group(1); g2 = iss_match.group(2); g3 = iss_match.group(3)
        rev = iss_match.group(4); sheet = iss_match.group(5)
        docno = f"G{g1}{g2}{g3}"
        body = _edi_body(
            document_no=docno, rev=rev, sheet=sheet,
            description=" Impeller Specification Sheet",
            actual_size="A4", uom="Metric", doctype="DETAIL", lang="English",
            file_name=file_name, file_type="Pdf",
            order_number=(order_number or ""),
        )
        write_lines(edi, body)
        return
    if iss_style:
        if m is None and (document_no_override is None or rev_override is None or sheet_override is None):
            raise ValueError("write_edi: iss_style richiede 'm' oppure tutti gli override (document_no, rev, sheet)")

        if document_no_override is not None:
            document_no = document_no_override
            rev = rev_override or ""
            sheet = sheet_override or ""
        else:
            assert m is not None
            document_no = f"D{m.group(1)}{m.group(2)}{m.group(3)}"
            rev = rev_override or m.group(4)
            sheet = sheet_override or m.group(5)

        ext = Path(file_name).suffix.lower()
        file_type = "Pdf" if ext == ".pdf" else "Tiff"

        body = _edi_body(
            document_no=document_no,
            rev=rev,
            sheet=sheet,
            description=" Impeller Specification Sheet",
            actual_size="A4",
            uom="Metric",
            doctype="DETAIL",
            lang="English",
            file_name=file_name,
            file_type=file_type,
            order_number=(order_number or ""),
        )
        write_lines(edi, body)
        return
        
    if m is None or loc is None:
        raise ValueError("write_edi: per STANDARD/FIV servono 'm' (BASE_NAME) e 'loc' (map_location)")
    document_no = f"D{m.group(1)}{m.group(2)}{m.group(3)}"
    rev = m.group(4); sheet = m.group(5)
    file_type = "Pdf" if Path(file_name).suffix.lower() == ".pdf" else "Tiff"
    body = _edi_body(
        document_no=document_no, rev=rev, sheet=sheet, description="",
        actual_size=size_from_letter(m.group(1)), uom=uom_from_letter(m.group(6)),
        doctype=loc["doctype"], lang=loc["lang"],
        file_name=file_name, file_type=file_type,
    )
    write_lines(edi, body)

# ---- STORICO: routing helper --------------------------------------------------------

def _storico_dest_dir_for_name(cfg: Config, nm: str) -> Path:
    mm = BASE_NAME.fullmatch(nm)
    if not mm:
        return cfg.ARCHIVIO_STORICO / "unknown"
    return cfg.ARCHIVIO_STORICO / f"D{mm.group(1).upper()}"

# ---- CONFIG LOADER -------------------------------------------------------------------

def load_config(path: Path) -> Config:
    if not path.exists():
        raise FileNotFoundError(f"Config non trovato: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return Config.from_json(data)
