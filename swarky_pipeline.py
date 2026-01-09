#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import time
import logging
from pathlib import Path
from typing import List, Tuple, Optional

from swarky_core import (
    Config,
    BASE_NAME,
    map_location,
    check_orientation_ok,
    log_swarky,
    log_error,
    ui_phase,
    write_edi,
    _storico_dest_dir_for_name,
    move_to,
    move_to_storico_safe,
    _copy_file,
    _flush_file_log,
    _list_same_doc_prefisso,
    _list_nonstandard_same_doc,
    _append_filelog_line,
)

from swarky_iss_fiv import iss_loading, fiv_loading, heng_loading


# ---- PIPELINE PRINCIPALE (Hplotter → Archivio) --------------------------------------


def _iter_candidates(dirp: Path, accept_pdf: bool):
    exts = {".tif"}
    if accept_pdf:
        exts.add(".pdf")
    with os.scandir(dirp) as it:
        for de in it:
            if de.is_file():
                suf = os.path.splitext(de.name)[1].lower()
                if suf in exts:
                    yield Path(de.path)


def _process_candidate(p: Path, cfg: Config) -> bool:
    try:
        # --- normalizzazione estensione on-the-fly ---
        suf = p.suffix
        if suf == ".TIF":
            q = p.with_suffix(".tif")
            try:
                p.rename(q)
                p = q
            except Exception:
                pass
        elif suf.lower() == ".tiff":
            q = p.with_suffix(".tif")
            try:
                p.rename(q)
                p = q
            except Exception:
                pass

        name = p.name

        # ---- ORIENTAMENTO: subito in testa ----
        with ui_phase(f"{name} • Verse"):
            if not check_orientation_ok(p):
                log_error(cfg, name, "Immagine Girata")
                move_to(p, cfg.ERROR_DIR)
                return True

        # ---- Regex + validazioni ----
        with ui_phase(f"{name} • RE_Validate"):
            m = BASE_NAME.fullmatch(name)
            if not m:
                log_error(cfg, name, "Nome File Errato")
                move_to(p, cfg.ERROR_DIR)
                return True
            if m.group(1).upper() not in "ABCDE":
                log_error(cfg, name, "Formato Errato")
                move_to(p, cfg.ERROR_DIR)
                return True
            if m.group(2).upper() not in "MKFTESNP":
                log_error(cfg, name, "Location Errata")
                move_to(p, cfg.ERROR_DIR)
                return True
            if m.group(6).upper() not in "MIDN":
                log_error(cfg, name, "Metrica Errata")
                move_to(p, cfg.ERROR_DIR)
                return True

        new_rev = m.group(4)
        new_sheet = m.group(5)
        new_metric = m.group(6).upper()
        MI = {"M", "I"}
        DN = {"D", "N"}
        new_group = "MI" if new_metric in MI else "DN"
        new_rev_i = int(new_rev)

        # ---- Mappatura destinazione archivio ----
        with ui_phase(f"{name} • Map_Loc"):
            loc = map_location(m, cfg)
            dir_tif_loc = loc["dir_tif_loc"]
            tiflog = loc["log_name"]

        # ---- Elenco file con stesso DOCNO ----
        with ui_phase(f"{name} • Prefisso"):
            same_doc = _list_same_doc_prefisso(dir_tif_loc, m)

        with ui_phase(f"{name} • Sheet"):
            same_sheet = [(r, nm, met, sh) for (r, nm, met, sh) in same_doc if sh == new_sheet]

        # ---- Pari revisione (verifica via lista) ----
        with ui_phase(f"{name} • Filename"):
            if any((nm == name and r == new_rev) for (r, nm, met, sh) in same_sheet):
                log_error(cfg, name, "Pari Revisione")
                move_to(p, cfg.PARI_REV_DIR)
                return True

        # ---- Partizionamento e max rev ----
        same_sheet_mi = [(int(r), nm, met) for (r, nm, met, sh) in same_sheet if met in MI]
        same_sheet_dn = [(int(r), nm, met) for (r, nm, met, sh) in same_sheet if met in DN]
        same_sheet_same_metric = [(int(r), nm, met) for (r, nm, met, sh) in same_sheet if met == new_metric]

        def _max_rev(entries: List[Tuple[int, str, str]]) -> Optional[int]:
            return max((rv for (rv, _, _) in entries), default=None)

        max_mi = _max_rev(same_sheet_mi)
        max_dn = _max_rev(same_sheet_dn)
        own_max = _max_rev(same_sheet_same_metric)

        # ---- Revisioni precedenti rispetto all'altro gruppo ----
        other_entries = same_sheet_dn if new_group == "MI" else same_sheet_mi
        other_max = max_dn if new_group == "MI" else max_mi
        if other_max is not None and new_rev_i < other_max:
            ref = next((nm for (rv, nm, _met) in other_entries if rv == other_max), "")
            log_error(cfg, name, "Revisione Precendente", ref)
            move_to(p, cfg.ERROR_DIR)
            return True

        # ---- Revisioni precedenti rispetto stessa metrica ----
        if own_max is not None and new_rev_i < own_max:
            ref = next((nm for (rv, nm, _met) in same_sheet_same_metric if rv == own_max), "")
            log_error(cfg, name, "Revisione Precendente", ref)
            move_to(p, cfg.ERROR_DIR)
            return True

        # ---- Conflitti pari rev tra gruppi/metrica ----
        same_rev_mi = [(rv, nm, met) for (rv, nm, met) in same_sheet_mi if rv == new_rev_i]
        same_rev_dn = [(rv, nm, met) for (rv, nm, met) in same_sheet_dn if rv == new_rev_i]

        if new_group == "MI":
            if same_rev_dn:
                ref = same_rev_dn[0][1]
                log_error(cfg, name, "Conflitto Metrica (DN a pari revisione)", ref)
                move_to(p, cfg.ERROR_DIR)
                return True
            other_mi = next((nm for (_rv, nm, met) in same_rev_mi if met != new_metric), None)
            if other_mi:
                log_swarky(cfg, name, tiflog, "Metrica Diversa", other_mi)
        else:
            if same_rev_mi:
                ref = same_rev_mi[0][1]
                log_error(cfg, name, "Conflitto Metrica (MI a pari revisione)", ref)
                move_to(p, cfg.ERROR_DIR)
                return True
            other_dn = next((nm for (_rv, nm, met) in same_rev_dn if met != new_metric), None)
            if other_dn:
                log_error(cfg, name, "Conflitto Metrica (D/N a pari revisione)", other_dn)
                move_to(p, cfg.ERROR_DIR)
                return True

        # ---- ACCETTAZIONE del NUOVO ----
        with ui_phase(f"{name} • Archivio"):
            move_to(p, dir_tif_loc)
            new_path = dir_tif_loc / name

        # ---- LEGACY/NON STANDARD → STORICO (igiene archivio) ----
        with ui_phase(f"{name} • Non_Std_Legacy"):
            legacy = _list_nonstandard_same_doc(dir_tif_loc, m)
            for nm in legacy:
                old_path = dir_tif_loc / nm
                dest_dir = _storico_dest_dir_for_name(cfg, nm)
                try:
                    copied, rc = move_to_storico_safe(old_path, dest_dir)
                    if rc >= 8:
                        logging.exception("Storico (legacy) errore: %s → %s", old_path, dest_dir)
                    elif copied:
                        log_swarky(cfg, name, tiflog, "Legacy non standard", nm, "Storico")
                    else:
                        # già presente nello Storico → manda l’originale in ERROR_DIR
                        log_error(cfg, nm, "Presente in Storico")
                        try:
                            move_to(old_path, cfg.ERROR_DIR)
                        except FileNotFoundError:
                            pass
                except Exception as e:
                    logging.exception("Storico (legacy): %s → %s: %s", old_path, dest_dir, e)

        # ---- STORICIZZAZIONI (dopo l'accettazione) ----
        to_storico_same: List[Tuple[Path, Path, str]] = []
        to_storico_other: List[Tuple[Path, Path, str]] = []
        if own_max is None or new_rev_i > own_max:
            for rv, nm, _met in same_sheet_same_metric:
                if rv < new_rev_i:
                    to_storico_same.append((dir_tif_loc / nm, _storico_dest_dir_for_name(cfg, nm), nm))
        if other_max is not None and new_rev_i > other_max:
            for rv, nm, _met in other_entries:
                if rv < new_rev_i:
                    to_storico_other.append((dir_tif_loc / nm, _storico_dest_dir_for_name(cfg, nm), nm))

        if to_storico_same:
            with ui_phase(f"{name} • Move_Same_Sheet"):
                for old_path, dest_dir, nm in to_storico_same:
                    try:
                        copied, rc = move_to_storico_safe(old_path, dest_dir)
                        if rc >= 8:
                            logging.exception("Storico (same metric) errore: %s → %s", old_path, dest_dir)
                        elif copied:
                            log_swarky(cfg, name, tiflog, "Rev superata", nm, "Storico")
                        else:
                            log_error(cfg, nm, "Presente in Storico")
                            try:
                                move_to(old_path, cfg.ERROR_DIR)
                            except FileNotFoundError:
                                pass
                    except Exception as e:
                        logging.exception("Storico (same metric): %s → %s: %s", old_path, dest_dir, e)

        if to_storico_other:
            with ui_phase(f"{name} • Move_other"):
                for old_path, dest_dir, nm in to_storico_other:
                    try:
                        copied, rc = move_to_storico_safe(old_path, dest_dir)
                        if rc >= 8:
                            logging.exception("Storico (other grp) errore: %s → %s", old_path, dest_dir)
                        elif copied:
                            log_swarky(cfg, name, tiflog, "Rev superata", nm, "Storico")
                        else:
                            log_error(cfg, nm, "Presente in Storico")
                            try:
                                move_to(old_path, cfg.ERROR_DIR)
                            except FileNotFoundError:
                                pass
                    except Exception as e:
                        logging.exception("Storico (other grp): %s → %s: %s", old_path, dest_dir, e)

        # ---- PLM + EDI ----
        with ui_phase(f"{name} • To_PLM"):
            try:
                _copy_file(new_path, cfg.PLM_DIR / name)
            except Exception as e:
                logging.exception("PLM copy fallita per %s: %s", new_path, e)

        with ui_phase(f"{name} • Write_EDI"):
            try:
                write_edi(cfg, name, cfg.PLM_DIR, m=m, loc=loc)
            except Exception as e:
                logging.exception("Impossibile creare DESEDI per %s: %s", name, e)

        log_swarky(cfg, name, tiflog, "Archiviato", "", dest=tiflog)
        return True

    except Exception:
        logging.exception("Errore inatteso per %s", p)
        return False


