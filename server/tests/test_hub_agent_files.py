from io import BytesIO
import shutil
from time import monotonic
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.hub_agent import HubAgentConversation, HubAgentConversationContext
from app.services.hub_agent import HubAgentError, HubAgentService
from app.services.hub_agent_files import (
    AgentFileExtraction, HubAgentFileError, MAX_AGENT_FILE_TEXT, _ocr_pdf_page, extract_agent_file,
)


def test_agent_file_extraction_rejects_unreadable_and_oversized_files():
    assert extract_agent_file(filename=r"C:\uploads\Brief.txt", content=b"\xef\xbb\xbfHallo Welt") == AgentFileExtraction(
        name="Brief.txt", text="Hallo Welt"
    )
    with pytest.raises(HubAgentFileError, match="Nur PDF- und TXT"):
        extract_agent_file(filename="document.html", content=b"<p>test</p>")
    with pytest.raises(HubAgentFileError, match="5 MB"):
        extract_agent_file(filename="brief.txt", content=b"a" * (5 * 1024 * 1024 + 1))
    with pytest.raises(HubAgentFileError, match="100.000"):
        extract_agent_file(filename="brief.txt", content=b"a" * (MAX_AGENT_FILE_TEXT + 1))
    with pytest.raises(HubAgentFileError, match="UTF-8"):
        extract_agent_file(filename="brief.txt", content=b"\xff")

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    output = BytesIO()
    writer.write(output)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("app.services.hub_agent_files._ocr_pdf_page", lambda *_args: "")
        with pytest.raises(HubAgentFileError, match="kein lesbarer Text"):
            extract_agent_file(filename="scan.pdf", content=output.getvalue())


def test_agent_file_ocr_fallback_marks_recognized_text(monkeypatch):
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    output = BytesIO()
    writer.write(output)
    monkeypatch.setattr(
        "app.services.hub_agent_files._ocr_pdf_page",
        lambda _content, page_number, _deadline: f"Seite {page_number}: Rechnungsbetrag 42 Euro",
    )
    assert extract_agent_file(filename="scan.pdf", content=output.getvalue()) == AgentFileExtraction(
        name="scan.pdf", text="Seite 1: Rechnungsbetrag 42 Euro", used_ocr=True
    )


@pytest.mark.skipif(
    not shutil.which("pdftoppm") or not shutil.which("tesseract"),
    reason="OCR-Werkzeuge sind lokal nicht installiert",
)
def test_agent_file_ocr_recognizes_image_only_pdf():
    image_module = pytest.importorskip("PIL.Image")
    draw_module = pytest.importorskip("PIL.ImageDraw")
    font_module = pytest.importorskip("PIL.ImageFont")
    image = image_module.new("RGB", (1400, 300), "white")
    font = font_module.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 52)
    draw_module.Draw(image).text((45, 90), "Mahnung Betrag 42 Euro", fill="black", font=font)
    output = BytesIO()
    image.save(output, format="PDF")

    extraction = extract_agent_file(filename="scan.pdf", content=output.getvalue())
    assert extraction.used_ocr
    assert "Betrag 42 Euro" in extraction.text


def test_agent_file_ocr_pipeline_renders_and_recognizes_in_memory(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs["input"]))
        if command[0] == "pdftoppm":
            return SimpleNamespace(returncode=0, stdout=b"\x89PNG\r\n\x1a\nimage")
        return SimpleNamespace(returncode=0, stdout="Mahnung 123".encode())

    monkeypatch.setattr("app.services.hub_agent_files.subprocess.run", fake_run)
    assert _ocr_pdf_page(b"%PDF-scan", 2, monotonic() + 30) == "Mahnung 123"
    assert calls[0][0][:5] == ["pdftoppm", "-f", "2", "-l", "2"]
    assert calls[0][1] == b"%PDF-scan"
    assert calls[1][0][:3] == ["tesseract", "stdin", "stdout"]
    assert calls[1][1].startswith(b"\x89PNG")


def test_agent_file_ocr_page_limit(monkeypatch):
    writer = PdfWriter()
    for _ in range(11):
        writer.add_blank_page(width=200, height=200)
    output = BytesIO()
    writer.write(output)
    monkeypatch.setattr("app.services.hub_agent_files._ocr_pdf_page", lambda *_args: "Lesbarer Text aus dem Scan")
    with pytest.raises(HubAgentFileError, match="zehn bildbasierte"):
        extract_agent_file(filename="scan.pdf", content=output.getvalue())


def test_agent_file_extraction_reads_pdf_text(monkeypatch):
    class Page:
        def extract_text(self):
            return "Rechnung 42, Betrag 10 Euro"

    class Reader:
        is_encrypted = False
        pages = [Page()]

    monkeypatch.setattr("app.services.hub_agent_files.PdfReader", lambda *_args, **_kwargs: Reader())
    assert extract_agent_file(filename="Rechnung.pdf", content=b"%PDF-1.7\n") == AgentFileExtraction(
        name="Rechnung.pdf", text="Rechnung 42, Betrag 10 Euro"
    )


