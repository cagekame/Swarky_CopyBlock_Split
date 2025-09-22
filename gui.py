#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import sys, os, json, time, logging, re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple

# Solo Windows
if sys.platform != "win32":
    raise RuntimeError("Questo programma è supportato solo su Windows.")

# ====== LOGICA (no I/O) =====================================================
from swarky_logic import (
    BASE_NAME,
    ISS_BASENAME,
    ui_phase,
    log_error,
    log_swarky,
    map_location,
    parse_prefixed,          # parse dei nomi (no I/O)
    build_edi_document,      # costruzione corpo EDI (standard/ISS)
)

# ====== I/O (solo operazioni su FS) =========================================
from swarky_io import (
    iter_candidates,         # scandisce i candidati in cartella
    check_orientation_ok,    # lettura header tiff veloce
    move_to,                 # move con fallback copy+delete
    move_to_storico_safe,    # move "safe" senza overwrite (usa O_EXCL / rename)
    fast_copy_or_link,       # hardlink->copy a blocchi
    list_same_doc_prefisso,  # enumerate mirata docno* sul FS
    write_edi,               # scrive file .DESEDI
    write_lines,             # append testo su file
)

# ---- CONFIG ----------------------------------------------------------------
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

# ---- FILE LOG TXT (batch) + logging base -----------------------------------
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
    new_handlers = [h for h in root.handlers if not isinstance(h, logging.FileHandler)]
    new_handlers.append(fh)
    root.handlers = new_handlers

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

def _now_ddmonYYYY() -> str: return datetime.now().strftime("%d.%b.%Y")
def _now_HHMMSS() -> str:   return datetime.now().strftime("%H:%M:%S")

def _txt_processed(file_name: str, loc: str, process: str, archive_dwg: str="") -> str:
    return f"{_now_ddmonYYYY()} # {_now_HHMMSS()} # {file_name}\t# {loc}\t# {process}\t# {archive_dwg}"

def _txt_error(file_name: str, err: str, archive_dwg: str="") -> str:
    return f"{_now_ddmonYYYY()} # {_now_HHMMSS()} # {file_name}\t# ERRORE\t# {err}\t# {archive_dwg}"

# ---- SUPPORTI -----------------------------------------------------------------------
def _storico_dest_dir_for_name(cfg: Config, nm: str) -> Path:
    m = BASE_NAME.fullmatch(nm)
    if not m:
        return cfg.ARCHIVIO_STORICO / "unknown"
    return cfg.ARCHIVIO_STORICO / f"D{m.group(1).upper()}"

def _docno_from_match(m: re.Match) -> str:
    return f"D{m.group(1)}{m.group(2)}{m.group(3)}"

