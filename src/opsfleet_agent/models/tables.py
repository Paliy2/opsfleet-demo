from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID, uuid4

from sqlalchemy import JSON, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSON}


class AppUser(Base):
    __tablename__ = "app_users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(16))
    display_name: Mapped[str] = mapped_column(String(128))


class Entitlement(Base):
    __tablename__ = "entitlements"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), primary_key=True
    )
    brand: Mapped[str] = mapped_column(String(256), primary_key=True)


class ChatSessionRow(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ChatMessageRow(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (Index("ix_chat_messages_session_created", "session_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ResultSetRow(Base):
    __tablename__ = "result_sets"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True
    )
    sql: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