# ---- STATS ---------------------------------------------------------------------------

_LAST_STATS_TS: float = 0.0


def _count_files_quick(d: Path, exts: tuple[str, ...]) -> int:
    try:
        with os.scandir(d) as it:
            return sum(
                1
                for de in it
                if de.is_file() and os.path.splitext(de.name)[1].lower() in exts
            )
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
        candidates: List[Path] = list(_iter_candidates(cfg.DIR_HPLOTTER, cfg.ACCEPT_PDF))

    did_something = False
    for p in candidates:
        try:
            did_something |= _process_candidate(p, cfg)
        except Exception:
            logging.exception("Errore nel processing")

    did_arch = did_something
    did_iss  = iss_loading(cfg)
    did_fiv  = fiv_loading(cfg)
    did_heng = heng_loading(cfg)

    elapsed_all = time.time() - start_all
    minutes = int(elapsed_all // 60)
    seconds = int(elapsed_all % 60)
    _append_filelog_line(f"ProcessTime # {minutes:02d}:{seconds:02d}")

    _flush_file_log(cfg)

    if logging.getLogger().isEnabledFor(logging.DEBUG) and _should_emit_stats():
        logging.debug("Counts: %s", count_tif_files(cfg))

    return did_arch or did_iss or did_fiv or did_heng


def watch_loop(cfg: Config, interval: int):
    logging.info("Watch ogni %ds...", interval)
    while True:
        run_once(cfg)
        time.sleep(interval)
