from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.update_plan import UpdatePlan, UpdatePlanItem


class UpdatePlanRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, *, name: str, created_by: str, notes: str | None = None) -> UpdatePlan:
        plan = UpdatePlan(name=name, created_by=created_by, notes=notes)
        self.db.add(plan)
        self.db.flush()
        return plan

    def list(self, *, limit: int = 100) -> list[UpdatePlan]:
        statement = (
            select(UpdatePlan)
            .options(selectinload(UpdatePlan.items))
            .order_by(UpdatePlan.created_at.desc())
            .limit(limit)
        )
        return list(self.db.scalars(statement))

    def get(self, plan_id: int) -> UpdatePlan | None:
        statement = (
            select(UpdatePlan)
            .where(UpdatePlan.id == plan_id)
            .options(selectinload(UpdatePlan.items).selectinload(UpdatePlanItem.site))
        )
        return self.db.scalar(statement)
