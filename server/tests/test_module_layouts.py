import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.hub_user import HubUser
from app.services.module_layouts import ModuleLayoutError, ModuleLayoutService


def test_module_layouts_persist_global_order_and_append_new_items():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        admin = HubUser(username="operator", password_hash="hashed", role="admin")
        db.add(admin)
        db.commit()
        service = ModuleLayoutService(db=db)

        assert service.ordered_keys(layout_key="contact-fields", default_keys=("name", "email", "phone")) == (
            "name",
            "email",
            "phone",
        )
        service.configure(
            actor=admin,
            layout_key="contact-fields",
            item_order_json=json.dumps(["phone", "name", "email"]),
            allowed_keys=("name", "email", "phone"),
        )
        db.commit()

        assert service.ordered_keys(
            layout_key="contact-fields",
            default_keys=("name", "email", "phone", "mobile"),
        ) == ("phone", "name", "email", "mobile")


def test_module_layouts_persist_the_show_more_divider_position():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        admin = HubUser(username="operator", password_hash="hashed", role="admin")
        db.add(admin)
        db.commit()
        service = ModuleLayoutService(db=db)

        layout = service.configure(
            actor=admin,
            layout_key="contact-fields",
            item_order_json=json.dumps(["phone", service.SHOW_MORE_ITEM_KEY, "name", "email"]),
            allowed_keys=("name", "email", "phone"),
        )
        db.commit()

        assert json.loads(layout.item_order_json) == [
            "phone",
            service.SHOW_MORE_ITEM_KEY,
            "name",
            "email",
        ]
        assert service.ordered_keys_with_show_more(
            layout_key="contact-fields",
            default_keys=("name", "email", "phone", "mobile"),
        ) == (("phone", "name", "email", "mobile"), 1)


def test_module_layouts_use_the_module_default_until_a_divider_is_saved():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        service = ModuleLayoutService(db=db)

        assert service.ordered_keys_with_show_more(
            layout_key="customer-fields",
            default_keys=("name", "phone", "wordpress", "billing_address"),
            default_show_more_after="wordpress",
        ) == (("name", "phone", "wordpress", "billing_address"), 3)

        assert service.ordered_keys_with_show_more(
            layout_key="lead-fields",
            default_keys=("name", "company", "email"),
        ) == (("name", "company", "email"), 3)


def test_module_layouts_reject_non_admin_and_invalid_submissions():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        viewer = HubUser(username="viewer", password_hash="hashed", role="viewer")
        admin = HubUser(username="operator", password_hash="hashed", role="admin")
        db.add_all([viewer, admin])
        db.commit()
        service = ModuleLayoutService(db=db)

        with pytest.raises(ModuleLayoutError, match="administrators"):
            service.configure(
                actor=viewer,
                layout_key="contact-fields",
                item_order_json=json.dumps(["name", "email"]),
                allowed_keys=("name", "email"),
            )

        with pytest.raises(ModuleLayoutError, match="available fields"):
            service.configure(
                actor=admin,
                layout_key="contact-fields",
                item_order_json=json.dumps(["name", "unknown"]),
                allowed_keys=("name", "email"),
            )

        with pytest.raises(ModuleLayoutError, match="duplicate fields"):
            service.configure(
                actor=admin,
                layout_key="contact-fields",
                item_order_json=json.dumps(
                    ["name", service.SHOW_MORE_ITEM_KEY, service.SHOW_MORE_ITEM_KEY, "email"]
                ),
                allowed_keys=("name", "email"),
            )
