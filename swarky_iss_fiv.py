#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import logging
import time

from swarky_core import (
    Config,
    BASE_NAME,
    ISS_BASENAME,
    ISS_BASENAME_2,
    map_location,
    log_swarky,
    log_error,
    ui_phase,
    write_edi,
    move_to,
    write_lines,
)

# ---- ISS ----------------------------------------------------------------------


def iss_loading(cfg: Config) -> bool:
    """
    Elabora i PDF nella cartella ISS:
      - accetta 3 formati:
          1) ISS_BASENAME (G....ISSRxxSxx.pdf)
          2) ISS_BASENAME_2 (....RxxSxx.pdf) -> nuovo formato
          3) BASE_NAME (D.. ..RxxSxx?.pdf)   -> pdf standard ma EDI stile ISS
      - sposta in PLM;
      - genera DESEDI (stile ISS);
      - aggiorna SwarkyISS.log;
      - log_swarky()/log_error() verso la GUI.
    """
    did = False

    try:
        candidates = [
            p for p in cfg.DIR_ISS.iterdir()
            if p.is_file() and p.suffix.lower() == ".pdf"
        ]
    except Exception as e:
        logging.exception("ISS: impossibile leggere la cartella %s: %s", cfg.DIR_ISS, e)
        return False

    for p in candidates:
        m_iss1 = ISS_BASENAME.fullmatch(p.name)         # G....ISSRxxSxx.pdf
        m_iss2 = ISS_BASENAME_2.fullmatch(p.name)       # ....RxxSxx.pdf (nuovo)
        m_std  = None if (m_iss1 or m_iss2) else BASE_NAME.fullmatch(p.name)  # D.. ..pdf

        if not (m_iss1 or m_iss2 or m_std):
            log_error(cfg, p.name, "Nome ISS Errato")
            continue

        if m_iss1:
            kind = "ISS_CLASSIC"
        elif m_iss2:
            kind = "ISS_MARK"
        else:
            kind = "ISS_STD"

        try:
            with ui_phase(f"{p.name} • ISS_To_PLM"):
                move_to(p, cfg.PLM_DIR)

            with ui_phase(f"{p.name} • ISS_Write_EDI"):
                if m_iss1:
                    # formato G....ISSRxxSxx
                    write_edi(
                        file_name=p.name,
                        out_dir=cfg.PLM_DIR,
                        iss_match=m_iss1,
                    )

                elif m_iss2:
                    # formato nuovo: YYYYJOB...-...R00S01.pdf
                    docno = m_iss2.group("docno")
                    rev = m_iss2.group("rev")
                    sheet = m_iss2.group("sheet")

                    order_number = docno.split("-", 1)[0]

                    write_edi(
                        file_name=p.name,
                        out_dir=cfg.PLM_DIR,
                        iss_style=True,
                        document_no_override=docno,
                        order_number=order_number,
                        rev_override=rev,
                        sheet_override=sheet,
                    )

                else:
                    # BASE_NAME ma deve usare EDI stile ISS (campi diversi)
                    assert m_std is not None
                    write_edi(
                        file_name=p.name,
                        out_dir=cfg.PLM_DIR,
                        iss_style=True,
                        m=m_std,
                    )

            log_swarky(cfg, p.name, "ISS", kind, "", "")
            did = True

        except Exception as e:
            logging.exception("Impossibile processare ISS %s: %s", p.name, e)

        finally:
            try:
                now = time.localtime()
                log_path = cfg.DIR_ISS / "SwarkyISS.log"
                line = time.strftime("%d.%b.%Y # %H:%M:%S", now) + f" # {p.stem}"
                write_lines(log_path, [line])
            except Exception:
                logging.exception("ISS: impossibile aggiornare SwarkyISS.log")

    return did

# ---- FIV ----------------------------------------------------------------------


