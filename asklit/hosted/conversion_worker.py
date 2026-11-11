"""Resource-bounded, local-only document conversion. Invoked as an isolated script."""
import json
import resource
import sys
import os
import sysconfig
from pathlib import Path

resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
resource.setrlimit(resource.RLIMIT_FSIZE, (12 * 1024 * 1024,) * 2)
resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
resource.setrlimit(resource.RLIMIT_NPROC, (16, 16))

# Landlock restricts reads to Python libraries and this conversion's temp directory.
# In particular, /proc, application data, environment files, and other uploads are unreadable.
import ctypes
libc = ctypes.CDLL(None, use_errno=True)
class Ruleset(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]
class PathRule(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

abi = libc.syscall(444, 0, 0, 1)
if abi < 3:
    sys.exit(78)
all_fs = (1 << 15) - 1
rules = Ruleset(all_fs)
rules_fd = libc.syscall(444, ctypes.byref(rules), ctypes.sizeof(rules), 0)
if rules_fd < 0:
    sys.exit(78)
read = (1 << 2) | (1 << 3)
paths = {str(Path(p).resolve()) for p in
         ["/usr/lib", "/usr/local/lib", "/lib", "/lib64",
          sysconfig.get_path("purelib"), sysconfig.get_path("platlib"),
          sysconfig.get_path("stdlib"), sysconfig.get_path("platstdlib")] if p and Path(p).exists()}
try:
    for path, rights in [(p, read) for p in paths] + [(str(Path.cwd()), all_fs & ~1),
                          ("/etc/ld.so.cache", 1 << 2), ("/dev/urandom", 1 << 2)]:
        if not Path(path).exists():
            continue
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        try:
            rule = PathRule(rights, fd)
            if libc.syscall(445, rules_fd, 1, ctypes.byref(rule), 0) != 0:
                sys.exit(78)
        finally:
            os.close(fd)
    if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.syscall(446, rules_fd, 0) != 0:
        sys.exit(78)
finally:
    os.close(rules_fd)

# Block native sockets, subprocesses/threads, signals to other processes, and inspection.
lib = ctypes.CDLL("libseccomp.so.2")
lib.seccomp_init.argtypes = [ctypes.c_uint32]
lib.seccomp_init.restype = ctypes.c_void_p
lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
lib.seccomp_load.argtypes = [ctypes.c_void_p]
lib.seccomp_release.argtypes = [ctypes.c_void_p]
ctx = lib.seccomp_init(0x7fff0000)  # allow by default; deny dangerous facilities
if not ctx:
    sys.exit(78)
for name in ("socket", "socketpair", "connect", "execve", "execveat", "ptrace",
             "process_vm_readv", "process_vm_writev", "kill", "tkill", "tgkill",
             "fork", "vfork", "clone", "clone3", "perf_event_open", "shmget", "shmat"):
    number = lib.seccomp_syscall_resolve_name(name.encode())
    if number >= 0 and lib.seccomp_rule_add(ctx, 0x00050001, number, 0) != 0:
        sys.exit(78)
if lib.seccomp_load(ctx) != 0:
    sys.exit(78)
lib.seccomp_release(ctx)

class ConversionError(ValueError):
    pass

# These converters only read local bytes. Block accidental Python network calls.
def no_network(event, args):
    if event in {"socket.__new__", "socket.connect", "urllib.Request"}:
        raise ConversionError("Network access is disabled during conversion.")
sys.addaudithook(no_network)


def convert(path, max_chars, max_pages):
    import zipfile
    ext = path.suffix.lower()
    data = path.read_bytes()
    pages = []
    if ext == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise ConversionError("This file is not a PDF.")
        from pypdf import PdfReader
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ConversionError("Upload an unencrypted PDF.")
        if len(reader.pages) > max_pages:
            raise ConversionError(f"PDFs may contain at most {max_pages} pages.")
        total = 0
        for index, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if not text.strip() and page.images:
                raise ConversionError("This PDF has a scanned page without readable text. Run OCR first, then upload it again.")
            total += len(text)
            if total > max_chars:
                raise ConversionError("The document contains too much text. Upload a shorter excerpt.")
            pages.append({"text": text, "page_number": index + 1})
    elif ext in {".docx", ".pptx", ".xlsx", ".odt"}:
        if not data.startswith(b"PK"):
            raise ConversionError("The file does not match its document type.")
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 1000 or sum(i.file_size for i in entries) > 25 * 1024 * 1024:
                raise ConversionError("The expanded document exceeds the safety limits.")
            names = {i.filename for i in entries}
            required = {".docx": "word/document.xml", ".pptx": "ppt/presentation.xml",
                        ".xlsx": "xl/workbook.xml", ".odt": "content.xml"}[ext]
            if required not in names:
                raise ConversionError("The file does not match its document type.")
            for info in entries:
                name = info.filename.lower()
                if info.flag_bits & 1 or "vbaproject" in name or "macros/" in name:
                    raise ConversionError("Encrypted documents, macros, and embedded files are not supported.")
                if name.endswith((".xml", ".rels", ".vml")):
                    raw = archive.read(info)
                    # NUL removal catches UTF-16/32 XML declarations as well as UTF-8.
                    if b"<!DOCTYPE" in raw.replace(b"\x00", b"").upper():
                        raise ConversionError("Documents with XML document types are not supported.")
                if "/embeddings/" in name:
                    raise ConversionError("Documents with embedded files (including chart workbooks) are not supported. Export as PDF.")
        import anydoc
        text = anydoc.to_markdown(str(path), ocr="reject")
        pages = [{"text": text, "page_number": 1}]
    else:
        text = data.decode("utf-8-sig")
        if "\x00" in text:
            raise ConversionError("Upload a UTF-8 text document, not a binary file.")
        if ext in {".html", ".htm"}:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(text, "html.parser")
            for element in soup(["script", "style", "iframe", "object"]):
                element.decompose()
            text = soup.get_text(separator="\n")
        pages = [{"text": text, "page_number": 1}]
    for page in pages:
        page["text"] = page["text"].replace("\x00", "").encode("utf-8", errors="replace").decode("utf-8")
    text = "\n".join(p["text"] for p in pages).strip()
    if not text:
        raise ConversionError("The file has no readable text. Scans and images need OCR first.")
    if len(text) > max_chars:
        raise ConversionError("The document contains too much text. Upload a shorter excerpt.")
    return {"text": text, "pages": pages}


if __name__ == "__main__":
    try:
        result = convert(Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]))
    except ConversionError as exc:
        result = {"error": str(exc)[:250]}
    except Exception:
        result = {"error": "This document could not be read. Check its format or export it as text."}
    Path(sys.argv[4]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
