import hashlib
import json
import os
import re
from contextvars import ContextVar
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

_current_owner_id: ContextVar[Optional[int]] = ContextVar(
    "life_inbox_owner_id", default=None
)

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    create_engine,
    case,
    func,
    event,
    or_,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, sessionmaker, object_session

from app.capture.pipeline import (
    ANALYZABLE_STATUSES,
    ANALYZABLE_TYPES,
    STATUS_CAPTURED,
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_READY,
    IMAGE_ITEM_TYPE,
    PDF_ITEM_TYPE,
    URL_ITEM_TYPE,
    VOICE_ITEM_TYPE,
)
from app.capture.storage import delete_stored_file
from app.similarity import find_item_relations
from app.attention.dates import configured_timezone, parse_timing_phrase, reference_date, timing_intent_signature


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "life_inbox.db"
DEFAULT_DATABASE_URL = f"sqlite:///{DEFAULT_DB_PATH}"


def normalize_database_url(url: str) -> str:
    """Point bare postgres URLs (as hosts like Render supply) at psycopg 3.

    Only psycopg 3 is installed; SQLAlchemy would otherwise look for psycopg2.
    """
    url = url.strip()
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


DATABASE_URL = normalize_database_url(
    os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
)
AUTO_CREATE_TABLES = os.getenv(
    "LIFE_INBOX_AUTO_CREATE_TABLES", ""
).strip().lower() in {"1", "true", "yes"}


def _connect_args(database_url: str) -> dict:
    if database_url.startswith("sqlite"):
        return {"check_same_thread": False}
    return {}