# ---- PIPELINE -----------------------------------------------------------------------
def _process_candidate(p: Path, cfg: Config) -> bool:
    try:
        # Normalizzazione estensione
        suf = p.suffix
        if suf == ".TIF":
            try: p = p.rename(p.with_suffix(".tif")) or p.with_suffix(".tif")
            except Exception: pass
        elif suf.lower() == ".tiff":
            try: p = p.rename(p.with_suffix(".tif")) or p.with_suffix(".tif")
            except Exception: pass

        name = p.name

        # Orientamento
        with ui_phase(f"{name} • orientamento"):
            if not check_orientation_ok(p):
                _append_filelog_line(_txt_error(name, "Immagine Girata"))
                log_error(name, "Immagine Girata")
                move_to(p, cfg.ERROR_DIR)
                return True

        # Regex + validazioni
        with ui_phase(f"{name} • regex+validate"):
            m = BASE_NAME.fullmatch(name)
            if not m:
                _append_filelog_line(_txt_error(name, "Nome File Errato"))
                log_error(name, "Nome File Errato"); move_to(p, cfg.ERROR_DIR); return True
            if m.group(1).upper() not in "ABCDE":
                _append_filelog_line(_txt_error(name, "Formato Errato"))
                log_error(name, "Formato Errato"); move_to(p, cfg.ERROR_DIR); return True
            if m.group(2).upper() not in "MKFTESNP":
                _append_filelog_line(_txt_error(name, "Location Errata"))
                log_error(name, "Location Errata"); move_to(p, cfg.ERROR_DIR); return True
            if m.group(6).upper() not in "MIDN":
                _append_filelog_line(_txt_error(name, "Metrica Errata"))
                log_error(name, "Metrica Errata"); move_to(p, cfg.ERROR_DIR); return True

        new_rev    = m.group(4)
        new_sheet  = m.group(5)
        new_metric = m.group(6).upper()
        MI = {"M","I"}; DN = {"D","N"}
        new_group = "MI" if new_metric in MI else "DN"
        new_rev_i = int(new_rev)

        # Mappatura destinazione
        with ui_phase(f"{name} • map_location"):
            loc = map_location(m, cfg)
            dir_tif_loc = loc["dir_tif_loc"]
            tiflog      = loc["log_name"]

        # Elenco stessi docno (I/O mirato) + parse (logica)
        with ui_phase(f"{name} • list_same_doc_prefisso"):
            docno = _docno_from_match(m)
            names_all = list_same_doc_prefisso(dir_tif_loc, docno)  # tuple di nomi sul FS
            same_doc = parse_prefixed(tuple(n for n in names_all if n.lower().endswith((".tif", ".pdf"))))

        with ui_phase(f"{name} • derive_same_sheet"):
            same_sheet = [(r, nm, met, sh) for (r, nm, met, sh) in same_doc if sh == new_sheet]

        # Pari revisione
        with ui_phase(f"{name} • check_same_filename"):
            if any((nm == name and r == new_rev) for (r, nm, met, sh) in same_sheet):
                _append_filelog_line(_txt_error(name, "Pari Revisione"))
                log_error(name, "Pari Revisione"); move_to(p, cfg.PARI_REV_DIR); return True

        same_sheet_mi = [(int(r), nm, met) for (r, nm, met, sh) in same_sheet if met in MI]
        same_sheet_dn = [(int(r), nm, met) for (r, nm, met, sh) in same_sheet if met in DN]
        same_sheet_same_metric = [(int(r), nm, met) for (r, nm, met, sh) in same_sheet if met == new_metric]

        def _max_rev(entries: List[Tuple[int,str,str]]) -> Optional[int]:
            return max((rv for (rv, _, _) in entries), default=None)

        max_mi = _max_rev(same_sheet_mi)
        max_dn = _max_rev(same_sheet_dn)
        own_max = _max_rev(same_sheet_same_metric)

        # Rev precedenti (altro gruppo)
        other_entries = same_sheet_dn if new_group == "MI" else same_sheet_mi
        other_max = max_dn if new_group == "MI" else max_mi
        if other_max is not None and new_rev_i < other_max:
            ref = next((nm for (rv, nm, _met) in other_entries if rv == other_max), "")
            _append_filelog_line(_txt_error(name, "Revisione Precendente", ref))
            log_error(name, "Revisione Precendente", ref); move_to(p, cfg.ERROR_DIR); return True

        # Rev precedenti (stessa metrica)
        if own_max is not None and new_rev_i < own_max:
            ref = next((nm for (rv, nm, _met) in same_sheet_same_metric if rv == own_max), "")
            _append_filelog_line(_txt_error(name, "Revisione Precendente", ref))
            log_error(name, "Revisione Precendente", ref); move_to(p, cfg.ERROR_DIR); return True

        # Conflitti pari rev
        same_rev_mi = [(rv, nm, met) for (rv, nm, met) in same_sheet_mi if rv == new_rev_i]
        same_rev_dn = [(rv, nm, met) for (rv, nm, met) in same_sheet_dn if rv == new_rev_i]

        if new_group == "MI":
            if same_rev_dn:
                ref = same_rev_dn[0][1]
                _append_filelog_line(_txt_error(name, "Conflitto Metrica (DN a pari revisione)", ref))
                log_error(name, "Conflitto Metrica (DN a pari revisione)", ref); move_to(p, cfg.ERROR_DIR); return True
            other_mi = next((nm for (_rv, nm, met) in same_rev_mi if met != new_metric), None)
            if other_mi:
                _append_filelog_line(_txt_processed(name, tiflog, "Metrica Diversa", other_mi))
                log_swarky(name, tiflog, "Metrica Diversa", other_mi)
        else:
            if same_rev_mi:
                ref = same_rev_mi[0][1]
                _append_filelog_line(_txt_error(name, "Conflitto Metrica (MI a pari revisione)", ref))
                log_error(name, "Conflitto Metrica (MI a pari revisione)", ref); move_to(p, cfg.ERROR_DIR); return True
            other_dn = next((nm for (_rv, nm, met) in same_rev_dn if met != new_metric), None)
            if other_dn:
                _append_filelog_line(_txt_error(name, "Conflitto Metrica (D/N a pari revisione)", other_dn))
                log_error(name, "Conflitto Metrica (D/N a pari revisione)", other_dn); move_to(p, cfg.ERROR_DIR); return True

        # Accettazione nuovo
        with ui_phase(f"{name} • move_to_archivio"):
            move_to(p, dir_tif_loc)
            new_path = dir_tif_loc / name

        # Storico
        to_storico_same: list[tuple[Path, Path, str]] = []
        to_storico_other: list[tuple[Path, Path, str]] = []
        if own_max is None or new_rev_i > own_max:
            for rv, nm, _met in same_sheet_same_metric:
                if rv < new_rev_i:
                    to_storico_same.append((dir_tif_loc / nm, _storico_dest_dir_for_name(cfg, nm), nm))
        if other_max is not None and new_rev_i > other_max:
            for rv, nm, _met in other_entries:
                if rv < new_rev_i:
                    to_storico_other.append((dir_tif_loc / nm, _storico_dest_dir_for_name(cfg, nm), nm))

        if to_storico_same:
            with ui_phase(f"{name} • move_old_revs_same_metric"):
                for old_path, dest_dir, nm in to_storico_same:
                    try:
                        copied, rc = move_to_storico_safe(old_path, dest_dir)
                        if rc >= 8:
                            logging.exception("Storico (same metric) errore: %s → %s", old_path, dest_dir)
                        elif copied:
                            _append_filelog_line(_txt_processed(name, tiflog, "Rev superata", nm))
                            log_swarky(name, tiflog, "Rev superata", nm)
                        else:
                            _append_filelog_line(_txt_error(nm, "Presente in Storico"))
                            log_error(nm, "Presente in Storico")
                            try: move_to(old_path, cfg.ERROR_DIR)
                            except FileNotFoundError: pass
                    except Exception as e:
                        logging.exception("Storico (same metric): %s → %s: %s", old_path, dest_dir, e)

        if to_storico_other:
            with ui_phase(f"{name} • move_old_revs_other_group"):
                for old_path, dest_dir, nm in to_storico_other:
                    try:
                        copied, rc = move_to_storico_safe(old_path, dest_dir)
                        if rc >= 8:
                            logging.exception("Storico (other grp) errore: %s → %s", old_path, dest_dir)
                        elif copied:
                            _append_filelog_line(_txt_processed(name, tiflog, "Rev superata", nm))
                            log_swarky(name, tiflog, "Rev superata", nm)
                        else:
                            _append_filelog_line(_txt_error(nm, "Presente in Storico"))
                            log_error(nm, "Presente in Storico")
                            try: move_to(old_path, cfg.ERROR_DIR)
                            except FileNotFoundError: pass
                    except Exception as e:
                        logging.exception("Storico (other grp): %s → %s: %s", old_path, dest_dir, e)

        # PLM + EDI
        with ui_phase(f"{name} • link/copy_to_PLM"):
            try:
                fast_copy_or_link(new_path, cfg.PLM_DIR / name)
            except Exception as e:
                logging.exception("PLM copy/link fallita per %s: %s", new_path, e)

        with ui_phase(f"{name} • write_EDI"):
            try:
                file_type = "Pdf" if new_path.suffix.lower() == ".pdf" else "Tiff"
                body = build_edi_document(match=m, loc=loc, file_name=name, file_type=file_type)
                write_edi(file_name=name, out_dir=cfg.PLM_DIR, body_lines=body)
            except Exception as e:
                logging.exception("Impossibile creare DESEDI per %s: %s", name, e)

        _append_filelog_line(_txt_processed(name, tiflog, "Archiviato"))
        log_swarky(name, tiflog, "Archiviato", "", dest=tiflog)
        return True

    except Exception:
        logging.exception("Errore inatteso per %s", p)
        return False

