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
