"""
routers/expenses.py – Business expense logging.

Endpoints
---------
  POST   /expenses           Log a new expense.
  GET    /expenses           List expenses (filterable by year).
  DELETE /expenses/{id}      Remove an incorrectly logged expense.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlmodel import Session, select

from app.auth import get_current_user
from app.database import get_session
from app.models import Expense, User
from app.schemas import ExpenseCreate, ExpenseRead

router = APIRouter(prefix="/expenses", tags=["expenses"])


@router.post("", response_model=ExpenseRead, status_code=status.HTTP_201_CREATED)
def create_expense(
    payload: ExpenseCreate,
    session: Session = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> ExpenseRead:
    expense = Expense(**payload.model_dump(), user_id=_user.id)
    session.add(expense)
    session.commit()
    session.refresh(expense)
    return expense


@router.get("", response_model=list[ExpenseRead])
def list_expenses(
    year: Optional[int] = None,
    session: Session = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> list[ExpenseRead]:
    """List expenses, most recent first.

    Query parameters
    ----------------
    year  Filter to a specific calendar year (matches on ``Expense.date``).
          Defaults to the current year when omitted.
    """
    filter_year = year or date.today().year
    start = date(filter_year, 1, 1)
    end = date(filter_year, 12, 31)

    stmt = (
        select(Expense)
        .where(Expense.user_id == _user.id)
        .where(Expense.date >= start)
        .where(Expense.date <= end)
        .order_by(Expense.date.desc())
    )
    return session.exec(stmt).all()


@router.delete("/{expense_id}", response_model=None)
def delete_expense(
    expense_id: int,
    session: Session = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> Response:
    """Permanently remove an expense record.

    This is intentionally a hard delete. At this scale there is no business
    requirement for soft-deletion; incorrect entries should simply be removed.
    """
    expense = session.get(Expense, expense_id)
    if expense is None or expense.user_id != _user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Expense {expense_id} not found.",
        )
    session.delete(expense)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