# Hosted Postgres (for example Neon) closes idle connections when it suspends.
# Pre-ping replaces a dropped pooled connection instead of failing the request.
engine = create_engine(
    DATABASE_URL,
    connect_args=_connect_args(DATABASE_URL),
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
    """SQLite only enforces CASCADE when foreign_keys pragma is on."""
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


TEXT_ITEM_TYPE = "text"
READY_STATUS = STATUS_READY
ANALYSIS_RUNNING = "running"
ANALYSIS_SUCCEEDED = "succeeded"
ANALYSIS_FAILED = "failed"
TASK_OPEN = "open"
TASK_COMPLETED = "completed"
TASK_DISMISSED = "dismissed"
TASK_STATUSES = {TASK_OPEN, TASK_COMPLETED, TASK_DISMISSED}
DEFAULT_OWNER_EMAIL = "local@life-inbox.local"


class UserRecord(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        UniqueConstraint("google_sub", name="uq_users_google_sub"),
        {"sqlite_autoincrement": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(Text, nullable=False)
    display_name = Column(Text, nullable=False)
    auth_provider = Column(Text, nullable=False)
    google_sub = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False)


class InboxItemRecord(Base):
    __tablename__ = "inbox_items"
    __table_args__ = {"sqlite_autoincrement": True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_type = Column(Text, nullable=False)
    title = Column(Text, nullable=True)
    raw_text = Column(Text, nullable=True)
    source_url = Column(Text, nullable=True)
    original_filename = Column(Text, nullable=True)
    mime_type = Column(Text, nullable=True)
    processing_status = Column(Text, nullable=False)
    error_message = Column(Text, nullable=True)
    storage_path = Column(Text, nullable=True)
    extracted_text = Column(Text, nullable=True)
    ai_description = Column(Text, nullable=True)
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(Text, nullable=False)
    updated_at = Column(Text, nullable=True)
    time_reference_at = Column(Text, nullable=True)
    time_reference_timezone = Column(Text, nullable=True)
    task_analysis_fingerprint = Column(Text, nullable=True)


class AnalysisRunRecord(Base):
    __tablename__ = "analysis_runs"
    __table_args__ = {"sqlite_autoincrement": True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(Text, nullable=False)
    completed_at = Column(Text, nullable=True)
    status = Column(Text, nullable=False)
    mode = Column(Text, nullable=False)
    result_json = Column(Text, nullable=True)
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)


class TopicRecord(Base):
    __tablename__ = "topics"
    __table_args__ = (
        UniqueConstraint("name", "owner_user_id", name="uq_topics_name_owner"),
        {"sqlite_autoincrement": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(Text, nullable=False)
    updated_at = Column(Text, nullable=False)
    archived = Column(Integer, nullable=False, default=0)


class ItemAnalysisStateRecord(Base):
    __tablename__ = "item_analysis_state"
    item_id = Column(Integer, ForeignKey("inbox_items.id", ondelete="CASCADE"), primary_key=True)
    input_fingerprint = Column(Text, nullable=True)
    analysis_version = Column(Text, nullable=True)
    status = Column(Text, nullable=False, default="pending", server_default="pending")
    invalidation_generation = Column(Integer, nullable=False, default=0, server_default="0")
    processed_generation = Column(Integer, nullable=False, default=0, server_default="0")
    claimed_fingerprint = Column(Text, nullable=True)
    claim_token = Column(Text, nullable=True)
    claimed_at = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    last_run_id = Column(Integer, ForeignKey("analysis_runs.id"), nullable=True)


class TaskDetailRecord(Base):
    __tablename__ = "task_details"
    __table_args__ = (UniqueConstraint("task_id", "source_item_id", "text", name="uq_task_detail_source_text"),)
    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    source_item_id = Column(Integer, ForeignKey("inbox_items.id", ondelete="CASCADE"), nullable=False)
    text = Column(Text, nullable=False)
    created_at = Column(Text, nullable=False)


class AnalysisReviewRecord(Base):
    __tablename__ = "analysis_reviews"
    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    run_id = Column(Integer, ForeignKey("analysis_runs.id"), nullable=False)
    item_id = Column(Integer, ForeignKey("inbox_items.id", ondelete="CASCADE"), nullable=False)
    candidate_task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True)
    kind = Column(Text, nullable=False)
    reason = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="pending", server_default="pending")
    dedup_key = Column(Text, nullable=False, unique=True)
    created_at = Column(Text, nullable=False)


class ItemInsightRecord(Base):
    __tablename__ = "item_insights"

    item_id = Column(
        Integer,
        ForeignKey("inbox_items.id", ondelete="CASCADE"),
        primary_key=True,
    )
    analysis_run_id = Column(
        Integer,
        ForeignKey("analysis_runs.id"),
        nullable=True,
    )
    category = Column(Text, nullable=False)
    concise_title = Column(Text, nullable=False)
    priority = Column(Text, nullable=False)
    topic_id = Column(Integer, ForeignKey("topics.id"), nullable=True)
    tags_json = Column(Text, nullable=True)
    rationale = Column(Text, nullable=True)
    user_title = Column(Text, nullable=True)
    user_topic_id = Column(Integer, ForeignKey("topics.id"), nullable=True)
    user_priority = Column(Text, nullable=True)
    user_tags_json = Column(Text, nullable=True)
    needs_review = Column(Integer, nullable=False, default=0)
    updated_at = Column(Text, nullable=False)


class ItemRelationRecord(Base):
    __tablename__ = "item_relations"
    __table_args__ = (
        UniqueConstraint(
            "item_id_a",
            "item_id_b",
            "relation_type",
            name="uq_item_relations_pair_type",
        ),
        {"sqlite_autoincrement": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id_a = Column(
        Integer,
        ForeignKey("inbox_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_id_b = Column(
        Integer,
        ForeignKey("inbox_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    relation_type = Column(Text, nullable=False)
    score = Column(Text, nullable=False)
    created_at = Column(Text, nullable=False)
    dismissed = Column(Integer, nullable=False, default=0)


class TaskRecord(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "priority IN ('High', 'Medium', 'Low')",
            name="ck_tasks_priority",
        ),
        CheckConstraint(
            "status IN ('open', 'completed', 'dismissed')",
            name="ck_tasks_status",
        ),
        UniqueConstraint("dedup_key"),
        {"sqlite_autoincrement": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    text = Column(Text, nullable=False)
    priority = Column(Text, nullable=False)
    user_text = Column(Text, nullable=True)
    user_priority = Column(Text, nullable=True)
    timing_text = Column(Text, nullable=True)
    ai_timing_text = Column(Text, nullable=True)
    timing_confirmed = Column(Integer, nullable=False, default=0)
    due_on = Column(Text, nullable=True)
    due_confidence = Column(Text, nullable=True)
    deadline_status = Column(Text, nullable=False, default="legacy", server_default="legacy")
    deadline_needs_review = Column(Integer, nullable=False, default=1, server_default="1")
    deadline_note = Column(Text, nullable=True)
    deadline_reference_at = Column(Text, nullable=True)
    deadline_timezone = Column(Text, nullable=True)
    deadline_source_fingerprint = Column(Text, nullable=True)
    snoozed_until = Column(Text, nullable=True)
    reminder_at = Column(Text, nullable=True)
    status = Column(Text, nullable=False)
    dedup_key = Column(Text, nullable=False)
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(Text, nullable=False)
    updated_at = Column(Text, nullable=False)


class CalendarActionReceiptRecord(Base):
    __tablename__ = "calendar_action_receipts"
    __table_args__ = {"sqlite_autoincrement": True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    draft_id = Column(Text, nullable=False)
    task_id = Column(
        Integer,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider = Column(Text, nullable=False)
    status = Column(Text, nullable=False)
    payload_json = Column(Text, nullable=False)
    created_at = Column(Text, nullable=False)


class TaskSourceItemRecord(Base):
    __tablename__ = "task_source_items"

    task_id = Column(
        Integer,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    item_id = Column(
        Integer,
        ForeignKey("inbox_items.id", ondelete="CASCADE"),
        primary_key=True,
    )


DEFAULT_TOPIC_NAMES = (
    "Work",
    "Money & Admin",
    "Learning",
    "Shopping & Errands",
    "Health",
    "Travel",
    "Ideas",
    "People",
    "Personal / Reference",
)


def set_current_owner_id(owner_user_id: Optional[int]) -> None:
    _current_owner_id.set(owner_user_id)


def get_current_owner_id() -> Optional[int]:
    return _current_owner_id.get()


def _user_to_dict(user: UserRecord) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "auth_provider": user.auth_provider,
        "google_sub": user.google_sub,
        "created_at": user.created_at,
    }


def get_user_by_id(user_id: int) -> Optional[dict]:
    with SessionLocal() as session:
        user = session.get(UserRecord, user_id)
        return _user_to_dict(user) if user else None


def get_user_by_email(email: str) -> Optional[dict]:
    with SessionLocal() as session:
        user = session.scalar(
            select(UserRecord).where(UserRecord.email == email.strip().lower())
        )
        return _user_to_dict(user) if user else None


def get_user_by_google_sub(google_sub: str) -> Optional[dict]:
    cleaned = (google_sub or "").strip()
    if not cleaned:
        return None
    with SessionLocal() as session:
        user = session.scalar(
            select(UserRecord).where(UserRecord.google_sub == cleaned)
        )
        return _user_to_dict(user) if user else None


def ensure_user(
    *,
    email: str,
    display_name: str,
    auth_provider: str,
    google_sub: Optional[str] = None,
) -> dict:
    cleaned = email.strip().lower()
    subject = (google_sub or "").strip() or None
    with SessionLocal() as session:
        user = None
        if subject:
            user = session.scalar(
                select(UserRecord).where(UserRecord.google_sub == subject)
            )
        if user is None:
            user = session.scalar(
                select(UserRecord).where(UserRecord.email == cleaned)
            )
        if user is None:
            user = UserRecord(
                email=cleaned,
                display_name=display_name.strip() or cleaned,
                auth_provider=auth_provider,
                google_sub=subject,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            session.add(user)
            session.commit()
            return _user_to_dict(user)

        changed = False
        if display_name and user.display_name != display_name.strip():
            user.display_name = display_name.strip()
            changed = True
        if subject and user.google_sub != subject:
            user.google_sub = subject
            changed = True
        if auth_provider and user.auth_provider != auth_provider:
            # Prefer google once linked; do not downgrade google → local/dev.
            if auth_provider == "google" or user.auth_provider != "google":
                user.auth_provider = auth_provider
                changed = True
        if user.email != cleaned:
            user.email = cleaned
            changed = True
        if changed:
            session.commit()
        return _user_to_dict(user)


def get_or_create_default_owner_id() -> int:
    user = ensure_user(
        email=DEFAULT_OWNER_EMAIL,
        display_name="Local Owner",
        auth_provider="local",
    )
    return user["id"]


class OwnerRequiredError(PermissionError):
    """No owner is bound to the current request; user data is off limits."""


def require_owner_id() -> int:
    """Return the bound owner. Never falls back to Local Owner.

    Only the HTTP middleware decides when Local Owner applies (local mode with
    no session). Everywhere else, a missing owner fails closed.
    """
    owner = get_current_owner_id()
    if owner is None:
        raise OwnerRequiredError("Authentication required.")
    return owner


def _owned_item(session, item_id: int, owner_id: int) -> Optional["InboxItemRecord"]:
    item = session.get(InboxItemRecord, item_id)
    if item is None or item.owner_user_id != owner_id:
        return None
    return item


def _owned_task(session, task_id: int, owner_id: int, *, for_update=False) -> Optional["TaskRecord"]:
    task = session.scalar(select(TaskRecord).where(
        TaskRecord.id == task_id, TaskRecord.owner_user_id == owner_id
    ).with_for_update()) if for_update else session.get(TaskRecord, task_id)
    if task is None or task.owner_user_id != owner_id:
        return None
    return task


def _owned_topic(session, topic_id: int, owner_id: int) -> Optional["TopicRecord"]:
    topic = session.get(TopicRecord, topic_id)
    if topic is None or topic.owner_user_id != owner_id:
        return None
    return topic


def _owned_item_ids(owner_id: int):
    return select(InboxItemRecord.id).where(
        InboxItemRecord.owner_user_id == owner_id
    )


def _owned_topic_ids(session, owner_id: int) -> set[int]:
    return set(
        session.scalars(
            select(TopicRecord.id).where(TopicRecord.owner_user_id == owner_id)
        )
    )


def ensure_default_topics(session, owner_user_id: Optional[int] = None) -> None:
    """Seed missing default topics for one owner.

    A name already exists only when this owner has it. Another user's Work
    is not reused and does not block this owner's copy.
    """
    owner = owner_user_id if owner_user_id is not None else require_owner_id()
    existing_names = set(
        session.scalars(
            select(TopicRecord.name).where(TopicRecord.owner_user_id == owner)
        )
    )
    missing = [
        name for name in DEFAULT_TOPIC_NAMES if name not in existing_names
    ]
    if not missing:
        return
    now = datetime.now(timezone.utc).isoformat()
    for name in missing:
        session.add(
            TopicRecord(
                name=name,
                description=None,
                owner_user_id=owner,
                created_at=now,
                updated_at=now,
                archived=0,
            )
        )
    session.flush()


def init_db():
    """Prepare the DB connection.

    Alembic owns schema evolution. Table auto-creation is opt-in for isolated
    automated tests via LIFE_INBOX_AUTO_CREATE_TABLES=1.
    """
    if AUTO_CREATE_TABLES:
        Base.metadata.create_all(bind=engine)
        with SessionLocal() as session:
            owner = ensure_user(
                email=DEFAULT_OWNER_EMAIL,
                display_name="Local Owner",
                auth_provider="local",
            )
            ensure_default_topics(session, owner_user_id=owner["id"])
            session.commit()


def _default_item_title(item: InboxItemRecord) -> str:
    for value in (item.title, item.original_filename, item.extracted_text, item.raw_text, item.source_url):
        if value and value.strip():
            return value.strip().splitlines()[0][:160]
    return "Untitled capture"


def _pending_item_insight(item: InboxItemRecord) -> ItemInsightRecord:
    """A user-editable placeholder, explicitly not a successful model result."""
    return ItemInsightRecord(item_id=item.id, analysis_run_id=None,
        category="Unorganized", concise_title=_default_item_title(item), priority="Medium",
        topic_id=None, tags_json=None, rationale=None, needs_review=0,
        updated_at=item.created_at)


def _item_to_dict(item: InboxItemRecord, *, insight=None, topics=None, prefetched=False) -> dict:
    session = object_session(item)
    if session is not None and not prefetched:
        insight = session.get(ItemInsightRecord, item.id)
        topics = {topic.id: topic for topic in session.scalars(select(TopicRecord).where(
            TopicRecord.owner_user_id == item.owner_user_id)).all()}
    topics = topics or {}
    value = _insight_to_dict(insight or _pending_item_insight(item), set(topics))
    topic = topics.get(value["effective_topic_id"])
    return {
        "id": item.id,
        "item_type": item.item_type,
        "title": item.title,
        "raw_text": item.raw_text,
        "source_url": item.source_url,
        "original_filename": item.original_filename,
        "mime_type": item.mime_type,
        "processing_status": item.processing_status,
        "error_message": item.error_message,
        "storage_path": item.storage_path,
        "extracted_text": item.extracted_text,
        "ai_description": item.ai_description,
        "owner_user_id": item.owner_user_id,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "time_reference_at": item.time_reference_at,
        "time_reference_timezone": item.time_reference_timezone,
        "effective_title": value["effective_title"],
        "effective_priority": value["effective_priority"],
        "effective_topic_id": value["effective_topic_id"],
        "effective_topic_name": topic.name if topic else None,
        "analysis_pending": value["analysis_pending"],
    }


def add_item_to_db(raw_text: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    owner_id = require_owner_id()
    with SessionLocal() as session:
        item = InboxItemRecord(
            item_type=TEXT_ITEM_TYPE,
            raw_text=raw_text,
            processing_status=READY_STATUS,
            owner_user_id=owner_id,
            created_at=now,
            time_reference_at=now,
            time_reference_timezone=str(configured_timezone()),
        )
        session.add(item)
        session.commit()
        return _item_to_dict(item)


def list_items_from_db(
    search: Optional[str] = None,
    item_type: Optional[str] = None,
    category: Optional[str] = None,
    priority: Optional[str] = None,
    processing_status: Optional[str] = None,
    limit: Optional[int] = 100,
    offset: int = 0,
    topic_id: Optional[int] = None,
    sort: str = "oldest",
) -> list[dict]:
    owner_id = require_owner_id()
    with SessionLocal() as session:
        topics = {topic.id: topic for topic in session.scalars(select(TopicRecord).where(
            TopicRecord.owner_user_id == owner_id)).all()}
        # Ignore invalid historical foreign-topic links before applying fallback,
        # exactly as _insight_to_dict does for direct reads.
        effective_topic = func.coalesce(
            case((ItemInsightRecord.user_topic_id.in_(topics), ItemInsightRecord.user_topic_id)),
            case((ItemInsightRecord.topic_id.in_(topics), ItemInsightRecord.topic_id)),
        )
        effective_priority = func.coalesce(ItemInsightRecord.user_priority, ItemInsightRecord.priority, "Medium")
        statement = select(InboxItemRecord).outerjoin(ItemInsightRecord,
            ItemInsightRecord.item_id == InboxItemRecord.id).outerjoin(TopicRecord,
            (TopicRecord.id == effective_topic) & (TopicRecord.owner_user_id == owner_id)
        ).where(InboxItemRecord.owner_user_id == owner_id)
        search_text = (search or "").strip()
        if search_text:
            statement = statement.where(or_(
                InboxItemRecord.raw_text.icontains(search_text, autoescape=True),
                InboxItemRecord.title.icontains(search_text, autoescape=True),
                ItemInsightRecord.user_title.icontains(search_text, autoescape=True),
                ItemInsightRecord.concise_title.icontains(search_text, autoescape=True),
                InboxItemRecord.extracted_text.icontains(search_text, autoescape=True),
                InboxItemRecord.original_filename.icontains(search_text, autoescape=True),
            ))
        if item_type:
            statement = statement.where(InboxItemRecord.item_type == item_type)
        if category:
            statement = statement.where(func.coalesce(TopicRecord.name, ItemInsightRecord.category) == category)
        if topic_id is not None:
            statement = statement.where(effective_topic == topic_id)
        if priority:
            statement = statement.where(effective_priority == priority)
        if processing_status:
            statement = statement.where(InboxItemRecord.processing_status == processing_status)
        statement = statement.order_by(InboxItemRecord.id.desc() if sort == "newest" else InboxItemRecord.id.asc()).offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        items = session.scalars(statement).all()
        insights = {row.item_id: row for row in session.scalars(select(ItemInsightRecord).where(
            ItemInsightRecord.item_id.in_([item.id for item in items]))).all()}
        return [_item_to_dict(item, insight=insights.get(item.id), topics=topics, prefetched=True) for item in items]


def compute_inbox_fingerprint(session=None) -> str:
    """Stable hash of current inbox content used to detect stale analysis."""
    owner_id = require_owner_id()

    def _fingerprint(active_session) -> str:
        items = active_session.scalars(
            select(InboxItemRecord)
            .where(InboxItemRecord.owner_user_id == owner_id)
            .order_by(InboxItemRecord.id.asc())
        ).all()
        payload = [
            [
                item.id,
                item.item_type,
                item.raw_text or "",
                item.title or "",
                item.source_url or "",
                item.processing_status,
                item.updated_at or item.created_at,
            ]
            for item in items
        ]
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    if session is not None:
        return _fingerprint(session)
    with SessionLocal() as owned_session:
        return _fingerprint(owned_session)


def update_item_in_db(item_id: int, raw_text: str) -> Optional[dict]:
    owner_id = require_owner_id()
    with SessionLocal() as session:
        item = _owned_item(session, item_id, owner_id)
        if item is None or item.item_type != TEXT_ITEM_TYPE:
            return None

        if item.raw_text != raw_text:
            now = datetime.now(timezone.utc).isoformat()
            _update_source_time_reference(item, item.raw_text or "", raw_text, now)
            item.raw_text = raw_text
            item.updated_at = now
        session.commit()
        return _item_to_dict(item)


def _update_source_time_reference(item: InboxItemRecord, old_text: str, new_text: str, now: str) -> None:
    if timing_intent_signature(old_text) == timing_intent_signature(new_text):
        return
    from app.attention.dates import split_timing_sentences

    # Before the first analysis, mixed-obligation timing edits have no per-task
    # provenance to recover. Require review rather than assign one edit clock
    # to all the old relative phrases in that source.
    timed_obligations = [part for part in split_timing_sentences(old_text) if timing_intent_signature(part)]
    new_timed = [part for part in split_timing_sentences(new_text) if timing_intent_signature(part)]
    mixed_unanalyzed = timed_obligations and max(len(timed_obligations), len(new_timed)) > 1 and item.task_analysis_fingerprint is None
    item.time_reference_at = None if mixed_unanalyzed else now
    item.time_reference_timezone = str(configured_timezone())


def _remove_orphan_tasks(session, item_ids: list[int]) -> None:
    """Drop source links for deleted items and remove tasks with no sources."""
    if not item_ids:
        return

    affected_task_ids = session.scalars(
        select(TaskSourceItemRecord.task_id).where(
            TaskSourceItemRecord.item_id.in_(item_ids)
        )
    ).all()
    for link in session.scalars(
        select(TaskSourceItemRecord).where(
            TaskSourceItemRecord.item_id.in_(item_ids)
        )
    ).all():
        session.delete(link)
    session.flush()

    for task_id in sorted(set(affected_task_ids)):
        remaining = session.scalars(
            select(TaskSourceItemRecord.item_id).where(
                TaskSourceItemRecord.task_id == task_id
            )
        ).all()
        if remaining:
            task = session.get(TaskRecord, task_id)
            if task is not None:
                task.dedup_key = _task_dedup_key(list(remaining), task.text)
            continue
        task = session.get(TaskRecord, task_id)
        if task is not None:
            session.delete(task)


def delete_item_from_db(item_id: int) -> bool:
    owner_id = require_owner_id()
    with SessionLocal() as session:
        item = _owned_item(session, item_id, owner_id)
        if item is None:
            return False

        insight = session.get(ItemInsightRecord, item_id)
        if insight is not None:
            session.delete(insight)
        _remove_orphan_tasks(session, [item_id])
        storage_path = item.storage_path
        session.delete(item)
        session.commit()
        delete_stored_file(storage_path)
        return True


def _item_to_note(item: dict) -> dict:
    return {
        "id": item["id"],
        "text": item["raw_text"],
        "created_at": item["created_at"],
    }


def add_note_to_db(text: str) -> dict:
    return _item_to_note(add_item_to_db(text))


def list_notes_from_db() -> list[dict]:
    """Analyzable ready items exposed in the legacy note shape for the organizer."""
    notes = []
    for item in list_items_from_db(limit=None, offset=0):
        if item["item_type"] not in ANALYZABLE_TYPES:
            continue
        if item["processing_status"] not in ANALYZABLE_STATUSES:
            continue
        text = item.get("extracted_text") or item.get("raw_text")
        if not text:
            continue
        notes.append(
            {
                "id": item["id"],
                "text": text,
                "created_at": item["created_at"],
                "source_fingerprint": _deadline_fingerprint_values(
                    [[item["id"], text, item["time_reference_at"], item["time_reference_timezone"]]]
                ),
            }
        )
    return notes


def update_note_in_db(note_id: int, text: str) -> Optional[dict]:
    item = update_item_in_db(note_id, text)
    return _item_to_note(item) if item is not None else None


def delete_note_from_db(note_id: int) -> bool:
    return delete_item_from_db(note_id)


def start_analysis_run() -> int:
    started_at = datetime.now(timezone.utc).isoformat()
    owner_id = require_owner_id()
    with SessionLocal() as session:
        run = AnalysisRunRecord(
            started_at=started_at,
            status=ANALYSIS_RUNNING,
            mode="pending",
            owner_user_id=owner_id,
        )
        session.add(run)
        session.commit()
        return run.id


def _topic_id_by_name(session, name: str) -> Optional[int]:
    owner_id = require_owner_id()
    ensure_default_topics(session, owner_user_id=owner_id)
    topic = session.scalars(
        select(TopicRecord).where(
            TopicRecord.name == name,
            TopicRecord.owner_user_id == owner_id,
        )
    ).first()
    if topic is not None:
        return topic.id
    now = datetime.now(timezone.utc).isoformat()
    topic = TopicRecord(
        name=name,
        description="Created from AI organization",
        owner_user_id=owner_id,
        created_at=now,
        updated_at=now,
        archived=0,
    )
    session.add(topic)
    session.flush()
    return topic.id


def _normalize_task_text(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9\s]+", " ", (text or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _task_action_identity(text: str) -> str:
    """Match the obligation when its trailing date wording was edited."""
    from app.attention.dates import extract_timing_phrase

    text = re.sub(r"^(?:today|tomorrow|yesterday)\b[\s,:;-]*", "", text, flags=re.I)
    phrase = extract_timing_phrase(text)
    if phrase and text.endswith(phrase):
        action = text[:-len(phrase)].strip()
        if action:
            text = action
    return " ".join(word for word in _normalize_task_text(text).split() if word not in {"the", "a", "an"})


def _deadline_sources(session, source_item_ids: list[int], *, lock: bool = False) -> list[InboxItemRecord]:
    owner_id = require_owner_id()
    statement = select(InboxItemRecord).where(
        InboxItemRecord.id.in_(source_item_ids),
        InboxItemRecord.owner_user_id == owner_id,
    ).order_by(InboxItemRecord.id.asc())
    if lock:
        statement = statement.with_for_update()
    return list(session.scalars(statement).all())


def _deadline_fingerprint(sources: list[InboxItemRecord]) -> str:
    payload = [
        [item.id, item.extracted_text or item.raw_text or "",
         item.time_reference_at, item.time_reference_timezone]
        for item in sources
    ]
    return _deadline_fingerprint_values(payload)


def _deadline_fingerprint_values(payload: list) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


class SourceChangedDuringAnalysis(ValueError):
    pass


def _source_deadline(todo: dict, sources: list[InboxItemRecord]) -> dict:
    """Resolve source wording against its saved capture/edit context, never now."""
    from app.attention.dates import extract_timing_phrase, split_timing_sentences

    wording = (todo.get("when") or "").strip()
    context = None
    # Search every source before falling back to action wording. Expand matching
    # source sentences so a model cannot discard a time such as 'at 10a.m.'.
    sentences = [part for item in sources for part in split_timing_sentences(item.extracted_text or item.raw_text or "")]
    matching = [part for part in sentences if
                _task_action_identity(part) == _task_action_identity(todo["task"])
                or todo["task"].lower() in part.lower()]
    if matching and len(matching) > 1 and wording:
        narrowed = [part for part in matching if wording.lower() in part.lower()]
        matching = narrowed or matching
    if not matching:
        matching = [part for part in sentences if wording and wording.lower() in part.lower()]
    if matching or (len(sources) == 1 and len(sentences) == 1):
        context = " . ".join(matching or sentences)
    phrase = extract_timing_phrase(context) if context else None
    if not phrase and wording:
        phrase = wording
        if not context or wording.lower() not in context.lower():
            context = None
    if not phrase:
        unmatched = [extract_timing_phrase(item.extracted_text or item.raw_text or "") for item in sources]
        phrase = " / ".join(part for part in unmatched if part) or None
        if phrase:
            context = None
    base = {
        "timing_text": phrase,
        "ai_timing_text": phrase if context else " / ".join(
            part for item in sources
            if (part := extract_timing_phrase(item.extracted_text or item.raw_text or ""))
        ) or None,
        "deadline_status": "inferred",
        "deadline_reference_at": sources[0].time_reference_at if sources else None,
        "deadline_timezone": sources[0].time_reference_timezone if sources else None,
        "deadline_source_fingerprint": _deadline_fingerprint(sources),
        "due_on": None,
        "due_confidence": "none",
        "deadline_needs_review": 0,
        "deadline_note": "No deadline was found in the source wording.",
    }
    if not phrase:
        return base
    if not context:
        return {**base, "deadline_needs_review": 1,
                "deadline_note": "Suggested timing was not found in the original source. Choose a date."}
    references = [reference_date(item.time_reference_at, item.time_reference_timezone) for item in sources]
    if not references or any(ref is None for ref in references):
        if any(item.time_reference_at is None and item.time_reference_timezone and item.updated_at for item in sources):
            return {**base, "deadline_needs_review": 1,
                    "deadline_note": "Timing changed across multiple obligations before analysis. Review each date; no single reference was assumed for the mixed edit."}
        return {**base, "deadline_needs_review": 1,
                "deadline_note": "Historical capture timezone or reference time is unknown. Choose a date; no historical date was guessed."}
    if len(set(references)) != 1 or len({item.time_reference_timezone for item in sources}) != 1:
        return {**base, "deadline_needs_review": 1,
                "deadline_note": "Sources have different local reference dates or timezones. Choose the intended deadline."}
    parsed = parse_timing_phrase(phrase, reference=references[0])
    return {**base, "due_on": parsed.normalized_iso,
            "due_confidence": parsed.confidence,
            "deadline_needs_review": int(parsed.needs_confirmation),
            "deadline_note": parsed.note}


def _reconcile_inferred_deadline(task: TaskRecord, inferred: dict) -> None:
    previous = timing_intent_signature(task.ai_timing_text or task.timing_text, unique=True)
    current = timing_intent_signature(inferred["ai_timing_text"] or inferred["timing_text"], unique=True)
    if previous == current and not inferred["deadline_needs_review"]:
        # Acknowledge the content version without renewing 'today/tomorrow'.
        task.deadline_source_fingerprint = inferred["deadline_source_fingerprint"]
        return
    if previous != current and current and inferred["due_on"] and not inferred["deadline_needs_review"]:
        for key, value in inferred.items():
            setattr(task, key, value)
        return
    task.deadline_needs_review = 1
    wording = inferred["ai_timing_text"] or inferred["timing_text"] or "(timing removed)"
    task.deadline_note = (
        "Source timing changed ambiguously. The previous deadline and reference were retained. "
        f"Review the source and choose or clear a date. New wording: {wording[:400]}"
    )


def _match_edited_task(candidates: list[TaskRecord], text: str):
    target = _task_action_identity(text)
    exact = [candidate for candidate in candidates if _task_action_identity(candidate.text) == target]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, exact
    scored = []
    for candidate in candidates:
        action = _task_action_identity(candidate.text)
        left, right = set(target.split()), set(action.split())
        overlap = len(left & right) / len(left | right) if left and right else 0
        similarity = SequenceMatcher(None, target, action).ratio()
        score = max(overlap, similarity if similarity >= 0.85 else 0)
        if score >= 0.6:
            scored.append((score, candidate))
    scored.sort(key=lambda row: row[0], reverse=True)
    if not scored:
        return None, []
    plausible = [candidate for score, candidate in scored if score >= scored[0][0] - 0.1]
    return (plausible[0], []) if len(plausible) == 1 else (None, plausible)


def complete_analysis_run(run_id: int, result: dict, source_versions: Optional[dict[int, str]] = None) -> dict:
    completed_at = datetime.now(timezone.utc).isoformat()
    owner_id = require_owner_id()
    with SessionLocal() as session:
        run = session.get(AnalysisRunRecord, run_id)
        if (
            run is None
            or run.status != ANALYSIS_RUNNING
            or run.owner_user_id != owner_id
        ):
            raise ValueError("Analysis run is not available for completion")

        ensure_default_topics(session, owner_user_id=owner_id)
        if source_versions is not None:
            for item_id, fingerprint in source_versions.items():
                current = _deadline_sources(session, [item_id], lock=True)
                if not current or _deadline_fingerprint(current) != fingerprint:
                    raise SourceChangedDuringAnalysis("Source content changed during analysis. Analyze again to use the updated capture.")

        for bucket in result["buckets"]:
            topic_id = _topic_id_by_name(session, bucket["name"])
            for analyzed_item in bucket["notes"]:
                item_id = analyzed_item["id"]
                if _owned_item(session, item_id, owner_id) is None:
                    raise ValueError(
                        f"Analysis references missing item {item_id}"
                    )
                insight = session.get(ItemInsightRecord, item_id)
                if insight is None:
                    insight = ItemInsightRecord(
                        item_id=item_id,
                        needs_review=0,
                    )
                    session.add(insight)
                insight.analysis_run_id = run_id
                # AI fields always refresh; user overrides are preserved.
                insight.category = bucket["name"]
                insight.concise_title = analyzed_item["title"]
                insight.priority = analyzed_item["priority"]
                insight.topic_id = topic_id
                tags = analyzed_item.get("tags") or []
                insight.tags_json = json.dumps(tags, ensure_ascii=False)
                insight.rationale = analyzed_item.get("rationale") or (
                    f"Grouped under “{bucket['name']}” based on the note content."
                )
                insight.needs_review = 0
                insight.updated_at = completed_at
                item = _owned_item(session, item_id, owner_id)
                if item is not None and insight.user_title is None:
                    item.title = analyzed_item["title"]

        processed_sources = set()
        reused_source_groups = set()
        matched_tasks = set()
        for todo in result["todos"]:
            source_item_ids = sorted(set(todo["source_note_ids"]))
            if not source_item_ids:
                continue
            for item_id in source_item_ids:
                if _owned_item(session, item_id, owner_id) is None:
                    raise ValueError(
                        f"Task references missing item {item_id}"
                    )

            sources = _deadline_sources(session, source_item_ids)
            source_fingerprint = _deadline_fingerprint(sources)
            source_group = tuple(source_item_ids)
            candidates = session.scalars(
                select(TaskRecord).join(TaskSourceItemRecord).where(
                    TaskRecord.owner_user_id == owner_id,
                    TaskSourceItemRecord.item_id.in_(source_item_ids),
                ).distinct().order_by(TaskRecord.id.asc())
            ).all()
            candidates = [candidate for candidate in candidates if set(session.scalars(
                select(TaskSourceItemRecord.item_id).where(TaskSourceItemRecord.task_id == candidate.id)
            ).all()) == set(source_item_ids)]
            # Repeated processing of the same source version reuses durable
            # tasks, even when a model proposes different wording or timing.
            if source_group not in processed_sources:
                processed_sources.add(source_group)
                if all(item.task_analysis_fingerprint == _deadline_fingerprint([item]) for item in sources) or (
                    candidates and all(candidate.deadline_source_fingerprint is None for candidate in candidates)
                ):
                    for candidate in candidates:
                        if candidate.deadline_source_fingerprint is None:
                            candidate.deadline_source_fingerprint = source_fingerprint
                    matched_tasks.update(candidate.id for candidate in candidates)
                    reused_source_groups.add(source_group)
            if source_group in reused_source_groups:
                if len(candidates) == 1 and sum(
                    set(row["source_note_ids"]) == set(source_item_ids)
                    for row in result["todos"]
                ) == 1:
                    # Presentation may refresh; the saved timing and identity
                    # remain tied to this unchanged source version.
                    candidates[0].text = todo["task"]
                    candidates[0].priority = todo["priority"]
                    candidates[0].updated_at = completed_at
                continue

            dedup_key = _task_dedup_key(source_item_ids, todo["task"])
            task = session.scalars(
                select(TaskRecord).where(
                    TaskRecord.dedup_key == dedup_key,
                    TaskRecord.owner_user_id == owner_id,
                )
            ).first()
            if task is not None and task.id in matched_tasks:
                continue

            if task is None:
                # Durable match: same sources + similar normalized wording.
                available = [candidate for candidate in candidates if candidate.id not in matched_tasks]
                task, ambiguous = _match_edited_task(available, todo["task"])
                if ambiguous:
                    for candidate in ambiguous:
                        if candidate.deadline_status == "inferred" and not candidate.timing_confirmed:
                            candidate.deadline_needs_review = 1
                            candidate.deadline_note = "Source edit could match multiple obligations. Previous deadline and reference retained; review the source."
                    continue

            if task is None:
                inferred = _source_deadline(todo, sources)
                if any(item.task_analysis_fingerprint and item.updated_at and item.updated_at != item.time_reference_at for item in sources):
                    inferred.update(
                        due_on=None, due_confidence="low", deadline_needs_review=1,
                        deadline_note="This new obligation came from an edited source with preserved older timing. Choose its deadline; no reference was guessed.",
                    )
                task = TaskRecord(
                    text=todo["task"],
                    priority=todo["priority"],
                    timing_confirmed=0,
                    status=TASK_OPEN,
                    dedup_key=dedup_key,
                    owner_user_id=owner_id,
                    created_at=completed_at,
                    updated_at=completed_at,
                    **inferred,
                )
                session.add(task)
                session.flush()
                for item_id in source_item_ids:
                    session.add(
                        TaskSourceItemRecord(
                            task_id=task.id,
                            item_id=item_id,
                        )
                    )
            else:
                task.text = todo["task"]
                task.priority = todo["priority"]
                if task.deadline_status == "inferred" and not task.timing_confirmed:
                    _reconcile_inferred_deadline(task, _source_deadline(todo, sources))
                task.dedup_key = dedup_key
                task.updated_at = completed_at
                linked = set(
                    session.scalars(
                        select(TaskSourceItemRecord.item_id).where(
                            TaskSourceItemRecord.task_id == task.id
                        )
                    ).all()
                )
                for item_id in source_item_ids:
                    if item_id not in linked:
                        session.add(
                            TaskSourceItemRecord(
                                task_id=task.id,
                                item_id=item_id,
                            )
                        )
            matched_tasks.add(task.id)

        _refresh_owner_relations(session, owner_id, completed_at)

        for item in session.scalars(select(InboxItemRecord).where(
            InboxItemRecord.owner_user_id == owner_id,
            InboxItemRecord.processing_status.in_(ANALYZABLE_STATUSES),
        )).all():
            if source_versions is None or item.id in source_versions:
                item.task_analysis_fingerprint = _deadline_fingerprint([item])

        fingerprint = compute_inbox_fingerprint(session)
        persisted = {
            **result,
            "inbox_fingerprint": fingerprint,
        }
        run.completed_at = completed_at
        run.status = ANALYSIS_SUCCEEDED
        run.mode = result["mode"]
        run.result_json = json.dumps(persisted, ensure_ascii=False)
        session.commit()
        return {
            **result,
            "analysis_run_id": run_id,
            "generated_at": completed_at,
            "is_stale": False,
            "inbox_fingerprint": fingerprint,
        }


def _refresh_owner_relations(session, owner_id: int, now: str) -> None:
    """Recompute deterministic relations among one owner's items only.

    A relation row must never connect items of different owners. Dismissed
    rows are kept only when both endpoints belong to this owner; any
    cross-owner row touching this owner's items is removed, dismissed or not.
    """
    owned_items = session.scalars(
        select(InboxItemRecord).where(InboxItemRecord.owner_user_id == owner_id)
    ).all()
    owned_ids = {item.id for item in owned_items}
    touching = session.scalars(
        select(ItemRelationRecord).where(
            or_(
                ItemRelationRecord.item_id_a.in_(_owned_item_ids(owner_id)),
                ItemRelationRecord.item_id_b.in_(_owned_item_ids(owner_id)),
            )
        )
    ).all()
    for relation in touching:
        same_owner = (
            relation.item_id_a in owned_ids and relation.item_id_b in owned_ids
        )
        if not same_owner or not relation.dismissed:
            session.delete(relation)
    session.flush()

    items = [_item_to_dict(item) for item in owned_items]
    for relation in find_item_relations(items):
        if (
            relation["item_id_a"] not in owned_ids
            or relation["item_id_b"] not in owned_ids
        ):
            continue
        existing = session.scalars(
            select(ItemRelationRecord).where(
                ItemRelationRecord.item_id_a == relation["item_id_a"],
                ItemRelationRecord.item_id_b == relation["item_id_b"],
                ItemRelationRecord.relation_type == relation["relation_type"],
            )
        ).first()
        if existing is not None:
            if existing.dismissed:
                continue
            existing.score = relation["score"]
            continue
        session.add(
            ItemRelationRecord(
                item_id_a=relation["item_id_a"],
                item_id_b=relation["item_id_b"],
                relation_type=relation["relation_type"],
                score=relation["score"],
                created_at=now,
                dismissed=0,
            )
        )
    session.flush()


def fail_analysis_run(run_id: int) -> None:
    completed_at = datetime.now(timezone.utc).isoformat()
    owner_id = require_owner_id()
    with SessionLocal() as session:
        run = session.get(AnalysisRunRecord, run_id)
        if (
            run is None
            or run.status != ANALYSIS_RUNNING
            or run.owner_user_id != owner_id
        ):
            return
        run.completed_at = completed_at
        run.status = ANALYSIS_FAILED
        run.mode = "error"
        run.result_json = None
        session.commit()


def get_latest_successful_analysis() -> Optional[dict]:
    owner_id = require_owner_id()
    with SessionLocal() as session:
        run = session.scalars(
            select(AnalysisRunRecord)
            .where(
                AnalysisRunRecord.status == ANALYSIS_SUCCEEDED,
                AnalysisRunRecord.owner_user_id == owner_id,
            )
            .order_by(
                AnalysisRunRecord.completed_at.desc(),
                AnalysisRunRecord.id.desc(),
            )
            .limit(1)
        ).first()
        if run is None:
            return None
        stored = json.loads(run.result_json)
        if stored.get("contract_version") == "delta-v1":
            return _incremental_view(session, run, stored)
        fingerprint = stored.pop("inbox_fingerprint", None)
        current = compute_inbox_fingerprint(session)
        return {
            **stored,
            "analysis_run_id": run.id,
            "generated_at": run.completed_at,
            "is_stale": fingerprint != current,
            "inbox_fingerprint": fingerprint,
        }


def _task_dedup_key(source_item_ids: list[int], task_text: str = "") -> str:
    fingerprint = json.dumps(
        {
            "sources": sorted(set(source_item_ids)),
            "task": _normalize_task_text(task_text),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def _task_to_dict(session, task: TaskRecord) -> dict:
    source_item_ids = session.scalars(
        select(TaskSourceItemRecord.item_id)
        .where(TaskSourceItemRecord.task_id == task.id,
               TaskSourceItemRecord.item_id.in_(_owned_item_ids(task.owner_user_id)))
        .order_by(TaskSourceItemRecord.item_id.asc())
    ).all()
    from datetime import date

    due_on = task.due_on
    needs_review = bool(task.deadline_needs_review)
    note = task.deadline_note
    if due_on:
        try:
            if date.fromisoformat(due_on).isoformat() != due_on:
                raise ValueError("Non-canonical date")
        except (TypeError, ValueError):
            due_on = None
            needs_review = True
            note = "The stored legacy deadline is invalid. Choose a valid calendar date."
    current_fingerprint = _deadline_fingerprint(_deadline_sources(session, list(source_item_ids)))
    return {
        "id": task.id,
        "text": task.user_text or task.text,
        "priority": task.user_priority or task.priority,
        "ai_text": task.text,
        "ai_priority": task.priority,
        "user_text": task.user_text,
        "user_priority": task.user_priority,
        "has_user_overrides": task.user_text is not None or task.user_priority is not None,
        "timing_text": task.timing_text,
        "ai_timing_text": task.ai_timing_text,
        "timing_confirmed": bool(task.timing_confirmed),
        "due_on": due_on,
        "due_confidence": task.due_confidence,
        "deadline_status": task.deadline_status,
        "deadline_needs_review": needs_review,
        "deadline_note": note,
        "deadline_reference_at": task.deadline_reference_at,
        "deadline_timezone": task.deadline_timezone,
        "source_changed": bool(task.deadline_source_fingerprint and task.deadline_source_fingerprint != current_fingerprint),
        "details": [
            {"source_item_id": row.source_item_id, "text": row.text}
            for row in session.scalars(select(TaskDetailRecord).where(
                TaskDetailRecord.task_id == task.id,
                TaskDetailRecord.source_item_id.in_(_owned_item_ids(require_owner_id())),
            ).order_by(TaskDetailRecord.id.asc())).all()
        ],
        "snoozed_until": task.snoozed_until,
        "reminder_at": task.reminder_at,
        "status": task.status,
        "source_item_ids": list(source_item_ids),
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def list_tasks_from_db(status_filter: str = TASK_OPEN) -> list[dict]:
    if status_filter not in TASK_STATUSES | {"all"}:
        raise ValueError("Unsupported task status")

    owner_id = require_owner_id()
    with SessionLocal() as session:
        statement = (
            select(TaskRecord)
            .where(TaskRecord.owner_user_id == owner_id)
            .order_by(TaskRecord.id.asc())
        )
        if status_filter != "all":
            statement = statement.where(TaskRecord.status == status_filter)
        tasks = session.scalars(statement).all()
        return [_task_to_dict(session, task) for task in tasks]


def update_task_overrides(task_id: int, *, text: Optional[str] = None,
                          priority: Optional[str] = None, clear_overrides: bool = False) -> Optional[dict]:
    """A task correction changes presentation, never inferred identity or dates."""
    owner_id = require_owner_id()
    with SessionLocal() as session:
        task = _owned_task(session, task_id, owner_id, for_update=True)
        if task is None:
            return None
        if clear_overrides:
            task.user_text, task.user_priority = None, None
        else:
            if text is not None:
                if not text.strip():
                    raise ValueError("Task title cannot be blank.")
                task.user_text = text.strip()
            if priority is not None:
                if priority not in {"High", "Medium", "Low"}:
                    raise ValueError("Unsupported task priority.")
                task.user_priority = priority
        task.updated_at = datetime.now(timezone.utc).isoformat()
        session.commit()
        return _task_to_dict(session, task)


def set_task_status(task_id: int, task_status: str) -> Optional[dict]:
    if task_status not in TASK_STATUSES:
        raise ValueError("Unsupported task status")

    owner_id = require_owner_id()
    with SessionLocal() as session:
        task = _owned_task(session, task_id, owner_id)
        if task is None:
            return None
        if task.status != task_status:
            task.status = task_status
            task.updated_at = datetime.now(timezone.utc).isoformat()
            session.commit()
        return _task_to_dict(session, task)


def add_pdf_item_to_db(
    original_filename: str,
    storage_path: str,
    mime_type: str,
) -> dict:
    """Persist uploaded PDF metadata before text extraction."""
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        item = InboxItemRecord(
            item_type=PDF_ITEM_TYPE,
            original_filename=original_filename,
            storage_path=storage_path,
            mime_type=mime_type,
            processing_status=STATUS_CAPTURED,
            owner_user_id=require_owner_id(),
            created_at=now,
            time_reference_at=now,
            time_reference_timezone=str(configured_timezone()),
        )
        session.add(item)
        session.commit()
        return _item_to_dict(item)


def complete_pdf_item_processing(
    item_id: int,
    *,
    title: str,
    raw_text: str,
    extracted_text: str,
    mime_type: Optional[str],
) -> Optional[dict]:
    owner_id = require_owner_id()
    with SessionLocal() as session:
        item = _owned_item(session, item_id, owner_id)
        if item is None:
            return None
        item.title = title
        item.raw_text = raw_text
        item.extracted_text = extracted_text
        item.mime_type = mime_type
        item.processing_status = STATUS_READY
        item.error_message = None
        item.updated_at = datetime.now(timezone.utc).isoformat()
        session.commit()
        return _item_to_dict(item)


def add_image_item_to_db(
    original_filename: str,
    storage_path: str,
    mime_type: str,
) -> dict:
    """Persist uploaded image metadata before OCR/description."""
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        item = InboxItemRecord(
            item_type=IMAGE_ITEM_TYPE,
            original_filename=original_filename,
            storage_path=storage_path,
            mime_type=mime_type,
            processing_status=STATUS_CAPTURED,
            owner_user_id=require_owner_id(),
            created_at=now,
            time_reference_at=now,
            time_reference_timezone=str(configured_timezone()),
        )
        session.add(item)
        session.commit()
        return _item_to_dict(item)


def complete_image_item_processing(
    item_id: int,
    *,
    title: str,
    raw_text: str,
    extracted_text: str,
    ai_description: str,
    mime_type: Optional[str],
) -> Optional[dict]:
    owner_id = require_owner_id()
    with SessionLocal() as session:
        item = _owned_item(session, item_id, owner_id)
        if item is None:
            return None
        item.title = title
        item.raw_text = raw_text
        item.extracted_text = extracted_text
        item.ai_description = ai_description
        item.mime_type = mime_type
        item.processing_status = STATUS_READY
        item.error_message = None
        item.updated_at = datetime.now(timezone.utc).isoformat()
        session.commit()
        return _item_to_dict(item)


def add_voice_item_to_db(
    original_filename: str,
    storage_path: str,
    mime_type: str,
) -> dict:
    """Persist uploaded voice note metadata before transcription."""
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        item = InboxItemRecord(
            item_type=VOICE_ITEM_TYPE,
            original_filename=original_filename,
            storage_path=storage_path,
            mime_type=mime_type,
            processing_status=STATUS_CAPTURED,
            owner_user_id=require_owner_id(),
            created_at=now,
            time_reference_at=now,
            time_reference_timezone=str(configured_timezone()),
        )
        session.add(item)
        session.commit()
        return _item_to_dict(item)


def complete_voice_item_processing(
    item_id: int,
    *,
    title: str,
    raw_text: str,
    extracted_text: str,
    mime_type: Optional[str],
) -> Optional[dict]:
    owner_id = require_owner_id()
    with SessionLocal() as session:
        item = _owned_item(session, item_id, owner_id)
        if item is None:
            return None
        item.title = title
        item.raw_text = raw_text
        item.extracted_text = extracted_text
        item.mime_type = mime_type
        item.processing_status = STATUS_READY
        item.error_message = None
        item.updated_at = datetime.now(timezone.utc).isoformat()
        session.commit()
        return _item_to_dict(item)


def update_item_extracted_text(
    item_id: int,
    extracted_text: str,
) -> Optional[dict]:
    """User edit of extracted/OCR/transcript text (facts), preserving AI description."""
    cleaned = extracted_text.strip()
    owner_id = require_owner_id()
    with SessionLocal() as session:
        item = _owned_item(session, item_id, owner_id)
        if item is None:
            return None
        if item.item_type not in {IMAGE_ITEM_TYPE, VOICE_ITEM_TYPE, PDF_ITEM_TYPE}:
            return None
        if item.extracted_text != cleaned:
            _update_source_time_reference(item, item.extracted_text or "", cleaned, datetime.now(timezone.utc).isoformat())
        item.extracted_text = cleaned
        if item.item_type == IMAGE_ITEM_TYPE and item.ai_description:
            item.raw_text = (
                f"{cleaned}\n\n[AI description — inferred]\n{item.ai_description}"
                if cleaned
                else f"[AI description — inferred]\n{item.ai_description}"
            )
        else:
            item.raw_text = cleaned
        item.updated_at = datetime.now(timezone.utc).isoformat()
        session.commit()
        return _item_to_dict(item)


def add_url_item_to_db(url: str) -> dict:
    """Persist URL first (captured), then return the row for processing."""
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        item = InboxItemRecord(
            item_type=URL_ITEM_TYPE,
            source_url=url,
            processing_status=STATUS_CAPTURED,
            owner_user_id=require_owner_id(),
            created_at=now,
            time_reference_at=now,
            time_reference_timezone=str(configured_timezone()),
        )
        session.add(item)
        session.commit()
        return _item_to_dict(item)


def mark_item_processing(item_id: int) -> Optional[dict]:
    owner_id = require_owner_id()
    with SessionLocal() as session:
        item = _owned_item(session, item_id, owner_id)
        if item is None:
            return None
        item.processing_status = STATUS_PROCESSING
        item.error_message = None
        item.updated_at = datetime.now(timezone.utc).isoformat()
        session.commit()
        return _item_to_dict(item)


def complete_url_item_processing(
    item_id: int,
    *,
    title: str,
    raw_text: str,
    mime_type: Optional[str],
    final_url: Optional[str] = None,
) -> Optional[dict]:
    owner_id = require_owner_id()
    with SessionLocal() as session:
        item = _owned_item(session, item_id, owner_id)
        if item is None:
            return None
        item.title = title
        item.raw_text = raw_text
        item.extracted_text = raw_text
        item.mime_type = mime_type
        if final_url:
            item.source_url = final_url
        item.processing_status = STATUS_READY
        item.error_message = None
        item.updated_at = datetime.now(timezone.utc).isoformat()
        session.commit()
        return _item_to_dict(item)


def fail_item_processing(item_id: int, error_message: str) -> Optional[dict]:
    owner_id = require_owner_id()
    with SessionLocal() as session:
        item = _owned_item(session, item_id, owner_id)
        if item is None:
            return None
        item.processing_status = STATUS_FAILED
        item.error_message = error_message[:1000]
        item.updated_at = datetime.now(timezone.utc).isoformat()
        session.commit()
        return _item_to_dict(item)


def get_item_from_db(item_id: int) -> Optional[dict]:
    owner_id = require_owner_id()
    with SessionLocal() as session:
        item = _owned_item(session, item_id, owner_id)
        if item is None:
            return None
        return _item_to_dict(item)


def confirm_task_timing(
    task_id: int,
    timing_text: Optional[str] = None,
) -> Optional[dict]:
    owner_id = require_owner_id()
    with SessionLocal() as session:
        task = _owned_task(session, task_id, owner_id, for_update=True)
        if task is None:
            return None
        if timing_text is not None and timing_text != task.timing_text:
            # Compatibility for callers explicitly entering NEW wording. The UI
            # uses the date-only PATCH instead. Unsupported times stay unresolved.
            task.timing_text = timing_text
            now = datetime.now(timezone.utc).isoformat()
            zone = str(configured_timezone())
            parsed = parse_timing_phrase(timing_text, reference=reference_date(now, zone))
            task.due_on = parsed.normalized_iso
            task.due_confidence = parsed.confidence
            task.deadline_reference_at = now
            task.deadline_timezone = zone
            task.deadline_status = "edited"
            task.deadline_needs_review = int(parsed.needs_confirmation)
            task.deadline_note = parsed.note
        else:
            current = _task_to_dict(session, task)
            if not current["due_on"]:
                raise ValueError("There is no saved date to confirm. Choose a date-only deadline.")
            task.deadline_status = "confirmed"
            task.deadline_needs_review = 0
            task.deadline_note = "You confirmed the saved date. Original source wording is retained."
        task.timing_confirmed = 1
        sources = _deadline_sources(session, _task_to_dict(session, task)["source_item_ids"])
        task.deadline_source_fingerprint = _deadline_fingerprint(sources)
        task.updated_at = datetime.now(timezone.utc).isoformat()
        session.commit()
        return _task_to_dict(session, task)


def update_task_deadline(task_id: int, due_on: Optional[str]) -> Optional[dict]:
    """Explicit date-only correction or durable clearing; inference cannot win."""
    owner_id = require_owner_id()
    with SessionLocal() as session:
        task = _owned_task(session, task_id, owner_id)
        if task is None:
            return None
        task.due_on = due_on
        task.timing_text = due_on
        task.timing_confirmed = 1
        task.due_confidence = "high" if due_on else "none"
        task.deadline_status = "edited" if due_on else "cleared"
        task.deadline_needs_review = 0
        task.deadline_note = "Date-only deadline chosen by you." if due_on else "Deadline cleared by you. Re-analysis will keep it cleared."
        task.deadline_reference_at = datetime.now(timezone.utc).isoformat()
        task.deadline_timezone = str(configured_timezone())
        sources = _deadline_sources(session, _task_to_dict(session, task)["source_item_ids"])
        task.deadline_source_fingerprint = _deadline_fingerprint(sources)
        task.updated_at = task.deadline_reference_at
        session.commit()
        return _task_to_dict(session, task)


def snooze_task_in_db(task_id: int, until_iso_date: str) -> Optional[dict]:
    owner_id = require_owner_id()
    with SessionLocal() as session:
        task = _owned_task(session, task_id, owner_id)
        if task is None:
            return None
        task.snoozed_until = until_iso_date
        task.reminder_at = until_iso_date
        task.updated_at = datetime.now(timezone.utc).isoformat()
        session.commit()
        return _task_to_dict(session, task)


def set_task_reminder_in_db(task_id: int, reminder_iso_date: str) -> Optional[dict]:
    owner_id = require_owner_id()
    with SessionLocal() as session:
        task = _owned_task(session, task_id, owner_id)
        if task is None:
            return None
        task.reminder_at = reminder_iso_date
        task.updated_at = datetime.now(timezone.utc).isoformat()
        session.commit()
        return _task_to_dict(session, task)


def get_attention_board() -> dict:
    from app.attention.dates import (
        classify_attention_bucket,
        today_in_configured_tz,
    )
    from datetime import date as date_cls

    today = today_in_configured_tz()
    board = {
        "today": [],
        "upcoming": [],
        "overdue": [],
        "unscheduled": [],
        "snoozed": [],
        "needs_confirmation": [],
        "reference_date": today.isoformat(),
        "timezone": str(configured_timezone()),
    }
    for task in list_tasks_from_db(status_filter=TASK_OPEN):
        due = None
        if task.get("due_on"):
            try:
                due = date_cls.fromisoformat(task["due_on"])
            except ValueError:
                pass
        snoozed = None
        if task.get("snoozed_until"):
            try:
                snoozed = date_cls.fromisoformat(task["snoozed_until"])
            except ValueError:
                snoozed = None
        bucket = classify_attention_bucket(due, reference=today, snoozed_until=snoozed)
        entry = {
            **task,
            "original_timing_phrase": task.get("ai_timing_text") or task.get("timing_text"),
            "normalized_due_on": due.isoformat() if due else None,
            "due_confidence": task.get("due_confidence"),
            "needs_confirmation": task["deadline_needs_review"],
            "attention_bucket": bucket,
            "parse_note": task["deadline_note"],
        }
        board[bucket].append(entry)
        if entry["needs_confirmation"]:
            board["needs_confirmation"].append(entry)
    return board


def get_daily_briefing() -> dict:
    board = get_attention_board()
    priority_rank = {"High": 0, "Medium": 1, "Low": 2}
    focus = sorted(
        board["overdue"] + board["today"],
        key=lambda item: (
            priority_rank.get(item.get("priority"), 9),
            item.get("normalized_due_on") or "9999",
        ),
    )
    suggestions = []
    for item in focus[:5]:
        reason = []
        if item["attention_bucket"] == "overdue":
            reason.append("overdue")
        if item["attention_bucket"] == "today":
            reason.append("due today")
        if item.get("priority") == "High":
            reason.append("high priority")
        suggestions.append(
            {
                "task_id": item["id"],
                "text": item["text"],
                "priority": item["priority"],
                "reason": ", ".join(reason) or "on your attention list",
            }
        )
    return {
        "reference_date": board["reference_date"],
        "counts": {
            "overdue": len(board["overdue"]),
            "today": len(board["today"]),
            "upcoming": len(board["upcoming"]),
            "unscheduled": len(board["unscheduled"]),
            "snoozed": len(board["snoozed"]),
            "needs_confirmation": len(board["needs_confirmation"]),
        },
        "focus": focus[:10],
        "priority_suggestions": suggestions,
        "summary": (
            f"{len(board['overdue'])} overdue, {len(board['today'])} due today, "
            f"{len(board['upcoming'])} upcoming."
        ),
    }


def save_calendar_receipt(
    *,
    draft_id: str,
    task_id: int,
    provider: str,
    status: str,
    payload: dict,
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    owner_id = require_owner_id()
    with SessionLocal() as session:
        if _owned_task(session, task_id, owner_id) is None:
            raise ValueError("Task not found")
        receipt = CalendarActionReceiptRecord(
            draft_id=draft_id,
            task_id=task_id,
            provider=provider,
            status=status,
            payload_json=json.dumps(payload),
            created_at=now,
        )
        session.add(receipt)
        session.commit()
        return {
            "id": receipt.id,
            "draft_id": receipt.draft_id,
            "task_id": receipt.task_id,
            "provider": receipt.provider,
            "status": receipt.status,
            "payload": payload,
            "created_at": receipt.created_at,
        }


def _topic_to_dict(topic: TopicRecord, item_count: int = 0) -> dict:
    return {
        "id": topic.id,
        "name": topic.name,
        "description": topic.description,
        "archived": bool(topic.archived),
        "item_count": item_count,
        "created_at": topic.created_at,
        "updated_at": topic.updated_at,
    }


def list_topics_from_db(include_archived: bool = False) -> list[dict]:
    owner_id = require_owner_id()
    with SessionLocal() as session:
        ensure_default_topics(session, owner_user_id=owner_id)
        session.commit()
        statement = (
            select(TopicRecord)
            .where(TopicRecord.owner_user_id == owner_id)
            .order_by(TopicRecord.name.asc())
        )
        if not include_archived:
            statement = statement.where(TopicRecord.archived == 0)
        topics = session.scalars(statement).all()
        owned_topic_ids = _owned_topic_ids(session, owner_id)
        counts = {}
        for insight in session.scalars(_owned_insights(owner_id)).all():
            effective_topic = _insight_to_dict(insight, owned_topic_ids)[
                "effective_topic_id"
            ]
            if effective_topic is None:
                continue
            counts[effective_topic] = counts.get(effective_topic, 0) + 1
        return [
            _topic_to_dict(topic, counts.get(topic.id, 0)) for topic in topics
        ]


def create_topic_in_db(name: str, description: Optional[str] = None) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    owner_id = require_owner_id()
    with SessionLocal() as session:
        ensure_default_topics(session, owner_user_id=owner_id)
        existing = session.scalars(
            select(TopicRecord).where(
                TopicRecord.name == name,
                TopicRecord.owner_user_id == owner_id,
            )
        ).first()
        if existing is not None:
            if existing.archived:
                existing.archived = 0
                existing.description = description
                existing.updated_at = now
                session.commit()
                return _topic_to_dict(existing)
            raise ValueError("Topic already exists")
        topic = TopicRecord(
            name=name,
            description=description,
            owner_user_id=owner_id,
            created_at=now,
            updated_at=now,
            archived=0,
        )
        session.add(topic)
        session.commit()
        return _topic_to_dict(topic)


def update_topic_in_db(
    topic_id: int,
    name: Optional[str] = None,
    description: Optional[str] = None,
    archived: Optional[bool] = None,
) -> Optional[dict]:
    owner_id = require_owner_id()
    with SessionLocal() as session:
        topic = session.get(TopicRecord, topic_id)
        if topic is not None and topic.owner_user_id != owner_id:
            return None
        if topic is None:
            return None
        if name is not None:
            topic.name = name
        if description is not None:
            topic.description = description
        if archived is not None:
            topic.archived = 1 if archived else 0
        topic.updated_at = datetime.now(timezone.utc).isoformat()
        session.commit()
        return _topic_to_dict(topic)


def _parse_tags(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [str(tag) for tag in value]


def _owned_insights(owner_id: int):
    return select(ItemInsightRecord).where(
        ItemInsightRecord.item_id.in_(_owned_item_ids(owner_id))
    )


def _insight_to_dict(
    insight: ItemInsightRecord,
    owned_topic_ids: Optional[set[int]] = None,
) -> dict:
    """Serialize an insight; topic references outside the owner are dropped."""

    def _own_topic(topic_id: Optional[int]) -> Optional[int]:
        if topic_id is None or owned_topic_ids is None:
            return topic_id
        return topic_id if topic_id in owned_topic_ids else None

    topic_id = _own_topic(insight.topic_id)
    user_topic_id = _own_topic(insight.user_topic_id)
    effective_title = insight.user_title or insight.concise_title
    effective_priority = insight.user_priority or insight.priority
    effective_topic_id = user_topic_id if user_topic_id is not None else topic_id
    effective_tags = (
        _parse_tags(insight.user_tags_json)
        if insight.user_tags_json is not None
        else _parse_tags(insight.tags_json)
    )
    return {
        "item_id": insight.item_id,
        "analysis_run_id": insight.analysis_run_id,
        "analysis_pending": insight.analysis_run_id is None,
        "category": insight.category,
        "concise_title": insight.concise_title,
        "priority": insight.priority,
        "topic_id": topic_id,
        "tags": _parse_tags(insight.tags_json),
        "rationale": insight.rationale,
        "user_title": insight.user_title,
        "user_topic_id": user_topic_id,
        "user_priority": insight.user_priority,
        "user_tags": (
            _parse_tags(insight.user_tags_json)
            if insight.user_tags_json is not None
            else None
        ),
        "effective_title": effective_title,
        "effective_priority": effective_priority,
        "effective_topic_id": effective_topic_id,
        "effective_tags": effective_tags,
        "needs_review": bool(insight.needs_review),
        "updated_at": insight.updated_at,
        "has_user_overrides": any(
            [
                insight.user_title is not None,
                user_topic_id is not None,
                insight.user_priority is not None,
                insight.user_tags_json is not None,
            ]
        ),
    }


def get_item_insight(item_id: int) -> Optional[dict]:
    owner_id = require_owner_id()
    with SessionLocal() as session:
        item = _owned_item(session, item_id, owner_id)
        if item is None:
            return None
        insight = session.get(ItemInsightRecord, item_id) or _pending_item_insight(item)
        return _insight_to_dict(insight, _owned_topic_ids(session, owner_id))


def get_item_detail(item_id: int) -> Optional[dict]:
    """Read one owned source and its owned derived records, without analysis."""
    owner_id = require_owner_id()
    with SessionLocal() as session:
        item = _owned_item(session, item_id, owner_id)
        if item is None:
            return None
        topics = {topic.id: topic for topic in session.scalars(select(TopicRecord).where(
            TopicRecord.owner_user_id == owner_id)).all()}
        insight = session.get(ItemInsightRecord, item_id)
        tasks = session.scalars(select(TaskRecord).join(TaskSourceItemRecord).where(
            TaskRecord.owner_user_id == owner_id,
            TaskSourceItemRecord.item_id == item_id).order_by(TaskRecord.id)).all()
        relations = session.scalars(_owned_relations(owner_id).where(
            ItemRelationRecord.dismissed == 0,
            or_(ItemRelationRecord.item_id_a == item_id, ItemRelationRecord.item_id_b == item_id)
        ).order_by(ItemRelationRecord.id)).all()
        related_ids = {relation.item_id_b if relation.item_id_a == item_id else relation.item_id_a
                       for relation in relations}
        related = {row.id: row for row in session.scalars(select(InboxItemRecord).where(
            InboxItemRecord.owner_user_id == owner_id, InboxItemRecord.id.in_(related_ids))).all()}
        related_insights = {row.item_id: row for row in session.scalars(select(ItemInsightRecord).where(
            ItemInsightRecord.item_id.in_(related))).all()}
        return {
            "item": _item_to_dict(item, insight=insight, topics=topics, prefetched=True),
            "insight": _insight_to_dict(insight or _pending_item_insight(item), set(topics)),
            "tasks": [_task_to_dict(session, task) for task in tasks],
            "related_items": [{"relation_id": relation.id, "relation_type": relation.relation_type,
                "item": _item_to_dict(related[other_id], insight=related_insights.get(other_id), topics=topics, prefetched=True)}
                for relation in relations
                for other_id in [relation.item_id_b if relation.item_id_a == item_id else relation.item_id_a]
                if other_id in related],
        }


def update_item_insight_overrides(
    item_id: int,
    user_title: Optional[str] = None,
    user_topic_id: Optional[int] = None,
    user_priority: Optional[str] = None,
    user_tags: Optional[list[str]] = None,
    clear_overrides: bool = False,
    needs_review: Optional[bool] = None,
) -> Optional[dict]:
    owner_id = require_owner_id()
    with SessionLocal() as session:
        item = _owned_item(session, item_id, owner_id)
        if item is None:
            return None
        insight = session.get(ItemInsightRecord, item_id)
        if insight is None:
            insight = _pending_item_insight(item)
            session.add(insight)
        if clear_overrides:
            # Repair old versions which mirrored a user title into generated title.
            if insight.user_title and item.title == insight.user_title:
                item.title = insight.concise_title
            insight.user_title = None
            insight.user_topic_id = None
            insight.user_priority = None
            insight.user_tags_json = None
        else:
            if user_title is not None:
                insight.user_title = user_title
            if user_topic_id is not None:
                if _owned_topic(session, user_topic_id, owner_id) is None:
                    raise ValueError("Topic not found")
                insight.user_topic_id = user_topic_id
            if user_priority is not None:
                insight.user_priority = user_priority
            if user_tags is not None:
                insight.user_tags_json = json.dumps(
                    user_tags, ensure_ascii=False
                )
        if needs_review is not None:
            insight.needs_review = 1 if needs_review else 0
        insight.updated_at = datetime.now(timezone.utc).isoformat()
        session.commit()
        return _insight_to_dict(insight, _owned_topic_ids(session, owner_id))


def _relation_to_dict(row: ItemRelationRecord) -> dict:
    return {
        "id": row.id,
        "item_id_a": row.item_id_a,
        "item_id_b": row.item_id_b,
        "relation_type": row.relation_type,
        "score": float(row.score),
        "dismissed": bool(row.dismissed),
        "created_at": row.created_at,
    }


def _owned_relations(owner_id: int):
    """Relations whose two endpoints both belong to this owner."""
    owned_ids = _owned_item_ids(owner_id)
    return select(ItemRelationRecord).where(
        ItemRelationRecord.item_id_a.in_(owned_ids),
        ItemRelationRecord.item_id_b.in_(owned_ids),
    )


def list_relations_from_db(
    include_dismissed: bool = False,
) -> list[dict]:
    owner_id = require_owner_id()
    with SessionLocal() as session:
        statement = _owned_relations(owner_id).order_by(
            ItemRelationRecord.id.asc()
        )
        if not include_dismissed:
            statement = statement.where(ItemRelationRecord.dismissed == 0)
        rows = session.scalars(statement).all()
        return [_relation_to_dict(row) for row in rows]


def dismiss_relation_in_db(relation_id: int) -> Optional[dict]:
    owner_id = require_owner_id()
    with SessionLocal() as session:
        row = session.scalars(
            _owned_relations(owner_id).where(
                ItemRelationRecord.id == relation_id
            )
        ).first()
        if row is None:
            return None
        row.dismissed = 1
        session.commit()
        return _relation_to_dict(row)


def get_organization_dashboard() -> dict:
    owner_id = require_owner_id()
    with SessionLocal() as session:
        ensure_default_topics(session, owner_user_id=owner_id)
        session.commit()
        topics = list_topics_from_db()
        owned_topic_ids = _owned_topic_ids(session, owner_id)
        insights = {
            insight.item_id: _insight_to_dict(insight, owned_topic_ids)
            for insight in session.scalars(_owned_insights(owner_id)).all()
        }
        items = [
            _item_to_dict(item)
            for item in session.scalars(
                select(InboxItemRecord)
                .where(InboxItemRecord.owner_user_id == owner_id)
                .order_by(InboxItemRecord.id.desc())
            ).all()
        ]
        high_priority = []
        needs_review = []
        for item in items:
            insight = insights.get(item["id"])
            if insight is None:
                needs_review.append(
                    {
                        **item,
                        "reason": "Not yet organized",
                    }
                )
                continue
            card = {
                **item,
                "effective_title": insight["effective_title"],
                "effective_priority": insight["effective_priority"],
                "effective_topic_id": insight["effective_topic_id"],
                "rationale": insight["rationale"],
                "has_user_overrides": insight["has_user_overrides"],
            }
            if insight["effective_priority"] == "High":
                high_priority.append(card)
            if insight["needs_review"]:
                needs_review.append({**card, "reason": "Marked for review"})

        latest = get_latest_successful_analysis()
        if latest and latest.get("is_stale"):
            for item in items:
                if item["id"] in insights:
                    needs_review.append(
                        {
                            **item,
                            "effective_title": insights[item["id"]][
                                "effective_title"
                            ],
                            "reason": "Analysis stale — re-analyze recommended",
                        }
                    )
                    break

        open_tasks = list_tasks_from_db(TASK_OPEN)
        relations = list_relations_from_db()
        return {
            "topics": topics,
            "high_priority": high_priority[:20],
            "open_tasks": open_tasks[:20],
            "recently_captured": items[:10],
            "needs_review": needs_review[:20],
            "related_items": relations[:20],
            "analysis": latest,
        }


def _analysis_ready(item):
    return item.item_type in ANALYZABLE_TYPES and item.processing_status in ANALYZABLE_STATUSES and bool(item.extracted_text or item.raw_text)


def _analysis_states(session, owner_id):
    """Bootstrap already-saved legacy results once, without paying to analyze them again."""
    from app.analysis.fingerprints import input_fingerprint, analysis_version
    items = list(session.scalars(select(InboxItemRecord).where(
        InboxItemRecord.owner_user_id == owner_id).order_by(InboxItemRecord.id)).all())
    for item in items:
        if session.get(ItemAnalysisStateRecord, item.id) is not None:
            continue
        insight = session.get(ItemInsightRecord, item.id)
        run = session.get(AnalysisRunRecord, insight.analysis_run_id) if insight and insight.analysis_run_id is not None else None
        saved = bool(_analysis_ready(item) and run and run.owner_user_id == owner_id
                     and run.status == ANALYSIS_SUCCEEDED
                     and item.task_analysis_fingerprint == _deadline_fingerprint([item]))
        state = ItemAnalysisStateRecord(
            item_id=item.id, status="processed" if saved else "pending",
            input_fingerprint=input_fingerprint(item) if saved else None,
            analysis_version=analysis_version() if saved else None,
            invalidation_generation=0, processed_generation=0,
            last_run_id=run.id if saved else None,
        )
        # A capture can be polled while organization claims it. The competing
        # initializer owns the same fact; never overwrite its claim or generation.
        try:
            with session.begin_nested():
                session.add(state)
                session.flush()
        except IntegrityError:
            if session.get(ItemAnalysisStateRecord, item.id) is None:
                raise
    session.flush()
    return items


def _analysis_eligible(item, state):
    from app.analysis.fingerprints import input_fingerprint, analysis_version
    if not _analysis_ready(item):
        return False
    fingerprint = input_fingerprint(item)
    same_version = state.analysis_version == analysis_version()
    if state.status == "processed" and same_version and state.input_fingerprint == fingerprint and state.processed_generation == state.invalidation_generation:
        return False
    if state.status in {"failed", "blocked", "processing"} and same_version and state.claimed_fingerprint == fingerprint:
        return False
    return True


def _effective_item_titles(session, items):
    insights = {row.item_id: row for row in session.scalars(select(ItemInsightRecord).where(
        ItemInsightRecord.item_id.in_([item.id for item in items]))).all()}
    return {item.id: (insights[item.id].user_title or insights[item.id].concise_title)
            if item.id in insights else _default_item_title(item) for item in items}


def list_analysis_states():
    from app.analysis.fingerprints import input_fingerprint
    owner_id = require_owner_id()
    with SessionLocal() as session:
        items = _analysis_states(session, owner_id)
        titles = _effective_item_titles(session, items)
        result = []
        for item in items:
            state = session.get(ItemAnalysisStateRecord, item.id)
            result.append({"item_id": item.id, "title": titles[item.id],
                           "status": "pending" if _analysis_eligible(item, state) else state.status,
                           "input_fingerprint": state.input_fingerprint,
                           "current_fingerprint": input_fingerprint(item), "analysis_version": state.analysis_version,
                           "invalidation_generation": state.invalidation_generation,
                           "processed_generation": state.processed_generation,
                           "error": state.error, "last_run_id": state.last_run_id})
        session.commit()
        return result


def invalidate_item_analysis(item_id):
    owner_id = require_owner_id()
    with SessionLocal() as session:
        item = _owned_item(session, item_id, owner_id)
        if item is None:
            return None
        _analysis_states(session, owner_id)
        state = session.get(ItemAnalysisStateRecord, item_id)
        state.invalidation_generation += 1
        state.status, state.error, state.claim_token = "pending", None, None
        session.commit()
        return {"item_id": item_id, "status": "pending", "invalidation_generation": state.invalidation_generation}


def prepare_incremental_batch(run_id):
    from datetime import timedelta
    import uuid
    from app.analysis.fingerprints import input_fingerprint, analysis_version
    owner_id = require_owner_id()
    with SessionLocal() as session:
        items = _analysis_states(session, owner_id)
        token = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        targets, generations, characters = [], {}, 0
        for item in items:
            state = session.scalar(select(ItemAnalysisStateRecord).where(
                ItemAnalysisStateRecord.item_id == item.id).with_for_update())
            if state.status == "processing" and state.claimed_at:
                if datetime.fromisoformat(state.claimed_at) < now - timedelta(minutes=15):
                    state.status, state.error = "failed", "Interrupted analysis claim. Explicitly retry this item."
                    state.claim_token = None
            if not _analysis_eligible(item, state):
                continue
            text = item.extracted_text or item.raw_text
            fingerprint = input_fingerprint(item)
            if len(text) > 24000:
                state.status, state.error = "blocked", "Source exceeds the 24000-character item budget; shorten or split it, then retry."
                state.claimed_fingerprint, state.analysis_version = fingerprint, analysis_version()
                continue
            if len(targets) >= 8 or characters + len(text) > 48000:
                continue
            # SQLite ignores FOR UPDATE. A compare-and-set is required too:
            # only the request that acquired this exact state may call a model.
            acquired = session.execute(update(ItemAnalysisStateRecord).where(
                ItemAnalysisStateRecord.item_id == item.id,
                ItemAnalysisStateRecord.status == state.status,
                ItemAnalysisStateRecord.claim_token == state.claim_token,
                ItemAnalysisStateRecord.claimed_fingerprint == state.claimed_fingerprint,
                ItemAnalysisStateRecord.analysis_version == state.analysis_version,
                ItemAnalysisStateRecord.invalidation_generation == state.invalidation_generation,
            ).values(status="processing", error=None, analysis_version=analysis_version(),
                     claimed_fingerprint=fingerprint, claim_token=token, claimed_at=now.isoformat(),
                     last_run_id=run_id).execution_options(synchronize_session=False))
            if acquired.rowcount != 1:
                continue
            targets.append({"id": item.id, "text": text, "item_type": item.item_type,
                            "input_fingerprint": fingerprint, "reference_at": item.time_reference_at,
                            "timezone": item.time_reference_timezone, "created_at": item.created_at})
            generations[item.id] = state.invalidation_generation
            characters += len(text)
        session.commit()
        return {"targets": targets, "claim_token": token, "generations": generations, "version": analysis_version()}


def release_incremental_claim(claim, error, *, blocked=False):
    owner_id = require_owner_id()
    with SessionLocal() as session:
        states = session.scalars(select(ItemAnalysisStateRecord).where(
            ItemAnalysisStateRecord.item_id.in_(_owned_item_ids(owner_id)),
            ItemAnalysisStateRecord.claim_token == claim["claim_token"],
        )).all()
        for state in states:
            state.status = "blocked" if blocked else "failed"
            state.error, state.claim_token = error[:1000], None
        session.commit()


def _task_context(session, task):
    value = _task_to_dict(session, task)
    sources = _deadline_sources(session, value["source_item_ids"])
    corrections = {}
    for source in sources:
        insight = session.get(ItemInsightRecord, source.id)
        if insight:
            corrections[str(source.id)] = {name: getattr(insight, name) for name in (
                "user_title", "user_priority", "user_topic_id", "user_tags_json")
                if getattr(insight, name) is not None}
    value["corrections"] = corrections
    value["source_text"] = "\n".join(item.extracted_text or item.raw_text or "" for item in sources)
    value["source_fingerprint"] = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return value


def analysis_context_candidates():
    from app.analysis.fingerprints import input_fingerprint
    owner_id = require_owner_id()
    with SessionLocal() as session:
        tasks = [_task_context(session, task) for task in session.scalars(select(TaskRecord).where(
            TaskRecord.owner_user_id == owner_id).order_by(TaskRecord.id)).all()]
        snippets = []
        for item in session.scalars(select(InboxItemRecord).where(InboxItemRecord.owner_user_id == owner_id)).all():
            if not _analysis_ready(item):
                continue
            insight = session.get(ItemInsightRecord, item.id)
            corrections = {name: getattr(insight, name) for name in (
                "user_title", "user_priority", "user_topic_id", "user_tags_json")
                if insight and getattr(insight, name) is not None}
            snippets.append({"id": item.id, "text": item.extracted_text or item.raw_text,
                             "item_type": item.item_type, "source_url": item.source_url,
                             "reference_at": item.time_reference_at, "timezone": item.time_reference_timezone,
                             "corrections": corrections, "input_fingerprint": input_fingerprint(item)})
        return tasks, snippets


def _incremental_view(session, run, stored):
    """A live read projection; only the delta is persisted in analysis_runs."""
    owner_id = require_owner_id()
    buckets, priority = {}, {"High": [], "Medium": [], "Low": []}
    topic_ids = _owned_topic_ids(session, owner_id)
    for insight in session.scalars(_owned_insights(owner_id).order_by(ItemInsightRecord.item_id)).all():
        value = _insight_to_dict(insight, topic_ids)
        topic = session.get(TopicRecord, value["effective_topic_id"]) if value["effective_topic_id"] else None
        category = topic.name if topic else value["category"]
        note = {"id": insight.item_id, "title": value["effective_title"], "priority": value["effective_priority"],
                "tags": value["effective_tags"], "rationale": value["rationale"]}
        buckets.setdefault(category, []).append(note)
        priority[note["priority"]].append({"id": note["id"], "title": note["title"]})
    todos = []
    for task in session.scalars(select(TaskRecord).where(TaskRecord.owner_user_id == owner_id).order_by(TaskRecord.id)).all():
        saved = _task_to_dict(session, task)
        source_ids = saved["source_item_ids"]
        if source_ids:
            todos.append({"task": saved["text"], "priority": saved["priority"], "source_note_ids": source_ids, "when": task.timing_text})
    items = session.scalars(select(InboxItemRecord).where(InboxItemRecord.owner_user_id == owner_id)).all()
    pending = 0
    for item in items:
        state = session.get(ItemAnalysisStateRecord, item.id)
        pending += int(_analysis_ready(item) and (state is None or _analysis_eligible(item, state)))
    metrics = dict(stored.get("metrics", {}), pending_count=pending)
    return {"buckets": [{"name": name, "notes": notes} for name, notes in buckets.items()], "todos": todos,
            "priority_view": priority, "mode": run.mode, "analysis_run_id": run.id,
            "generated_at": run.completed_at, "is_stale": bool(pending),
            "inbox_fingerprint": compute_inbox_fingerprint(session),
            "metrics": metrics, "changes": stored.get("applied_changes", [])}


def _save_analysis_review(session, run_id, item_id, task_id, kind, reason):
    from app.analysis.fingerprints import input_fingerprint
    owner_id = require_owner_id()
    source = _owned_item(session, item_id, owner_id)
    key = hashlib.sha256(json.dumps([owner_id, item_id, task_id, kind,
                                    input_fingerprint(source)]).encode()).hexdigest()
    row = session.scalar(select(AnalysisReviewRecord).where(AnalysisReviewRecord.dedup_key == key))
    if row is None:
        row = AnalysisReviewRecord(owner_user_id=owner_id, run_id=run_id, item_id=item_id,
                                   candidate_task_id=task_id, kind=kind, reason=reason, status="pending",
                                   dedup_key=key, created_at=datetime.now(timezone.utc).isoformat())
        session.add(row)
    if task_id is not None:
        from app.similarity import similarity_score
        task = _owned_task(session, task_id, owner_id)
        source = _owned_item(session, item_id, owner_id)
        if task is not None and source is not None:
            for other_id in _task_to_dict(session, task)["source_item_ids"]:
                if other_id == item_id:
                    continue
                a, b = sorted([item_id, other_id])
                relation_type = "near_duplicate" if kind == "possible_duplicate" else "related"
                existing = session.scalar(select(ItemRelationRecord).where(
                    ItemRelationRecord.item_id_a == a, ItemRelationRecord.item_id_b == b,
                    ItemRelationRecord.relation_type == relation_type))
                if existing is None:
                    other = _owned_item(session, other_id, owner_id)
                    if other is None:
                        continue
                    score = similarity_score(source.extracted_text or source.raw_text or "", other.extracted_text or other.raw_text or "")
                    session.add(ItemRelationRecord(item_id_a=a, item_id_b=b, relation_type=relation_type,
                                                   score=str(score), dismissed=0, created_at=datetime.now(timezone.utc).isoformat()))
    return row


def _attach_analysis_source(session, task, source_ids):
    saved = _task_to_dict(session, task)
    linked = set(saved["source_item_ids"])
    preserve_source_warning = bool(linked and saved["source_changed"])
    for item_id in source_ids:
        if item_id not in linked:
            session.add(TaskSourceItemRecord(task_id=task.id, item_id=item_id))
    session.flush()
    # New, validated provenance does not reinterpret an existing deadline.
    if not set(source_ids) <= linked and not preserve_source_warning:
        all_ids = sorted(linked | set(source_ids))
        task.deadline_source_fingerprint = _deadline_fingerprint(_deadline_sources(session, all_ids))


def apply_incremental_delta(run_id, claim, plan, context, metrics, mode):
    from app.analysis.contracts import validate_plan
    from app.analysis.context import equivalent_obligations
    from app.analysis.fingerprints import input_fingerprint
    plan = validate_plan(plan, claim["targets"], context)
    if plan["lookup_requests"]:
        raise ValueError("Unresolved context requests cannot be persisted as completed analysis")
    owner_id = require_owner_id()
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        run = session.get(AnalysisRunRecord, run_id)
        if run is None or run.owner_user_id != owner_id or run.status != ANALYSIS_RUNNING:
            raise ValueError("Analysis run unavailable")
        # Repair only invalid historical ownership links. Valid relations,
        # including dismissed ones, are never globally regenerated by a delta.
        touching = session.scalars(select(ItemRelationRecord).where(or_(
            ItemRelationRecord.item_id_a.in_(_owned_item_ids(owner_id)),
            ItemRelationRecord.item_id_b.in_(_owned_item_ids(owner_id)),
        ))).all()
        for relation in touching:
            if (_owned_item(session, relation.item_id_a, owner_id) is None
                    or _owned_item(session, relation.item_id_b, owner_id) is None):
                session.delete(relation)
        referenced = {change["task_id"] for change in plan["changes"] if change["task_id"] is not None}
        parent_ids = {row["id"] for row in claim["targets"]} | {row["id"] for row in context.get("snippets", [])}
        parent_ids.update(session.scalars(select(TaskSourceItemRecord.item_id).join(TaskRecord).where(
            TaskRecord.owner_user_id == owner_id, TaskRecord.id.in_(referenced))).all())
        locked_sources = {row.id: row for row in session.scalars(select(InboxItemRecord).where(
            InboxItemRecord.owner_user_id == owner_id, InboxItemRecord.id.in_(parent_ids)
        ).order_by(InboxItemRecord.id).with_for_update()).all()}
        by_id = {}
        for target in claim["targets"]:
            item = locked_sources.get(target["id"])
            state = session.get(ItemAnalysisStateRecord, target["id"])
            if (item is None or state is None or state.claim_token != claim["claim_token"]
                    or state.invalidation_generation != claim["generations"][target["id"]]
                    or input_fingerprint(item) != target["input_fingerprint"]):
                raise SourceChangedDuringAnalysis("Analysis input changed. Explicitly retry the updated item.")
            by_id[item.id] = item
        for snippet in context.get("snippets", []):
            item = locked_sources.get(snippet["id"])
            if item is None or input_fingerprint(item) != snippet.get("input_fingerprint"):
                raise SourceChangedDuringAnalysis("Supporting source changed. Retry with fresh context.")
        context_tasks = {row["id"]: row for row in context.get("tasks", [])}
        for task_id in sorted(referenced):
            task = session.scalar(select(TaskRecord).where(TaskRecord.id == task_id,
                TaskRecord.owner_user_id == owner_id).with_for_update())
            if task is None:
                raise ValueError("Task reference is not available to this owner")
            if _task_context(session, task)["source_fingerprint"] != context_tasks[task_id].get("source_fingerprint"):
                raise SourceChangedDuringAnalysis("Supporting task changed. Explicitly retry analysis with fresh context.")
        for organization in plan["organizations"]:
            item_id = organization["item_id"]
            insight = session.get(ItemInsightRecord, item_id)
            if insight is None:
                insight = ItemInsightRecord(item_id=item_id)
                session.add(insight)
            insight.analysis_run_id, insight.category = run_id, organization["category"]
            insight.concise_title, insight.priority = organization["title"], organization["priority"]
            insight.tags_json = json.dumps(organization["tags"], ensure_ascii=False)
            insight.rationale, insight.updated_at = organization["rationale"], now
            insight.topic_id = _topic_id_by_name(session, organization["category"])
            if insight.user_title is None:
                by_id[item_id].title = organization["title"]

        local_tasks, applied = {}, []
        original_task_ids = set(session.scalars(select(TaskRecord.id).where(TaskRecord.owner_user_id == owner_id)).all())
        matched_original_ids = set()
        for change in plan["changes"]:
            source_ids = sorted(change["source_item_ids"])
            sources = [by_id[item_id] for item_id in source_ids]
            task = _owned_task(session, change["task_id"], owner_id) if change["task_id"] else local_tasks.get(change["existing_proposal_id"])
            operation = change["operation"]
            todo = {"task": change["text"] or "", "when": change["timing_text"],
                    "priority": change["priority"], "source_note_ids": source_ids}
            if operation == "create_task":
                # Match changed-source obligations server-side too, including
                # candidates omitted by the supporting-context budget.
                candidates = list(session.scalars(select(TaskRecord).where(
                    TaskRecord.owner_user_id == owner_id,
                    TaskRecord.id.in_(select(TaskSourceItemRecord.task_id).where(TaskSourceItemRecord.item_id.in_(source_ids))),
                    TaskRecord.id.in_(original_task_ids - matched_original_ids)).order_by(TaskRecord.id).with_for_update()).all())
                candidates = [candidate for candidate in candidates if set(_task_to_dict(session, candidate)["source_item_ids"]) == set(source_ids)]
                same_timing = [candidate for candidate in candidates if timing_intent_signature(
                    candidate.ai_timing_text or candidate.timing_text, unique=True) == timing_intent_signature(change["timing_text"], unique=True)]
                if same_timing:
                    candidates = same_timing
                task, ambiguous = _match_edited_task(candidates, change["text"])
                if ambiguous:
                    for candidate in ambiguous:
                        _save_analysis_review(session, run_id, source_ids[0], candidate.id, "possible_duplicate", "Edited obligation matches multiple existing tasks.")
                        if candidate.deadline_status == "inferred" and not candidate.timing_confirmed:
                            candidate.deadline_needs_review = 1
                            candidate.deadline_note = "Source edit could match multiple obligations. Previous deadline and reference retained; review the source."
                    applied.append({"proposal_id": change["proposal_id"], "operation": "flag_possible_duplicate", "source_item_ids": source_ids})
                    continue
                if task is None:
                    inferred = _source_deadline(todo, sources)
                    if any(item.task_analysis_fingerprint and item.updated_at and item.updated_at != item.time_reference_at for item in sources):
                        inferred.update(due_on=None, due_confidence="low", deadline_needs_review=1,
                            deadline_note="New obligation in an edited source with older timing. Choose its deadline; no reference was guessed.")
                    identity = [owner_id, source_ids, change["text"], change["timing_text"],
                                [row["input_fingerprint"] for row in claim["targets"] if row["id"] in source_ids]]
                    key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
                    task = session.scalar(select(TaskRecord).where(TaskRecord.dedup_key == key, TaskRecord.owner_user_id == owner_id))
                if task is None:
                    task = TaskRecord(text=change["text"], priority=change["priority"], status=TASK_OPEN,
                                      timing_confirmed=0, dedup_key=key,
                                      owner_user_id=owner_id, created_at=now, updated_at=now,
                                      **inferred)
                    session.add(task)
                    session.flush()
                    _attach_analysis_source(session, task, source_ids)
                else:
                    _update_linked_delta_task(session, task, todo, sources, now)
                    matched_original_ids.add(task.id)
                local_tasks[change["proposal_id"]] = task
            elif task is None:
                raise ValueError("Proposal task reference unavailable")
            elif operation.startswith("flag_"):
                for item_id in source_ids:
                    _save_analysis_review(session, run_id, item_id, task.id,
                                          "possible_duplicate" if operation == "flag_possible_duplicate" else "related", change["reason"])
            elif operation in {"attach_source", "update_task"}:
                linked = set(_task_to_dict(session, task)["source_item_ids"])
                if change["existing_proposal_id"] is not None and operation == "attach_source":
                    candidate = dict(_source_deadline(todo, sources), text=change["text"] or "",
                                     reference_at=sources[0].time_reference_at, timezone=sources[0].time_reference_timezone)
                    if equivalent_obligations(candidate, _task_context(session, task)) != "equivalent":
                        raise ValueError("Batch-local proposals must establish the same occurrence before attaching")
                if not set(source_ids) <= linked:
                    inferred = _source_deadline(todo, sources) if change["text"] else {}
                    candidate = dict(inferred, text=change["text"] or (sources[0].extracted_text or sources[0].raw_text),
                                     source_text="\n".join(item.extracted_text or item.raw_text or "" for item in sources),
                                     reference_at=sources[0].time_reference_at, timezone=sources[0].time_reference_timezone)
                    existing = _task_context(session, task)
                    if existing["source_changed"] and existing["deadline_status"] in {"inferred", "legacy"}:
                        raise SourceChangedDuringAnalysis("Existing source timing needs review before new provenance can be attached.")
                    from app.analysis.planner import same_reference_detail
                    if equivalent_obligations(candidate, existing) != "equivalent" and not same_reference_detail(candidate, existing):
                        raise ValueError("Automatic attachment lacks clear obligation and occurrence evidence")
                    _attach_analysis_source(session, task, source_ids)
                elif operation == "attach_source" and change["text"]:
                    peers = list(session.scalars(select(TaskRecord).where(
                        TaskRecord.owner_user_id == owner_id, TaskRecord.id.in_(original_task_ids - matched_original_ids),
                        TaskRecord.id.in_(select(TaskSourceItemRecord.task_id).where(TaskSourceItemRecord.item_id.in_(source_ids)))
                    ).order_by(TaskRecord.id).with_for_update()).all())
                    peers = [peer for peer in peers if set(_task_to_dict(session, peer)["source_item_ids"]) == set(source_ids)]
                    same_timing = [peer for peer in peers if timing_intent_signature(peer.ai_timing_text or peer.timing_text, unique=True)
                                   == timing_intent_signature(change["timing_text"], unique=True)]
                    selected, ambiguous = _match_edited_task(same_timing or peers, change["text"])
                    if ambiguous:
                        for peer in ambiguous:
                            _save_analysis_review(session, run_id, source_ids[0], peer.id, "possible_duplicate", "Source edit could refer to multiple existing obligations.")
                            if peer.deadline_status == "inferred" and not peer.timing_confirmed:
                                peer.deadline_needs_review = 1
                                peer.deadline_note = "Ambiguous source edit. Previous deadline retained; review the source."
                        continue
                    if selected is not None and selected.id != task.id:
                        raise ValueError("The proposed task does not match the edited obligation")
                    if task.id in matched_original_ids:
                        raise ValueError("A saved obligation cannot be rewritten twice in one batch")
                    _update_linked_delta_task(session, task, todo, sources, now)
                    matched_original_ids.add(task.id)
                if operation == "update_task":
                    for item_id in source_ids:
                        exists = session.scalar(select(TaskDetailRecord).where(TaskDetailRecord.task_id == task.id,
                            TaskDetailRecord.source_item_id == item_id, TaskDetailRecord.text == change["fields"]["detail"]))
                        if exists is None:
                            session.add(TaskDetailRecord(task_id=task.id, source_item_id=item_id,
                                                        text=change["fields"]["detail"], created_at=now))
            applied.append({**change, "task_id": task.id})

        for target in claim["targets"]:
            item = by_id[target["id"]]
            state = session.get(ItemAnalysisStateRecord, item.id)
            state.status, state.input_fingerprint = "processed", target["input_fingerprint"]
            state.processed_generation, state.error, state.claim_token = state.invalidation_generation, None, None
            state.last_run_id = run_id
            item.task_analysis_fingerprint = _deadline_fingerprint([item])
        stored = {"contract_version": "delta-v1", "targets": [{"id": row["id"], "input_fingerprint": row["input_fingerprint"]} for row in claim["targets"]],
                  "plan": plan, "applied_changes": applied, "metrics": metrics}
        run.status, run.mode, run.completed_at = ANALYSIS_SUCCEEDED, mode, now
        run.result_json = json.dumps(stored, ensure_ascii=False)
        session.commit()
        return _incremental_view(session, run, stored)


def _update_linked_delta_task(session, task, todo, sources, now):
    matched, ambiguous = _match_edited_task([task], todo["task"])
    if matched is None or ambiguous:
        raise ValueError("An unrelated obligation cannot rewrite an existing linked task")
    task.text = todo["task"]
    if not any(session.get(ItemInsightRecord, item.id).user_priority for item in sources if session.get(ItemInsightRecord, item.id)):
        task.priority = todo["priority"]
    if task.deadline_status == "inferred" and not task.timing_confirmed:
        _reconcile_inferred_deadline(task, _source_deadline(todo, sources))
        if not task.deadline_needs_review:
            task.deadline_source_fingerprint = _deadline_fingerprint(_deadline_sources(session, _task_to_dict(session, task)["source_item_ids"]))
    task.updated_at = now


def list_analysis_reviews():
    owner_id = require_owner_id()
    with SessionLocal() as session:
        rows = session.scalars(select(AnalysisReviewRecord).where(
            AnalysisReviewRecord.owner_user_id == owner_id,
            AnalysisReviewRecord.item_id.in_(_owned_item_ids(owner_id)),
        ).order_by(AnalysisReviewRecord.id)).all()
        items = session.scalars(select(InboxItemRecord).where(
            InboxItemRecord.owner_user_id == owner_id,
            InboxItemRecord.id.in_([row.item_id for row in rows]))).all()
        titles = _effective_item_titles(session, items)
        tasks = {task.id: task for task in session.scalars(select(TaskRecord).where(
            TaskRecord.owner_user_id == owner_id,
            TaskRecord.id.in_([row.candidate_task_id for row in rows if row.candidate_task_id is not None]))).all()}
        return [{"id": row.id, "item_id": row.item_id,
                 "candidate_task_id": row.candidate_task_id if row.candidate_task_id in tasks else None,
                 "source_title": titles[row.item_id],
                 "candidate_task_text": (tasks[row.candidate_task_id].user_text or tasks[row.candidate_task_id].text)
                     if row.candidate_task_id in tasks else None,
                 "kind": row.kind, "reason": row.reason, "status": row.status} for row in rows]


def resolve_analysis_review(review_id, decision):
    owner_id = require_owner_id()
    with SessionLocal() as session:
        review = session.scalar(select(AnalysisReviewRecord).where(
            AnalysisReviewRecord.id == review_id, AnalysisReviewRecord.owner_user_id == owner_id))
        if review is None or _owned_item(session, review.item_id, owner_id) is None:
            return None
        if review.status != "pending":
            return {"id": review.id, "status": review.status}
        if decision == "attach_source":
            task = _owned_task(session, review.candidate_task_id, owner_id, for_update=True)
            if task is None:
                return None
            _attach_analysis_source(session, task, [review.item_id])
            review.status = "attached"
        elif decision == "keep_separate":
            review.status = "kept_separate"
        else:
            raise ValueError("Unknown review decision")
        session.commit()
        return {"id": review.id, "status": review.status}
