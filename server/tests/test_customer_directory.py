from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.customer import Customer
from app.models.site import Site
from app.services.customer_directory import CustomerDirectoryService


def _site(*, site_id: int, domain: str, customer_id: int | None = None) -> Site:
    return Site(
        id=site_id,
        uuid=f"a1b2c3d4-0000-4000-8000-{site_id:012d}",
        domain=domain,
        home_url=f"https://{domain}/",
        site_url=f"https://{domain}/",
        customer_id=customer_id,
    )


def test_customer_directory_requires_explicit_review_before_linking_exact_domain():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Example Customer", zoho_id="zoho-1", website_domain="example-customer.de")
        site = _site(site_id=1, domain="www.example-customer.de")
        db.add_all([customer, site])
        db.commit()

        service = CustomerDirectoryService(db=db)
        entry = service.list_entries(candidates_only=True)[0]
        assert entry.customer.id == customer.id
        assert entry.exact_match_candidate is not None
        assert entry.exact_match_candidate.id == site.id
        assert db.get(Site, site.id).customer_id is None

        service.link_exact_match(customer_id=customer.id, site_id=site.id)
        assert db.get(Site, site.id).customer_id == customer.id


def test_customer_directory_rejects_non_matching_or_ambiguous_sites():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Example Customer", zoho_id="zoho-1", website_domain="example-customer.de")
        same_domain_first = _site(site_id=1, domain="example-customer.de")
        same_domain_second = _site(site_id=2, domain="www.example-customer.de")
        other_site = _site(site_id=3, domain="other-customer.de")
        db.add_all([customer, same_domain_first, same_domain_second, other_site])
        db.commit()

        service = CustomerDirectoryService(db=db)
        assert service.list_entries(candidates_only=True) == []

        try:
            service.link_exact_match(customer_id=customer.id, site_id=other_site.id)
        except ValueError as exc:
            assert "exact Zoho website-domain match" in str(exc)
        else:
            raise AssertionError("A non-matching site must not be linkable.")

        try:
            service.link_exact_match(customer_id=customer.id, site_id=same_domain_first.id)
        except ValueError as exc:
            assert "ambiguous" in str(exc)
        else:
            raise AssertionError("An ambiguous domain must not be linkable.")