# ---- ISS / FIV ----------------------------------------------------------------------
def iss_loading(cfg: Config) -> bool:
    did = False
    try:
        files = [p for p in cfg.DIR_ISS.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"]
    except Exception as e:
        logging.exception("ISS: impossibile leggere la cartella %s: %s", cfg.DIR_ISS, e)
        return False

    for p in files:
        m = ISS_BASENAME.fullmatch(p.name)
        if not m:
            _append_filelog_line(_txt_error(p.name, "Nome ISS Errato"))
            log_error(p.name, "Nome ISS Errato")
            continue
        try:
            move_to(p, cfg.PLM_DIR)
            body = build_edi_document(match=m, file_name=p.name, scheme="iss")
            write_edi(file_name=p.name, out_dir=cfg.PLM_DIR, body_lines=body)
            _append_filelog_line(_txt_processed(p.name, "ISS", "ISS"))
            log_swarky(p.name, "ISS", "ISS", "", "")
            did = True
        except Exception as e:
            logging.exception("Impossibile processare ISS %s: %s", p.name, e)
        try:
            now = datetime.now()
            write_lines(cfg.DIR_ISS / "SwarkyISS.log",
                        [f"{now.strftime('%d.%b.%Y')} # {now.strftime('%H:%M:%S')} # {p.stem}"])
        except Exception:
            logging.exception("ISS: impossibile aggiornare SwarkyISS.log")

    return did

def fiv_loading(cfg: Config) -> bool:
    did = False
    try:
        files = [p for p in cfg.DIR_FIV_LOADING.iterdir() if p.is_file()]
    except Exception as e:
        logging.exception("FIV: lettura cartella fallita: %s", e)
        return False

    for p in files:
        ext = p.suffix.lower()
        if ext not in (".tif", ".tiff") and not (cfg.ACCEPT_PDF and ext == ".pdf"):
            continue
        m = BASE_NAME.fullmatch(p.name)
        if not m:
            _append_filelog_line(_txt_error(p.name, "Nome FIV Errato"))
            log_error(p.name, "Nome FIV Errato")
            continue
        loc = map_location(m, cfg)
        try:
            body = build_edi_document(
                match=m,
                loc=loc,
                file_name=p.name,
                file_type=("Pdf" if ext == ".pdf" else "Tiff"),
            )
            write_edi(file_name=p.name, out_dir=cfg.PLM_DIR, body_lines=body)
            move_to(p, cfg.PLM_DIR)
            _append_filelog_line(_txt_processed(p.name, "FIV", "FIV loading"))
            log_swarky(p.name, "FIV", "FIV loading", "", "")
            did = True
        except Exception as e:
            logging.exception("Impossibile processare FIV %s: %s", p.name, e)

    return did

# ---- STATS --------------------------------------------------------------------------
_LAST_STATS_TS: float = 0.0

def _count_files_quick(d: Path, exts: tuple[str, ...]) -> int:
    try:
        with os.scandir(d) as it:
            return sum(1 for de in it if de.is_file() and os.path.splitext(de.name)[1].lower() in exts)
    except (OSError, FileNotFoundError):
        return 0

def _stats_interval_sec() -> int:
    val = os.environ.get("SWARKY_STATS_EVERY", "300")
    try:
        n = int(val)
    except Exception:
        n = 300
    return n if n >= 10 else 10

def _should_emit_stats() -> bool:
    global _LAST_STATS_TS
    now = time.monotonic()
    if now - _LAST_STATS_TS >= _stats_interval_sec():
        _LAST_STATS_TS = now
        return True
    return False

def count_tif_files(cfg: Config) -> dict:
    return {
        "Same Rev Dwg": _count_files_quick(cfg.PARI_REV_DIR, (".tif", ".pdf")),
        "Check Dwg": _count_files_quick(cfg.ERROR_DIR, (".tif", ".pdf")),
        "Heng Dwg": _count_files_quick(cfg.DIR_HENGELO, (".tif", ".pdf")),
        "Tab Dwg": _count_files_quick(cfg.DIR_TABELLARI, (".tif", ".pdf")),
        "Plm error Dwg": _count_files_quick(cfg.DIR_PLM_ERROR, (".tif", ".pdf")),
    }

# ---- LOOP ---------------------------------------------------------------------------
def run_once(cfg: Config) -> bool:
    start_all = time.time()

    with ui_phase("Scan candidati (hplotter)"):
        candidates: List[Path] = list(iter_candidates(cfg.DIR_HPLOTTER, cfg.ACCEPT_PDF))

    did_something = False
    for p in candidates:
        try:
            did_something |= _process_candidate(p, cfg)
        except Exception:
            logging.exception("Errore nel processing")

    did_arch = did_something
    did_iss  = iss_loading(cfg)
    did_fiv  = fiv_loading(cfg)

    elapsed_all = time.time() - start_all
    minutes = int(elapsed_all // 60)
    seconds = int(elapsed_all % 60)
    _append_filelog_line(f"ProcessTime # {minutes:02d}:{seconds:02d}")

    _flush_file_log(cfg)

    if logging.getLogger().isEnabledFor(logging.DEBUG) and _should_emit_stats():
        logging.debug("Counts: %s", count_tif_files(cfg))

    return did_arch or did_iss or did_fiv

def watch_loop(cfg: Config, interval: int):
    logging.info("Watch ogni %ds...", interval)
    while True:
        run_once(cfg); time.sleep(interval)

# ---- CLI ---------------------------------------------------------------------------
def parse_args(argv: List[str]):
    import argparse
    ap = argparse.ArgumentParser(description="Swarky - batch archiviazione/EDI")
    ap.add_argument("--watch", type=int, default=0, help="Loop di polling in secondi, 0=una sola passata")
    return ap.parse_args(argv)

def load_config(path: Path) -> Config:
    if not path.exists():
        raise FileNotFoundError(f"Config non trovato: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return Config.from_json(data)

def main(argv: List[str]):
    args = parse_args(argv)
    cfg = load_config(Path("config.json"))
    setup_logging(cfg)

    if args.watch > 0:
        watch_loop(cfg, args.watch)
    else:
        run_once(cfg)

if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except KeyboardInterrupt:
        print("Interrotto")
