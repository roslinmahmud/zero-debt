from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.db import Base


class DecimalStr(TypeDecorator):
    """Stores Decimal as text; SQLite's NUMERIC would round-trip through float."""

    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else str(Decimal(value))

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


class Account(Base):
    __tablename__ = "account"

    id: Mapped[int] = mapped_column(primary_key=True)
    bank: Mapped[str] = mapped_column(String(120))
    account_ref: Mapped[str] = mapped_column(String(4), default="")
    limit_bdt: Mapped[Decimal] = mapped_column(DecimalStr)
    day_basis: Mapped[int] = mapped_column(Integer, default=365)
    posting_frequency: Mapped[str] = mapped_column(String(10), default="monthly")
    posting_day: Mapped[str] = mapped_column(String(10), default="month_end")
    posting_grace_days: Mapped[int] = mapped_column(Integer, default=7)
    renewal_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="BDT")
    fx_eur_bdt: Mapped[Decimal | None] = mapped_column(DecimalStr, nullable=True)
    settlement_fee_pct: Mapped[Decimal] = mapped_column(DecimalStr, default=Decimal("0"))
    recon_threshold_bdt: Mapped[Decimal] = mapped_column(DecimalStr, default=Decimal("10"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class RateChange(Base):
    __tablename__ = "rate_change"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"))
    effective_date: Mapped[date] = mapped_column(Date)
    rate_pct: Mapped[Decimal] = mapped_column(DecimalStr)


class Txn(Base):
    __tablename__ = "txn"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"))
    date: Mapped[date] = mapped_column(Date, index=True)
    type: Mapped[str] = mapped_column(String(20))
    amount_bdt: Mapped[Decimal] = mapped_column(DecimalStr)
    eur_sent: Mapped[Decimal | None] = mapped_column(DecimalStr, nullable=True)
    fx_rate: Mapped[Decimal | None] = mapped_column(DecimalStr, nullable=True)
    transfer_fee_eur: Mapped[Decimal | None] = mapped_column(DecimalStr, nullable=True)
    method: Mapped[str | None] = mapped_column(String(30), nullable=True)
    proof_path: Mapped[str | None] = mapped_column(String(80), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class StatementCheckpoint(Base):
    __tablename__ = "statement_checkpoint"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"))
    date: Mapped[date] = mapped_column(Date, index=True)
    balance_bdt: Mapped[Decimal] = mapped_column(DecimalStr)
    note: Mapped[str] = mapped_column(Text, default="")
