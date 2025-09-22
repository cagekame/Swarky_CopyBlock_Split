# swarky_io.py
from __future__ import annotations
import os, ctypes
import ctypes.wintypes as wt
import logging
from pathlib import Path
from typing import List, Optional, Tuple, Protocol, Iterable
from datetime import datetime

# ====== Buffer di copia (override via env) ======
DEFAULT_COPY_BUF_MIB = int(os.environ.get("SWARKY_COPY_BUF_MIB", "8"))
COPY_BUF_BYTES = max(1, DEFAULT_COPY_BUF_MIB) * 1024 * 1024

# ====== Interfaccia per le operazioni di I/O ======
class IOOps(Protocol):
    def iter_candidates(self, dirp: Path, accept_pdf: bool) -> Iterable[Path]: ...
    def list_same_doc_prefisso(self, dirp: Path, docno_prefix: str) -> tuple[str, ...]: ...
    def check_orientation_ok(self, tif_path: Path) -> bool: ...
    def fast_copy_or_link(self, src: Path, dst: Path) -> None: ...
    def move_to(self, src: Path, dst_dir: Path) -> None: ...
    def move_to_storico_safe(self, src: Path, dst_dir: Path) -> tuple[bool, int]: ...
    def write_lines(self, p: Path, lines: List[str]) -> None: ...
    def write_edi(self, *, file_name: str, out_dir: Path,
                  body_lines: List[str]) -> None: ...

# ====== Implementazione di default (Windows/SMB) ======

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
FILE_ATTRIBUTE_DIRECTORY = 0x10
FIND_FIRST_EX_LARGE_FETCH = 2
FindExInfoBasic = 1
FindExSearchNameMatch = 0
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3

class WIN32_FIND_DATAW(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wt.DWORD),
        ("ftCreationTime", wt.FILETIME),
        ("ftLastAccessTime", wt.FILETIME),
        ("ftLastWriteTime", wt.FILETIME),
        ("nFileSizeHigh", wt.DWORD),
        ("nFileSizeLow", wt.DWORD),
        ("dwReserved0", wt.DWORD),
        ("dwReserved1", wt.DWORD),
        ("cFileName", ctypes.c_wchar * 260),
        ("cAlternateFileName", ctypes.c_wchar * 14),
    ]

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_FindFirstFileW = _k32.FindFirstFileW
_FindFirstFileW.argtypes = [wt.LPCWSTR, ctypes.POINTER(WIN32_FIND_DATAW)]
_FindFirstFileW.restype = wt.HANDLE
_FindNextFileW = _k32.FindNextFileW
_FindNextFileW.argtypes = [wt.HANDLE, ctypes.POINTER(WIN32_FIND_DATAW)]
_FindNextFileW.restype = wt.BOOL
_FindClose = _k32.FindClose
_FindClose.argtypes = [wt.HANDLE]
_FindClose.restype = wt.BOOL

try:
    _FindFirstFileExW = _k32.FindFirstFileExW
    _FindFirstFileExW.argtypes = [
        wt.LPCWSTR, ctypes.c_int, ctypes.POINTER(WIN32_FIND_DATAW),
        ctypes.c_int, ctypes.c_void_p, wt.DWORD
    ]
    _FindFirstFileExW.restype = wt.HANDLE
except AttributeError:
    _FindFirstFileExW = None

def _win_find_names_ex(dirp: Path, pattern: str) -> tuple[str, ...]:
    query = str(dirp / pattern)
    data = WIN32_FIND_DATAW()

    if _FindFirstFileExW is None:
        h = _FindFirstFileW(query, ctypes.byref(data))
    else:
        h = _FindFirstFileExW(query, FindExInfoBasic, ctypes.byref(data),
                              FindExSearchNameMatch, None, FIND_FIRST_EX_LARGE_FETCH)

    if h == INVALID_HANDLE_VALUE:
        err = ctypes.get_last_error()
        if err in (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND):
            return tuple()
        # fallback best-effort
        h = _k32.FindFirstFileW(query, ctypes.byref(data))
        if h == INVALID_HANDLE_VALUE:
            return tuple()

    names: list[str] = []
    try:
        while True:
            nm = data.cFileName
            if nm not in (".", "..") and not (data.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY):
                names.append(nm)
            if not _FindNextFileW(h, ctypes.byref(data)):
                break
    finally:
        _FindClose(h)
    return tuple(names)

def _buffered_copy(src: Path, dst: Path, bufsize: int = COPY_BUF_BYTES) -> None:
    # no exists(): si prova a scrivere subito il file finale
    with open(src, "rb", buffering=0) as sf, open(dst, "wb", buffering=0) as df:
        while True:
            chunk = sf.read(bufsize)
            if not chunk:
                break
            df.write(chunk)
    # preserva mtime/atime (non i permessi remoti)
    try:
        st = os.stat(src)
        os.utime(dst, (st.st_atime, st.st_mtime))
    except Exception:
        pass

