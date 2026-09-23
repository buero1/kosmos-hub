"""Extract bounded, untrusted text for a Hub-Agent conversation."""

from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePath
import subprocess
from time import monotonic

from pypdf import PdfReader


MAX_AGENT_FILE_BYTES = 5 * 1024 * 1024
MAX_AGENT_FILE_PAGES = 30
MAX_AGENT_FILE_TEXT = 100_000
MAX_AGENT_FILE_CONTEXT_TOTAL = 160_000
MAX_AGENT_OCR_PAGES = 10
MAX_AGENT_OCR_SECONDS = 45


@dataclass(frozen=True)
class AgentFileExtraction:
    name: str
    text: str
    used_ocr: bool = False


class HubAgentFileError(ValueError):
    pass


def _ocr_pdf_page(content: bytes, page_number: int, deadline: float) -> str:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise HubAgentFileError("Die Texterkennung hat zu lange gedauert. Bitte verwende ein kürzeres PDF.")
    try:
        rendered = subprocess.run(
            ["pdftoppm", "-f", str(page_number), "-l", str(page_number),
             "-scale-to", "1800", "-gray", "-png", "-singlefile", "-"],
            input=content, capture_output=True, check=False,
            timeout=min(15, remaining),
        )
        if rendered.returncode or not rendered.stdout.startswith(b"\x89PNG"):
            raise HubAgentFileError("Eine PDF-Seite konnte nicht für OCR vorbereitet werden.")
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise HubAgentFileError("Die Texterkennung hat zu lange gedauert. Bitte verwende ein kürzeres PDF.")
        for segmentation_mode in ("3", "6"):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise HubAgentFileError("Die Texterkennung hat zu lange gedauert. Bitte verwende ein kürzeres PDF.")
            recognized = subprocess.run(
                ["tesseract", "stdin", "stdout", "-l", "deu+eng", "--psm", segmentation_mode],
                input=rendered.stdout, capture_output=True, check=False,
                timeout=min(20, remaining),
            )
            if recognized.returncode:
                raise HubAgentFileError("Die Texterkennung für das PDF ist fehlgeschlagen.")
            text = recognized.stdout.decode("utf-8", errors="replace").strip()
            if text:
                return text
        return ""
    except FileNotFoundError as exc:
        raise HubAgentFileError("Die OCR-Werkzeuge sind auf dem Server nicht verfügbar.") from exc
    except subprocess.TimeoutExpired as exc:
        raise HubAgentFileError("Die Texterkennung hat zu lange gedauert. Bitte verwende ein kürzeres PDF.") from exc


def extract_agent_file(*, filename: str, content: bytes) -> AgentFileExtraction:
    name = filename.replace("\\", "/").split("/")[-1].strip()
    if not name or len(name) > 180 or any(ord(character) < 32 for character in name):
        raise HubAgentFileError("Der Dateiname ist ungültig oder zu lang.")
    if not content or len(content) > MAX_AGENT_FILE_BYTES:
        raise HubAgentFileError("Die Datei muss zwischen 1 Byte und 5 MB groß sein.")

    suffix = PurePath(name).suffix.casefold()
    if suffix == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise HubAgentFileError("Die Datei ist keine gültige PDF-Datei.")
        try:
            reader = PdfReader(BytesIO(content), strict=False)
            if reader.is_encrypted:
                raise HubAgentFileError("Passwortgeschützte PDFs werden nicht unterstützt.")
            if len(reader.pages) > MAX_AGENT_FILE_PAGES:
                raise HubAgentFileError("Das PDF darf höchstens 30 Seiten enthalten.")
            chunks: list[str] = []
            length = 0
            ocr_pages = 0
            used_ocr = False
            deadline = monotonic() + MAX_AGENT_OCR_SECONDS
            for page_number, page in enumerate(reader.pages, start=1):
                chunk = (page.extract_text() or "").strip()
                if len(chunk) < 25:
                    ocr_pages += 1
                    if ocr_pages > MAX_AGENT_OCR_PAGES:
                        raise HubAgentFileError("Für OCR sind höchstens zehn bildbasierte PDF-Seiten möglich.")
                    recognized = _ocr_pdf_page(content, page_number, deadline)
                    if not recognized and not chunk:
                        raise HubAgentFileError(f"Auf Seite {page_number} wurde auch mit OCR kein lesbarer Text gefunden.")
                    if recognized and len(recognized) > len(chunk):
                        chunk = recognized
                        used_ocr = True
                length += len(chunk)
                if length > MAX_AGENT_FILE_TEXT:
                    raise HubAgentFileError("Der Dateitext ist zu lang (maximal 100.000 Zeichen).")
                chunks.append(chunk)
            text = "\n".join(chunks)
        except HubAgentFileError:
            raise
        except Exception as exc:
            raise HubAgentFileError("Das PDF konnte nicht gelesen werden.") from exc
    elif suffix == ".txt":
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HubAgentFileError("Textdateien müssen UTF-8-kodiert sein.") from exc
    else:
        raise HubAgentFileError("Nur PDF- und TXT-Dateien werden unterstützt.")

    text = text.strip()
    if not text:
        raise HubAgentFileError("Auch mit Texterkennung wurde kein lesbarer Text im PDF gefunden." if suffix == ".pdf" else "Die Datei enthält keinen lesbaren Text.")
    if len(text) > MAX_AGENT_FILE_TEXT:
        raise HubAgentFileError("Der Dateitext ist zu lang (maximal 100.000 Zeichen).")
    return AgentFileExtraction(name=name, text=text, used_ocr=used_ocr if suffix == ".pdf" else False)
