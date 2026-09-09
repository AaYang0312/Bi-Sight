"""聊天会话的最小持久化与归属检查。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from psycopg.types.json import Jsonb


class ChatNotFound(Exception):
    """会话不存在，或不属于当前身份。"""


class ChatBusy(Exception):
    """同一会话已有回答在运行。"""


class ChatSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    role: str
    content: str
    artifacts: list[dict[str, object]] = Field(default_factory=list)
    status: str
    created_at: datetime


class ChatRename(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        value = value.strip()
        if not 1 <= len(value) <= 80:
            raise ValueError("标题长度应为1至80个字符")
        return value


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        value = value.strip()
        if not 1 <= len(value) <= 4000:
            raise ValueError("消息长度应为1至4000个字符")
        return value


def _summary(row: tuple[Any, ...]) -> ChatSummary:
    return ChatSummary(id=row[0], title=row[1], created_at=row[2], updated_at=row[3])


def _message(row: tuple[Any, ...]) -> ChatMessage:
    artifacts = row[3]
    if isinstance(artifacts, str):
        artifacts = json.loads(artifacts)
    return ChatMessage(
        id=row[0], role=row[1], content=row[2], artifacts=artifacts,
        status=row[4], created_at=row[5],
    )


def create_chat(conn: Any, subject: str) -> ChatSummary:
    row = conn.execute(
        """INSERT INTO bi.app_chats (id, subject_id, title)
           VALUES (%s, %s, '新对话')
           RETURNING id, title, created_at, updated_at""",
        (uuid4(), subject),
    ).fetchone()
    return _summary(row)


def list_chats(conn: Any, subject: str) -> list[ChatSummary]:
    rows = conn.execute(
        """SELECT id, title, created_at, updated_at
           FROM bi.app_chats WHERE subject_id = %s
           ORDER BY updated_at DESC, id DESC""",
        (subject,),
    ).fetchall()
    return [_summary(row) for row in rows]


def require_chat(conn: Any, subject: str, chat_id: UUID) -> None:
    row = conn.execute(
        "SELECT 1 FROM bi.app_chats WHERE id = %s AND subject_id = %s",
        (chat_id, subject),
    ).fetchone()
    if row is None:
        raise ChatNotFound


def load_messages(conn: Any, subject: str, chat_id: UUID) -> list[ChatMessage]:
    require_chat(conn, subject, chat_id)
    rows = conn.execute(
        """SELECT id, role, content, artifacts, status, created_at
           FROM bi.app_messages WHERE chat_id = %s
           ORDER BY ordinal""",
        (chat_id,),
    ).fetchall()
    return [_message(row) for row in rows]


def rename_chat(conn: Any, subject: str, chat_id: UUID, title: str) -> ChatSummary:
    row = conn.execute(
        """UPDATE bi.app_chats
           SET title = %s, title_source = 'user', updated_at = now()
           WHERE id = %s AND subject_id = %s
           RETURNING id, title, created_at, updated_at""",
        (title, chat_id, subject),
    ).fetchone()
    if row is None:
        raise ChatNotFound
    return _summary(row)


def delete_chat(conn: Any, subject: str, chat_id: UUID) -> None:
    row = conn.execute(
        "DELETE FROM bi.app_chats WHERE id = %s AND subject_id = %s RETURNING id",
        (chat_id, subject),
    ).fetchone()
    if row is None:
        raise ChatNotFound


def load_chat_context(conn: Any, subject: str, chat_id: UUID) -> tuple[dict[str, object], list[tuple[str, str]]]:
    """读取最近完整可见回合；不恢复工具调用或provider私有上下文。"""
    row = conn.execute(
        "SELECT filters FROM bi.app_chats WHERE id = %s AND subject_id = %s",
        (chat_id, subject),
    ).fetchone()
    if row is None:
        raise ChatNotFound
    filters = row[0]
    if isinstance(filters, str):
        filters = json.loads(filters)
    rows = conn.execute(
        """SELECT role, content, status FROM bi.app_messages
           WHERE chat_id = %s ORDER BY ordinal DESC LIMIT 24""",
        (chat_id,),
    ).fetchall()
    rows.reverse()
    turns: list[tuple[str, str]] = []
    pending_user: tuple[str, str] | None = None
    for role, content, status in rows:
        if role == "user":
            pending_user = (role, content)
        elif role == "assistant" and status == "complete" and pending_user is not None:
            turns.extend((pending_user, (role, content)))
            pending_user = None
    return dict(filters), turns[-12:]


def save_user_message(conn: Any, chat_id: UUID, subject: str, content: str) -> ChatMessage:
    require_chat(conn, subject, chat_id)
    title = " ".join(content.split())[:20] or "新对话"
    conn.execute(
        """UPDATE bi.app_chats SET title = %s, updated_at = now()
           WHERE id = %s AND subject_id = %s AND title_source = 'auto'
             AND NOT EXISTS (
                 SELECT 1 FROM bi.app_messages
                 WHERE chat_id = bi.app_chats.id AND role = 'user'
             )""",
        (title, chat_id, subject),
    )
    row = conn.execute(
        """INSERT INTO bi.app_messages(id, chat_id, role, content, status)
           VALUES (%s, %s, 'user', %s, 'complete')
           RETURNING id, role, content, artifacts, status, created_at""",
        (uuid4(), chat_id, content),
    ).fetchone()
    return _message(row)


def save_assistant_message(conn: Any, chat_id: UUID, subject: str, content: str,
                           artifacts: list[dict[str, object]], *, status: str) -> ChatMessage:
    require_chat(conn, subject, chat_id)
    row = conn.execute(
        """INSERT INTO bi.app_messages(id, chat_id, role, content, artifacts, status)
           VALUES (%s, %s, 'assistant', %s, %s, %s)
           RETURNING id, role, content, artifacts, status, created_at""",
        (uuid4(), chat_id, content, Jsonb(artifacts), status),
    ).fetchone()
    conn.execute("UPDATE bi.app_chats SET updated_at = now() WHERE id = %s", (chat_id,))
    return _message(row)


def update_chat_filters(conn: Any, chat_id: UUID, subject: str,
                        filters: dict[str, object]) -> None:
    require_chat(conn, subject, chat_id)
    conn.execute(
        "UPDATE bi.app_chats SET filters = %s, updated_at = now() WHERE id = %s",
        (Jsonb(filters), chat_id),
    )


def claim_chat_turn(conn: Any, chat_id: UUID, subject: str) -> None:
    require_chat(conn, subject, chat_id)
    locked = conn.execute(
        "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (str(chat_id),),
    ).fetchone()[0]
    if not locked:
        raise ChatBusy


def release_chat_turn(conn: Any, chat_id: UUID) -> None:
    conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (str(chat_id),))