def test_agent_file_extraction_accepts_five_text_heavy_pages(monkeypatch):
    class Page:
        def extract_text(self):
            return "A" * 12_000

    class Reader:
        is_encrypted = False
        pages = [Page() for _ in range(5)]

    monkeypatch.setattr("app.services.hub_agent_files.PdfReader", lambda *_args, **_kwargs: Reader())
    extraction = extract_agent_file(filename="Lang.pdf", content=b"%PDF-1.7\n")
    assert len(extraction.text) == 60_004
    assert extraction.text.count("\n") == 4


def test_agent_file_extraction_rejects_pdf_above_new_text_limit(monkeypatch):
    class Page:
        def extract_text(self):
            return "A" * 20_001

    class Reader:
        is_encrypted = False
        pages = [Page() for _ in range(5)]

    monkeypatch.setattr("app.services.hub_agent_files.PdfReader", lambda *_args, **_kwargs: Reader())
    with pytest.raises(HubAgentFileError, match="100.000"):
        extract_agent_file(filename="Zu-lang.pdf", content=b"%PDF-1.7\n")


def test_agent_context_column_supports_long_encrypted_pdf_text_on_mysql():
    column_type = HubAgentConversationContext.__table__.c.encrypted_snapshot_json.type
    assert column_type.compile(dialect=mysql.dialect()) == "MEDIUMTEXT"


def test_agent_file_is_encrypted_scoped_and_removed_from_conversation():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    with Session(engine) as db:
        service = HubAgentService(db=db, cipher=cipher)
        chat = service.start_conversation(actor="alice")
        chat = service.add_file_context(
            actor="alice", conversation_id=chat.conversation_id,
            extraction=extract_agent_file(filename="Anweisung.txt", content=b"Rechnung 42, Betrag 10 Euro"),
        )
        assert chat.contexts[0].resource_type == "file"
        assert chat.contexts[0].label == "Datei: Anweisung.txt"
        context = db.scalars(select(HubAgentConversationContext)).one()
        assert "Rechnung 42" not in context.encrypted_snapshot_json
        conversation = db.get(HubAgentConversation, chat.conversation_id)
        assert "Rechnung 42" in service._conversation_prompt_contexts(conversation, exclude_email_key="")[0]
        with pytest.raises(HubAgentError, match="nicht gefunden"):
            service.add_file_context(
                actor="bob", conversation_id=chat.conversation_id,
                extraction=AgentFileExtraction(name="fremd.txt", text="test"),
            )
        for index in range(2):
            service.add_file_context(
                actor="alice", conversation_id=chat.conversation_id,
                extraction=AgentFileExtraction(name=f"weitere-{index}.txt", text="Inhalt"),
            )
        with pytest.raises(HubAgentError, match="höchstens drei"):
            service.add_file_context(
                actor="alice", conversation_id=chat.conversation_id,
                extraction=AgentFileExtraction(name="vier.txt", text="Inhalt"),
            )
        chat = service.remove_context(
            actor="alice", conversation_id=chat.conversation_id, context_id=context.id
        )
        assert all(item.id != context.id for item in chat.contexts)


def test_agent_ocr_context_carries_review_warning():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        service = HubAgentService(db=db, cipher=SecretCipher("a" * 32))
        chat = service.start_conversation(actor="alice")
        chat = service.add_file_context(
            actor="alice", conversation_id=chat.conversation_id,
            extraction=AgentFileExtraction(name="scan.pdf", text="Rechnungsbetrag 42 Euro", used_ocr=True),
        )
        assert "OCR-Text" in chat.contexts[0].description
        conversation = db.get(HubAgentConversation, chat.conversation_id)
        assert "Prüfe Zahlen, Namen und Termine" in service._conversation_prompt_contexts(
            conversation, exclude_email_key=""
        )[0]


def test_agent_file_context_keeps_full_text_and_bounds_total():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        service = HubAgentService(db=db, cipher=SecretCipher("a" * 32))
        chat = service.start_conversation(actor="alice")
        chat = service.add_file_context(
            actor="alice", conversation_id=chat.conversation_id,
            extraction=AgentFileExtraction(name="lang.pdf", text="A" * 60_000 + "ENDE"),
        )
        conversation = db.get(HubAgentConversation, chat.conversation_id)
        prompt = service._conversation_prompt_contexts(conversation, exclude_email_key="")[0]
        assert prompt.endswith("ENDE")
        assert len(prompt) > 60_000
        context = db.scalars(select(HubAgentConversationContext)).one()
        assert len(context.encrypted_snapshot_json) > 64_000
        service.add_file_context(
            actor="alice", conversation_id=chat.conversation_id,
            extraction=AgentFileExtraction(name="zweite.txt", text="B" * 90_000),
        )
        with pytest.raises(HubAgentError, match="160.000"):
            service.add_file_context(
                actor="alice", conversation_id=chat.conversation_id,
                extraction=AgentFileExtraction(name="dritte.txt", text="C" * 20_000),
            )
