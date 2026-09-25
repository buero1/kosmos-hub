"""Permission-filtered conversion provenance for the shared Hub overview panel."""
from sqlalchemy import select
from sqlalchemy.orm import object_session

from app.models.customer import Customer
from app.models.hub_lead import HubLead
from app.models.hub_lead_conversion import HubLeadConversion
from app.services.hub_access_control import HubAccessControlService


def conversion_info(record, user, cipher):
    if user is None or not isinstance(record, (Customer, HubLead)):
        return None
    db = object_session(record)
    if db is None:
        return None
    column = HubLeadConversion.lead_id if isinstance(record, HubLead) else HubLeadConversion.customer_id
    conversion = db.scalar(select(HubLeadConversion).where(column == record.id))
    if conversion is None:
        return None
    links = []
    access = HubAccessControlService(db=db)
    if isinstance(record, HubLead) and conversion.customer_id and access.can_access_record(
        user=user, module_key="customers", record_id=conversion.customer_id
    ):
        customer = db.get(Customer, conversion.customer_id)
        if customer:
            links.append({"label": "Erstellter Kunde", "name": customer.name, "href": f"/customers/{customer.id}"})
    if isinstance(record, Customer) and conversion.lead_id and access.can_access_record(
        user=user, module_key="leads", record_id=conversion.lead_id
    ):
        from app.services.hub_leads import HubLeadService
        lead = HubLeadService(db=db, cipher=cipher).get_detail(lead_id=conversion.lead_id)
        if lead:
            links.append({"label": "Ursprünglicher Lead", "name": lead.name, "href": f"/leads/{lead.lead.id}"})
    return {"event": conversion, "links": links}
