"""Resume text extraction: PDF, DOCX, TXT and pasted text (spec 4.2).

Pure functions over bytes: no Telegram, no network, easy to test. Detects scanned
(image-only) PDFs by text density so the bot can ask for pasted text instead.
"""

from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePath

TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # getFile limit (Bot API FAQ, verified 2026-11)
MAX_RESUME_CHARS = 30_000
MIN_CHARS_PER_PAGE = 40
MIN_TOTAL_CHARS = 150


class SourceKind(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"
    PASTE = "paste"


class ResumeError(Exception):
    """User-facing extraction problem; the message is safe to show."""


class UnsupportedFileError(ResumeError):
    pass


class FileTooLargeError(ResumeError):
    pass


class ScannedPdfError(ResumeError):
    pass


class EmptyResumeError(ResumeError):
    pass


@dataclass(frozen=True, slots=True)
class ExtractedResume:
    text: str
    kind: SourceKind
    pages: int = 1
    truncated: bool = False


SUPPORTED_EXTENSIONS = {".pdf": SourceKind.PDF, ".docx": SourceKind.DOCX, ".txt": SourceKind.TXT}


def kind_for(filename: str | None, mime_type: str | None = None) -> SourceKind:
    ext = PurePath(filename or "").suffix.lower()
    if ext in SUPPORTED_EXTENSIONS:
        return SUPPORTED_EXTENSIONS[ext]
    mime = (mime_type or "").lower()
    if mime == "application/pdf":
        return SourceKind.PDF
    if "wordprocessingml" in mime:
        return SourceKind.DOCX
    if mime.startswith("text/plain"):
        return SourceKind.TXT
    if ext == ".doc":
        raise UnsupportedFileError(
            "Old .doc files aren't supported. Save it as PDF or DOCX, or paste the text."
        )
    raise UnsupportedFileError(
        "I can read PDF, DOCX or TXT files. Please send one of those, or paste the text."
    )


def check_size(size: int | None) -> None:
    if size is not None and size > TELEGRAM_DOWNLOAD_LIMIT:
        raise FileTooLargeError(
            "That file is over Telegram's 20 MB limit for bots. Send a smaller PDF "
            "(export without images) or paste the text."
        )


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text).replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _finish(text: str, kind: SourceKind, pages: int = 1) -> ExtractedResume:
    text = clean_text(text)
    if len(text) < MIN_TOTAL_CHARS:
        raise EmptyResumeError(
            "I could barely find any text in that. Please paste your resume as text."
        )
    truncated = len(text) > MAX_RESUME_CHARS
    return ExtractedResume(text[:MAX_RESUME_CHARS], kind, pages, truncated)


# ------------------------------------------------------------------- PDF
def _pdf_text_pypdf(data: bytes) -> tuple[str, int]:
    from pypdf import PdfReader  # noqa: PLC0415
    from pypdf.errors import PdfReadError  # noqa: PLC0415

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ResumeError(
                "That PDF is password-protected. Remove the password or paste the text."
            )
        pages = [page.extract_text() or "" for page in reader.pages]
    except PdfReadError as exc:
        raise ResumeError(
            "That PDF looks damaged. Try exporting it again, or paste the text."
        ) from exc
    return "\n".join(pages), len(pages)


def _pdf_text_pdfplumber(data: bytes) -> str:
    import pdfplumber  # noqa: PLC0415

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages)


def _looks_garbled(text: str) -> bool:
    letters = sum(ch.isalpha() for ch in text)
    return len(text) > 0 and letters / len(text) < 0.45


def extract_pdf(data: bytes) -> ExtractedResume:
    text, pages = _pdf_text_pypdf(data)
    if len(clean_text(text)) < MIN_CHARS_PER_PAGE * max(pages, 1) or _looks_garbled(text):
        try:
            alt = _pdf_text_pdfplumber(data)
        except Exception:
            alt = ""
        if len(clean_text(alt)) > len(clean_text(text)):
            text = alt
    if len(clean_text(text)) < MIN_CHARS_PER_PAGE * max(pages, 1):
        raise ScannedPdfError(
            "This PDF looks scanned (it's an image, so there's no text to read). "
            "Please paste your resume as text instead."
        )
    return _finish(text, SourceKind.PDF, pages)


# ------------------------------------------------------------------- DOCX
def extract_docx(data: bytes) -> ExtractedResume:
    import docx  # noqa: PLC0415

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise ResumeError(
            "I couldn't open that DOCX. Try saving it again, or paste the text."
        ) from exc
    lines: list[str] = [p.text for p in document.paragraphs]

    def walk_tables(tables: list) -> None:  # type: ignore[type-arg]
        for table in tables:
            for row in table.rows:
                seen: set[int] = set()
                for cell in row.cells:
                    if id(cell._tc) in seen:  # merged cells repeat
                        continue
                    seen.add(id(cell._tc))
                    lines.extend(p.text for p in cell.paragraphs)
                    walk_tables(cell.tables)

    walk_tables(document.tables)  # many Indian resume templates are table-based
    for section in document.sections:
        lines.extend(p.text for p in section.header.paragraphs)
    return _finish("\n".join(lines), SourceKind.DOCX)


# ------------------------------------------------------------------- TXT
def decode_text(data: bytes) -> str:
    for bom, enc in (
        (b"\xef\xbb\xbf", "utf-8-sig"),
        (b"\xff\xfe", "utf-16"),
        (b"\xfe\xff", "utf-16"),
    ):
        if data.startswith(bom):
            return data.decode(enc, errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def extract_txt(data: bytes) -> ExtractedResume:
    return _finish(decode_text(data), SourceKind.TXT)


def extract_pasted(text: str) -> ExtractedResume:
    return _finish(text, SourceKind.PASTE)


def extract_file(
    filename: str | None, data: bytes, mime_type: str | None = None
) -> ExtractedResume:
    check_size(len(data))
    kind = kind_for(filename, mime_type)
    if kind is SourceKind.PDF:
        return extract_pdf(data)
    if kind is SourceKind.DOCX:
        return extract_docx(data)
    return extract_txt(data)
