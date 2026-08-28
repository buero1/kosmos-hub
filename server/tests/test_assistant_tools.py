from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.site import Site
from app.models.site_snapshot import SiteSnapshot
from app.models.site_update_snapshot import SiteUpdateSnapshot
from app.services.assistant_tools import AssistantToolError, HubAssistantTools


def test_assistant_tools_resolve_fuzzy_customer_and_component_then_select_matching_sites():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    checked_at = datetime.now(UTC)

    with Session(engine) as db:
        customer = Customer(
            name="Autolackiererei Aschenbrenner",
            zoho_status="Aktuell",
            website_domain="autolackiererei-aschenbrenner.de",
        )
        active_site = _site(site_id=7, domain="autolackiererei-aschenbrenner.de", customer=customer)
        inactive_site = _site(site_id=8, domain="aschenbrenner-alt.de", customer=customer)
        db.add_all(
            [
                customer,
                active_site,
                inactive_site,
                SiteSnapshot(
                    site=active_site,
                    captured_at=checked_at,
                    wordpress_version="6.8",
                    php_version="8.3",
                    plugins_json=[
                        {
                            "name": "A3 Lazy Load",
                            "plugin_file": "a3-lazy-load/a3-lazy-load.php",
                            "version": "2.7.0",
                            "active": True,
                        }
                    ],
                    themes_json=[
                        {"name": "Hello Elementor", "stylesheet": "hello-elementor", "version": "3.5.0"}
                    ],
                    environment_json={},
                ),
                SiteSnapshot(
                    site=inactive_site,
                    captured_at=checked_at,
                    wordpress_version="6.8",
                    php_version="8.3",
                    plugins_json=[
                        {
                            "name": "A3 Lazy Load",
                            "plugin_file": "a3-lazy-load/a3-lazy-load.php",
                            "version": "2.7.0",
                            "active": False,
                        }
                    ],
                    themes_json=[],
                    environment_json={},
                ),
                SiteUpdateSnapshot(
                    site=active_site,
                    captured_at=checked_at,
                    core_updates_json=[],
                    plugin_updates_json=[
                        {
                            "name": "A3 Lazy Load",
                            "plugin_file": "a3-lazy-load/a3-lazy-load.php",
                            "current_version": "2.7.0",
                            "new_version": "2.8.0",
                        }
                    ],
                    theme_updates_json=[],
                    summary_json={},
                ),
                SiteUpdateSnapshot(
                    site=inactive_site,
                    captured_at=checked_at,
                    core_updates_json=[],
                    plugin_updates_json=[],
                    theme_updates_json=[],
                    summary_json={},
                ),
            ]
        )
        db.commit()

        tools = HubAssistantTools(db=db, cipher=SecretCipher("a" * 32), panel_site_ids=set())
        customers = tools.execute("search_customers", {"query": "lackiererei aschenbrunner"})
        components = tools.execute("search_components", {"query": "a13 lasy load", "kind": "plugin"})

        assert customers["matches"][0]["name"] == "Autolackiererei Aschenbrenner"
        assert components["matches"][0]["identifier"] == "a3-lazy-load/a3-lazy-load.php"

        sites = tools.execute(
            "query_sites",
            {
                "scope": "all",
                "customer_ids": [customer.id],
                "customer_status": "all",
                "customer_name_prefix": "",
                "site_ids": [],
                "component_kind": "plugin",
                "component_identifier": "a3-lazy-load/a3-lazy-load.php",
                "component_state": "active",
                "update_state": "any",
                "limit": 100,
            },
        )
        selection = tools.execute("set_site_selection", {"site_ids": [active_site.id]})
        current_customer_sites = tools.execute(
            "query_sites",
            {
                "scope": "all",
                "customer_ids": [],
                "customer_status": "Aktuell",
                "customer_name_prefix": "auto",
                "site_ids": [],
                "component_kind": "all",
                "component_identifier": "",
                "component_state": "any",
                "update_state": "any",
                "limit": 100,
            },
        )
        updates = tools.execute(
            "list_updates",
            {
                "scope": "all",
                "customer_ids": [customer.id],
                "site_ids": [],
                "component_kind": "plugin",
                "component_identifier": "a3-lazy-load/a3-lazy-load.php",
                "limit": 100,
            },
        )

        assert [site["id"] for site in sites["sites"]] == [active_site.id]
        assert selection["selected_site_ids"] == [active_site.id]
        assert {site["id"] for site in current_customer_sites["sites"]} == {active_site.id, inactive_site.id}
        assert updates["updates"][0]["target_version"] == "2.8.0"


def test_assistant_selection_tool_rejects_site_ids_not_returned_by_a_query():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        site = _site(site_id=7, domain="example.test")
        db.add(site)
        db.commit()
        tools = HubAssistantTools(db=db, cipher=SecretCipher("a" * 32), panel_site_ids=set())

        try:
            tools.execute("set_site_selection", {"site_ids": [site.id]})
        except AssistantToolError as exc:
            assert "query_sites" in str(exc)
        else:
            raise AssertionError("The model must not select arbitrary Hub sites.")


def _site(*, site_id: int, domain: str, customer: Customer | None = None) -> Site:
    return Site(
        id=site_id,
        uuid=f"a1b2c3d4-0000-4000-8000-{site_id:012d}",
        domain=domain,
        home_url=f"https://{domain}/",
        site_url=f"https://{domain}/",
        customer=customer,
    )