def _try_link_then_copy(src: Path, dst: Path) -> None:
    # 1) hardlink (velocissimo su stessa share/volume locale)
    try:
        os.link(src, dst)
        return
    except OSError:
        pass
    # 2) rename/move locale
    try:
        os.replace(src, dst)
        return
    except OSError:
        pass
    # 3) fallback: copia a blocchi (niente esistenza preventiva)
    try:
        _buffered_copy(src, dst)
    except Exception as e:
        # se qualcosa va storto: prova a pulire il parziale
        try:
            if dst.exists():
                dst.unlink()
        except Exception:
            pass
        raise e

# ---- TIFF (solo header) ----
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
            n = u16(f.read(2))
            TAG_W, TAG_H = 256, 257
            TYPE_SIZES = {1:1,2:1,3:2,4:4,5:8,7:1,9:4,10:8}
            w = h = None
            for _ in range(n):
                ent = f.read(12)
                tag = u16(ent[0:2]); typ = u16(ent[2:4]); cnt = u32(ent[4:8]); val = ent[8:12]
                unit = TYPE_SIZES.get(typ, None)
                if not unit: continue
                if unit * cnt <= 4:
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
                if w and h: return (w,h)
    except Exception:
        return None
    return None

def _check_orientation_ok(path: Path) -> bool:
    if path.suffix.lower() == ".pdf":
        return True
    wh = _tiff_read_size_vfast(path)
    if wh is None:
        return True
    w, h = wh
    return w > h

class DefaultIOOps:
    def iter_candidates(self, dirp: Path, accept_pdf: bool):
        exts = {".tif"}
        if accept_pdf:
            exts.add(".pdf")
        with os.scandir(dirp) as it:
            for de in it:
                if de.is_file():
                    suf = os.path.splitext(de.name)[1].lower()
                    if suf in exts:
                        yield Path(de.path)

    def list_same_doc_prefisso(self, dirp: Path, docno_prefix: str) -> tuple[str, ...]:
        return _win_find_names_ex(dirp, f"{docno_prefix}*")

    def check_orientation_ok(self, tif_path: Path) -> bool:
        return _check_orientation_ok(tif_path)

    def fast_copy_or_link(self, src: Path, dst: Path) -> None:
        _try_link_then_copy(src, dst)

    def move_to(self, src: Path, dst_dir: Path) -> None:
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / src.name
        _try_link_then_copy(src, dst)
        # se siamo arrivati qui via copia: prova a cancellare l'origine
        try:
            if src.exists():
                src.unlink(missing_ok=True)
        except Exception:
            pass

    def move_to_storico_safe(self, src: Path, dst_dir: Path) -> tuple[bool, int]:
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / src.name
        # niente exists(): si prova il rename, poi la copia
        try:
            os.replace(src, dst)
            return (True, 1)
        except OSError:
            try:
                _buffered_copy(src, dst)
                try:
                    src.unlink(missing_ok=True)
                except Exception:
                    pass
                return (True, 1)
            except Exception:
                return (False, 8)

    def write_lines(self, p: Path, lines: List[str]) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def write_edi(self, *, file_name: str, out_dir: Path, body_lines: List[str]) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        edi = out_dir / (Path(file_name).stem + ".DESEDI")
        if edi.exists():
            return
        self.write_lines(edi, body_lines)


_DEFAULT_IO = DefaultIOOps()


def iter_candidates(dirp: Path, accept_pdf: bool):
    """Itera i file candidati usando l'implementazione predefinita."""
    yield from _DEFAULT_IO.iter_candidates(dirp, accept_pdf)


def list_same_doc_prefisso(dirp: Path, docno_prefix: str) -> tuple[str, ...]:
    """Restituisce i nomi che iniziano con il prefisso indicato."""
    return _DEFAULT_IO.list_same_doc_prefisso(dirp, docno_prefix)


def check_orientation_ok(tif_path: Path) -> bool:
    return _DEFAULT_IO.check_orientation_ok(tif_path)


def fast_copy_or_link(src: Path, dst: Path) -> None:
    _DEFAULT_IO.fast_copy_or_link(src, dst)


def move_to(src: Path, dst_dir: Path) -> None:
    _DEFAULT_IO.move_to(src, dst_dir)


def move_to_storico_safe(src: Path, dst_dir: Path) -> tuple[bool, int]:
    return _DEFAULT_IO.move_to_storico_safe(src, dst_dir)


def write_lines(p: Path, lines: List[str]) -> None:
    _DEFAULT_IO.write_lines(p, lines)


def write_edi(*, file_name: str, out_dir: Path, body_lines: List[str]) -> None:
    _DEFAULT_IO.write_edi(file_name=file_name, out_dir=out_dir, body_lines=body_lines)
