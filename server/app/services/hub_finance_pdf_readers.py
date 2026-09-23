"""Authorized local PDF status/download readers; no generation or remote fetching."""
from app.models.hub_finance_invoice_pdf import HubFinanceInvoicePdf
from app.models.hub_finance_order_pdf import HubFinanceOrderPdf
from app.services.hub_finance_operations_shared import PDF_KINDS, require_record
from app.services.hub_finance_documents import HubFinanceDocumentService
from app.services.hub_finance_documents import HubFinanceDocumentError
from app.services.hub_finance_pdf_generation import HubFinancePdfService
from app.services.hub_finance_pdf_generation import HubFinancePdfError
from app.services.hub_operations import HubOperationError, HubOperationPending
from sqlalchemy import select


def pdf_record(service, kind, record_id):
    if kind not in PDF_KINDS:
        raise HubOperationError("Diese Belegart hat keine PDF.")
    return require_record(service, kind, record_id)


def generated_status(service, kind, record_id):
    pdf_record(service, kind, record_id)
    return HubFinancePdfService(db=service.db, cipher=service.cipher).view(document_type=kind, document_id=record_id)


def original_metadata(service, kind, record_id):
    pdf_record(service, kind, record_id)
    if kind == "invoices":
        return service.db.scalar(select(HubFinanceInvoicePdf).where(HubFinanceInvoicePdf.invoice_id == record_id))
    if kind == "orders":
        return service.db.scalar(select(HubFinanceOrderPdf).where(HubFinanceOrderPdf.order_id == record_id))
    return None


def load_pdf(service, kind, record_id, *, source="generated", wait=False):
    pdf_record(service, kind, record_id)
    if source not in {"generated", "original", "available"}:
        raise HubOperationError("Die PDF-Quelle ist ungueltig.")
    if source != "original":
        state = generated_status(service, kind, record_id)
        if wait and ((state and state.status in {"queued", "rendering"}) or (state is None and kind == "offers")):
            raise HubOperationPending("Die Beleg-PDF wird noch erzeugt. Bitte den Folgeschritt erneut ausfuehren.")
        if source == "generated" or state is not None:
            try:
                return HubFinancePdfService(db=service.db, cipher=service.cipher).load(document_type=kind, document_id=record_id)
            except HubFinancePdfError as exc:
                raise HubOperationError(str(exc)) from exc
    domain = HubFinanceDocumentService(db=service.db, cipher=service.cipher)
    try:
        if kind == "invoices":
            return domain.load_invoice_pdf(invoice_id=record_id)
        if kind == "orders":
            return domain.load_order_pdf(order_id=record_id)
    except HubFinanceDocumentError as exc:
        raise HubOperationError(str(exc)) from exc
    raise HubOperationError("Keine lokale PDF vorhanden. Bei Bedarf zuerst finance.pdf.generate ausfuehren.")
