"""Spend budgets (USD caps with forecast) and per-request receipts."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from oasis_route.api.deps import Principal, get_session, project_access, require_scope
from oasis_route.db.models import RequestReceipt, SpendBudget
from oasis_route.domain.enums import ApiKeyScope, ProjectRole
from oasis_route.domain.errors import NotFound
from oasis_route.identity import audit, service
from oasis_route.spend import budgets, receipts

router = APIRouter(prefix="/api/v1", tags=["budgets"])
READ = Depends(require_scope(ApiKeyScope.READ))
MANAGE = Depends(require_scope(ApiKeyScope.MANAGE))
Period = Literal["day", "month", "total"]


@router.get("/projects/{project_id}/budgets")
async def list_budgets(
    project_id: str, p: Principal = READ, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """USD caps with used, reserved (in-flight worst case), remaining, reset time and forecast."""
    project = await project_access(project_id, p, session)
    return {
        "project_id": project.id,
        "spend_policy": project.spend_policy,
        "budgets": [budgets.view(b) for b in await budgets.for_project(session, project.id)],
    }


class BudgetIn(BaseModel):
    limit_usd: float = Field(ge=0, le=1_000_000)


@router.put("/projects/{project_id}/budgets/{period}")
async def put_budget(
    project_id: str,
    period: Period,
    body: BudgetIn,
    p: Principal = MANAGE,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Create or change a cap. A budget limits spend; it never enables a paid route by itself."""
    project = await project_access(project_id, p, session, write=True)
    service.require_project_role(
        await service.member_role(session, project.id, p.user.id), {ProjectRole.OWNER}, p.user
    )
    row = await budgets.put(session, project.id, period, body.limit_usd)
    await audit.record(
        session,
        "project.budget_set",
        actor_user_id=p.user.id,
        actor_key_id=p.key.id,
        project_id=project.id,
        target_type="spend_budget",
        target_id=row.id,
        details={"period": period, "limit_usd": body.limit_usd},
    )
    return budgets.view(row)


@router.delete("/projects/{project_id}/budgets/{period}", status_code=204)
async def delete_budget(
    project_id: str, period: Period, p: Principal = MANAGE, session: AsyncSession = Depends(get_session)
) -> None:
    project = await project_access(project_id, p, session, write=True)
    service.require_project_role(
        await service.member_role(session, project.id, p.user.id), {ProjectRole.OWNER}, p.user
    )
    res = await session.execute(
        delete(SpendBudget).where(
            SpendBudget.project_id == project.id, SpendBudget.period == budgets.PERIODS[period].value
        )
    )
    if not getattr(res, "rowcount", 0):
        raise NotFound("no budget for this period")
    await audit.record(
        session,
        "project.budget_removed",
        actor_user_id=p.user.id,
        actor_key_id=p.key.id,
        project_id=project.id,
        details={"period": period},
    )


@router.get("/projects/{project_id}/receipts")
async def list_receipts(
    project_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    p: Principal = READ,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """One receipt per gateway request: estimate, reservation, actual usage and cost, outcome, digest."""
    await project_access(project_id, p, session)
    rows = (
        await session.execute(
            select(RequestReceipt)
            .where(RequestReceipt.project_id == project_id)
            .order_by(RequestReceipt.created_at.desc())
            .limit(limit)
        )
    ).scalars()
    return [receipts.out(r) for r in rows]


@router.get("/projects/{project_id}/receipts/{request_id}")
async def get_receipt(
    project_id: str, request_id: str, p: Principal = READ, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    await project_access(project_id, p, session)
    r = (
        await session.execute(
            select(RequestReceipt).where(
                RequestReceipt.project_id == project_id, RequestReceipt.request_id == request_id
            )
        )
    ).scalar_one_or_none()
    if r is None:
        raise NotFound("no receipt for this request id in this project")
    return receipts.out(r)