def fiv_loading(cfg: Config) -> bool:
    """
    Elabora i file nella cartella FIV_LOADING:
      - accetta TIF/TIFF e, opzionalmente, PDF (ACCEPT_PDF);
      - valida il nome (BASE_NAME);
      - mappa la location (map_location);
      - genera DESEDI in PLM_DIR;
      - sposta il file in PLM_DIR;
      - log_swarky()/log_error() verso la GUI.
    Ritorna True se ha fatto almeno qualcosa.
    """
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
            log_error(cfg, p.name, "Nome FIV Errato")
            continue

        try:
            with ui_phase(f"{p.name} • FIV_Map_loc"):
                loc = map_location(m, cfg)

            with ui_phase(f"{p.name} • FIV_Write_EDI"):
                write_edi(m=m, file_name=p.name, loc=loc, out_dir=cfg.PLM_DIR)

            with ui_phase(f"{p.name} • FIV_To_PLM"):
                move_to(p, cfg.PLM_DIR)

            log_swarky(cfg, p.name, "FIV", "FIV loading", "", "")
            did = True

        except Exception as e:
            logging.exception("Impossibile processare FIV %s: %s", p.name, e)

    return did


# ---- HENGELO ------------------------------------------------------------------


def heng_loading(cfg: Config) -> bool:
    """
    HENGELO → PLM
      - NON crea DESEDI (sono già in Hengelo)
      - Sposta SOLO coppie complete: (pdf/tif/tiff) + .DESEDI con stesso stem (case-insensitive)
      - Se manca uno dei due, non fa nulla per quello stem
      - Se ci sono duplicati (stesso stem con 2 dwg o 2 desedi) lo stem è ambiguo e non viene processato
      - Ordine per coppia: prima dwg, poi DESEDI, poi coppia successiva
    """
    did = False

    try:
        files = [p for p in cfg.DIR_HENGELO.iterdir() if p.is_file()]
    except Exception as e:
        logging.exception("HENG: lettura cartella fallita %s: %s", cfg.DIR_HENGELO, e)
        return False

    drawings: dict[str, Path] = {}
    desedis: dict[str, Path] = {}
    ambiguous: set[str] = set()

    for p in files:
        ext = p.suffix.lower()
        stem_key = p.stem.lower()

        if stem_key in ambiguous:
            continue

        if ext in (".pdf", ".tif", ".tiff"):
            if stem_key in drawings and drawings[stem_key].name.lower() != p.name.lower():
                log_error(cfg, p.name, "HENG: doppio disegno stesso nome base", drawings[stem_key].name)
                ambiguous.add(stem_key)
                drawings.pop(stem_key, None)
                desedis.pop(stem_key, None)
                continue
            drawings[stem_key] = p

        elif ext == ".desedi":
            if stem_key in desedis and desedis[stem_key].name.lower() != p.name.lower():
                log_error(cfg, p.name, "HENG: doppio DESEDI stesso nome base", desedis[stem_key].name)
                ambiguous.add(stem_key)
                drawings.pop(stem_key, None)
                desedis.pop(stem_key, None)
                continue
            desedis[stem_key] = p

    pair_keys = sorted((set(drawings.keys()) & set(desedis.keys())) - ambiguous)
    if not pair_keys:
        return False

    for k in pair_keys:
        dwg = drawings.get(k)
        edi = desedis.get(k)
        if not dwg or not edi:
            continue
        if not dwg.exists() or not edi.exists():
            continue

        try:
            with ui_phase(f"{dwg.name} • HENG_To_PLM (dwg)"):
                move_to(dwg, cfg.PLM_DIR)

            with ui_phase(f"{edi.name} • HENG_To_PLM (DESEDI)"):
                move_to(edi, cfg.PLM_DIR)

            log_swarky(cfg, dwg.name, "HENG", "Hengelo → PLM", "", "PLM")
            did = True

        except Exception as e:
            logging.exception("HENG: errore spostamento coppia %s: %s", dwg.stem, e)
            log_error(cfg, dwg.name, "HENG: errore spostamento coppia", edi.name)
            # niente rollback: si prosegue con le altre coppie
            continue

    return did
