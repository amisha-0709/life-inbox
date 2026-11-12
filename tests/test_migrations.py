import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def alembic_config(database_path: Path) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    return config


def test_upgrade_creates_inbox_items_in_empty_database(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    database_path = tmp_path / "empty.db"

    command.upgrade(alembic_config(database_path), "head")

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        inspector = inspect(engine)
        assert {
            "alembic_version",
            "analysis_runs",
            "inbox_items",
            "item_insights",
            "item_relations",
            "notes",
            "task_source_items",
            "tasks",
            "topics",
            "calendar_action_receipts",
            "users",
            "item_analysis_state",
            "task_details",
            "analysis_reviews",
        } <= set(inspector.get_table_names())
        columns = {
            column["name"]: column
            for column in inspector.get_columns("inbox_items")
        }
        assert set(columns) == {
            "id",
            "item_type",
            "title",
            "raw_text",
            "source_url",
            "original_filename",
            "mime_type",
            "processing_status",
            "error_message",
            "storage_path",
            "extracted_text",
            "ai_description",
            "owner_user_id",
            "created_at",
            "updated_at",
            "time_reference_at",
            "time_reference_timezone",
            "task_analysis_fingerprint",
        }
        assert columns["updated_at"]["nullable"] is True
        assert next(column for column in inspector.get_columns("item_insights")
                    if column["name"] == "analysis_run_id")["nullable"] is True
        assert {
            column["name"] for column in inspector.get_columns("item_analysis_state")
        } == {
            "item_id", "input_fingerprint", "analysis_version", "status",
            "invalidation_generation", "processed_generation", "claimed_fingerprint",
            "claim_token", "claimed_at", "error", "last_run_id",
        }
        assert {
            column["name"] for column in inspector.get_columns("task_details")
        } == {"id", "task_id", "source_item_id", "text", "created_at"}
        assert {
            column["name"] for column in inspector.get_columns("analysis_reviews")
        } == {
            "id", "owner_user_id", "run_id", "item_id", "candidate_task_id",
            "kind", "reason", "status", "dedup_key", "created_at",
        }
        assert {
            column["name"]
            for column in inspector.get_columns("analysis_runs")
        } == {
            "id",
            "started_at",
            "completed_at",
            "status",
            "mode",
            "result_json",
            "owner_user_id",
        }
        assert {
            column["name"]
            for column in inspector.get_columns("item_insights")
        } == {
            "item_id",
            "analysis_run_id",
            "category",
            "concise_title",
            "priority",
            "topic_id",
            "tags_json",
            "rationale",
            "user_title",
            "user_topic_id",
            "user_priority",
            "user_tags_json",
            "needs_review",
            "updated_at",
        }
        assert {
            column["name"]
            for column in inspector.get_columns("tasks")
        } == {
            "id",
            "text",
            "priority",
            "user_text",
            "user_priority",
            "timing_text",
            "ai_timing_text",
            "timing_confirmed",
            "due_on",
            "due_confidence",
            "deadline_status",
            "deadline_needs_review",
            "deadline_note",
            "deadline_reference_at",
            "deadline_timezone",
            "deadline_source_fingerprint",
            "snoozed_until",
            "reminder_at",
            "status",
            "dedup_key",
            "owner_user_id",
            "created_at",
            "updated_at",
        }
        assert "calendar_action_receipts" in set(inspector.get_table_names())
        assert {
            column["name"] for column in inspector.get_columns("topics")
        } == {
            "id",
            "name",
            "description",
            "owner_user_id",
            "created_at",
            "updated_at",
            "archived",
        }
        assert {
            column["name"]
            for column in inspector.get_columns("task_source_items")
        } == {"task_id", "item_id"}
        with engine.connect() as connection:
            revision = connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            topic_count = connection.scalar(text("SELECT COUNT(*) FROM topics"))
        assert revision == "0017_task_user_corrections"
        assert topic_count >= 9
    finally:
        engine.dispose()


def test_upgrade_adopts_legacy_database_without_changing_notes(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    database_path = tmp_path / "legacy.db"
    existing_notes = [
        (4, "Existing note", "2026-01-01T10:00:00+00:00"),
        (9, "Another existing note", "2026-01-02T10:00:00+00:00"),
    ]

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO notes (id, text, created_at) VALUES (?, ?, ?)",
            existing_notes,
        )
        connection.commit()

    command.upgrade(alembic_config(database_path), "head")

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        assert {"inbox_items", "notes"} <= set(
            inspect(engine).get_table_names()
        )
        with engine.connect() as connection:
            migrated_notes = connection.execute(
                text(
                    "SELECT id, text, created_at FROM notes ORDER BY id"
                )
            ).all()
            revision = connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            updated_values = connection.execute(
                text("SELECT updated_at FROM notes ORDER BY id")
            ).scalars().all()
            migrated_items = connection.execute(
                text(
                    """
                    SELECT
                        id,
                        item_type,
                        title,
                        raw_text,
                        source_url,
                        original_filename,
                        mime_type,
                        processing_status,
                        created_at,
                        updated_at
                    FROM inbox_items
                    ORDER BY id
                    """
                )
            ).all()
            next_id = connection.execute(
                text(
                    """
                    INSERT INTO inbox_items (
                        item_type,
                        raw_text,
                        processing_status,
                        created_at
                    )
                    VALUES ('text', 'New item', 'ready', '2026-01-03')
                    """
                )
            ).lastrowid
        assert [tuple(note) for note in migrated_notes] == existing_notes
        assert updated_values == [None, None]
        assert [tuple(item) for item in migrated_items] == [
            (
                note_id,
                "text",
                None,
                note_text,
                None,
                None,
                None,
                "ready",
                created_at,
                None,
            )
            for note_id, note_text, created_at in existing_notes
        ]
        assert next_id == 10
        assert revision == "0017_task_user_corrections"
    finally:
        engine.dispose()


def test_upgrade_consolidates_tasks_that_share_a_source(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    database_path = tmp_path / "duplicate_tasks.db"
    config = alembic_config(database_path)
    command.upgrade(config, "0005_create_tasks")

    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO inbox_items (
                    id, item_type, raw_text, processing_status, created_at
                )
                VALUES (1, 'text', 'Prepare notes', 'ready', '2026-01-01')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    id, text, priority, timing_text, status, dedup_key,
                    created_at, updated_at
                )
                VALUES
                    (
                        1, 'Prepare class notes', 'Low', NULL, 'completed',
                        'old-key', '2026-01-01', '2026-01-02'
                    ),
                    (
                        2, 'Prepare notes for class', 'High', NULL, 'open',
                        'new-key', '2026-01-03', '2026-01-04'
                    )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO task_source_items (task_id, item_id)
                VALUES (1, 1), (2, 1)
                """
            )
        )

    command.upgrade(config, "head")

    try:
        with engine.connect() as connection:
            tasks = connection.execute(
                text(
                    """
                    SELECT id, text, priority, status, created_at, updated_at
                    FROM tasks
                    """
                )
            ).all()
            links = connection.execute(
                text(
                    """
                    SELECT task_id, item_id
                    FROM task_source_items
                    """
                )
            ).all()
            revision = connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )

        assert [tuple(task) for task in tasks] == [
            (
                1,
                "Prepare notes for class",
                "High",
                "completed",
                "2026-01-01",
                "2026-01-04",
            )
        ]
        assert [tuple(link) for link in links] == [(1, 1)]
        assert revision == "0017_task_user_corrections"

        # Milestone 2 drops the one-task-per-source unique index so one
        # inbox item may source multiple durable tasks.
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO tasks (
                        id, text, priority, status, dedup_key,
                        created_at, updated_at, timing_confirmed
                    )
                    VALUES (
                        2, 'Another task', 'Medium', 'open',
                        'another-key', '2026-01-05', '2026-01-05', 0
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO task_source_items (task_id, item_id)
                    VALUES (2, 1)
                    """
                )
            )
            multi_links = connection.execute(
                text(
                    """
                    SELECT task_id, item_id
                    FROM task_source_items
                    ORDER BY task_id
                    """
                )
            ).all()
        assert [tuple(link) for link in multi_links] == [(1, 1), (2, 1)]
    finally:
        engine.dispose()


def test_topic_uniqueness_is_per_owner_and_keeps_existing_rows(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    database_path = tmp_path / "topics_unique.db"
    config = alembic_config(database_path)
    command.upgrade(config, "0011_multi_user_ownership")

    engine = create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        before = connection.execute(
            text(
                """
                SELECT id, name, description, owner_user_id, created_at,
                       updated_at, archived
                FROM topics
                ORDER BY id
                """
            )
        ).all()
        assert any(
            row.name == "Work" and row.owner_user_id == 1 for row in before
        )
        assert len(before) >= 9
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO topics (
                        name, description, created_at, updated_at,
                        archived, owner_user_id
                    )
                    VALUES (
                        'Work', NULL, '2026-01-02T00:00:00+00:00',
                        '2026-01-02T00:00:00+00:00', 0, 2
                    )
                    """
                )
            )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        inspector = inspect(engine)
        uniques = inspector.get_unique_constraints("topics")
        assert not any(
            list(constraint["column_names"]) == ["name"]
            for constraint in uniques
        )
        assert any(
            set(constraint["column_names"]) == {"owner_user_id", "name"}
            for constraint in uniques
        )
        create_sql = ""
        with engine.connect() as connection:
            create_sql = connection.scalar(
                text(
                    """
                    SELECT sql FROM sqlite_master
                    WHERE type = 'table' AND name = 'topics'
                    """
                )
            )
            migrated = connection.execute(
                text(
                    """
                    SELECT id, name, description, owner_user_id, created_at,
                           updated_at, archived
                    FROM topics
                    ORDER BY id
                    """
                )
            ).all()
            sequence = connection.scalar(
                text(
                    """
                    SELECT seq FROM sqlite_sequence WHERE name = 'topics'
                    """
                )
            )
            revision = connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
        assert "AUTOINCREMENT" in create_sql
        assert "uq_topics_name_owner" in create_sql
        assert "UNIQUE (name)" not in create_sql
        assert [tuple(row) for row in migrated] == [tuple(row) for row in before]
        assert sequence >= max(row.id for row in before)
        assert revision == "0017_task_user_corrections"

        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO users (
                        email, display_name, auth_provider, created_at
                    )
                    VALUES (
                        'dev@example.com', 'Dev', 'dev',
                        '2026-01-02T00:00:00+00:00'
                    )
                    """
                )
            )
            dev_id = connection.scalar(
                text("SELECT id FROM users WHERE email = 'dev@example.com'")
            )
            connection.execute(
                text(
                    """
                    INSERT INTO topics (
                        name, description, created_at, updated_at,
                        archived, owner_user_id
                    )
                    VALUES (
                        'Work', NULL, '2026-01-02T00:00:00+00:00',
                        '2026-01-02T00:00:00+00:00', 0, :owner
                    )
                    """
                ),
                {"owner": dev_id},
            )
            owners = connection.execute(
                text(
                    """
                    SELECT owner_user_id FROM topics
                    WHERE name = 'Work'
                    ORDER BY owner_user_id
                    """
                )
            ).scalars().all()
        assert owners == [1, dev_id]

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO topics (
                            name, description, created_at, updated_at,
                            archived, owner_user_id
                        )
                        VALUES (
                            'Work', NULL, '2026-01-03T00:00:00+00:00',
                            '2026-01-03T00:00:00+00:00', 0, :owner
                        )
                        """
                    ),
                    {"owner": dev_id},
                )
    finally:
        engine.dispose()


def test_deadline_migration_preserves_saved_dates_and_unknown_historical_context(
    tmp_path, monkeypatch
):
    """A current timezone must never be invented as a historical capture zone."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("LIFE_INBOX_TIMEZONE", "Asia/Kolkata")
    database_path = tmp_path / "saved_deadlines.db"
    config = alembic_config(database_path)
    command.upgrade(config, "0013_google_oauth_subject")
    engine = create_engine(f"sqlite:///{database_path}")
    saved = [
        (1, "Pay bill today", "today", "today", 0, "2026-10-15", "high"),
        (2, "Call plumber tomorrow", "2026-10-17", "tomorrow", 1, "2026-10-17", "high"),
        (3, "Prepare notes today", "today", "today", 0, None, None),
        (4, "Review invalid saved date", "today", "today", 0, "invalid-date", "low"),
    ]
    try:
        with engine.begin() as connection:
            for task_id, task_text, timing, ai_timing, confirmed, due, confidence in saved:
                connection.execute(
                    text("""
                    INSERT INTO inbox_items
                        (id, item_type, raw_text, processing_status, owner_user_id, created_at)
                    VALUES (:id, 'text', :text, 'ready', 1, '2026-10-15T10:00:00+00:00')
                    """),
                    {"id": task_id, "text": task_text},
                )
                connection.execute(
                    text("""
                    INSERT INTO tasks
                        (id, text, priority, timing_text, ai_timing_text, timing_confirmed,
                         due_on, due_confidence, status, dedup_key, owner_user_id,
                         created_at, updated_at)
                    VALUES (:id, :text, 'High', :timing, :ai, :confirmed, :due, :confidence,
                            'open', :key, 1, '2026-10-15T10:00:00+00:00',
                            '2026-10-15T10:00:00+00:00')
                    """),
                    {"id": task_id, "text": task_text, "timing": timing,
                     "ai": ai_timing, "confirmed": confirmed, "due": due,
                     "confidence": confidence, "key": f"legacy-deadline-{task_id}"},
                )
                connection.execute(
                    text("INSERT INTO task_source_items (task_id, item_id) VALUES (:id, :id)"),
                    {"id": task_id},
                )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            after = connection.execute(text("""
                SELECT id, text, timing_text, ai_timing_text, timing_confirmed,
                       due_on, due_confidence FROM tasks ORDER BY id
            """)).all()
            metadata = connection.execute(text("""
                SELECT id, deadline_status, deadline_needs_review,
                       deadline_reference_at, deadline_timezone
                FROM tasks ORDER BY id
            """)).all()
            source_references = connection.execute(text("""
                SELECT time_reference_at, time_reference_timezone, task_analysis_fingerprint
                FROM inbox_items ORDER BY id
            """)).all()
        assert [tuple(row) for row in after] == saved
        assert metadata[0].deadline_status == "legacy"
        assert metadata[0].deadline_needs_review == 1
        assert metadata[1].deadline_status == "confirmed"
        assert metadata[1].deadline_needs_review == 0
        assert metadata[2].deadline_needs_review == 1
        assert metadata[3].deadline_needs_review == 1
        assert all(row.deadline_reference_at is None and row.deadline_timezone is None
                   for row in metadata)
        assert [tuple(row) for row in source_references] == [(None, None, None)] * 4
    finally:
        engine.dispose()


def test_pending_correction_migration_preserves_existing_insights(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    path = tmp_path / "existing-insights.db"
    config = alembic_config(path)
    command.upgrade(config, "0015_incremental_analysis")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            owner_id = connection.scalar(text("SELECT id FROM users ORDER BY id LIMIT 1"))
            connection.execute(text("""INSERT INTO inbox_items
                (id,item_type,raw_text,processing_status,owner_user_id,created_at)
                VALUES (1001,'text','Call home tomorrow','ready',:owner,'2026-10-15T00:00:00+00:00')"""), {"owner": owner_id})
            connection.execute(text("""INSERT INTO analysis_runs
                (id,started_at,completed_at,status,mode,result_json,owner_user_id)
                VALUES (1001,'2026-10-15','2026-10-15','succeeded','fallback_no_api_call','{}',:owner)"""), {"owner": owner_id})
            connection.execute(text("""INSERT INTO item_insights
                (item_id,analysis_run_id,category,concise_title,priority,user_title,user_priority,needs_review,updated_at)
                VALUES (1001,1001,'Personal','Call home','Medium','My corrected title','Low',1,'2026-10-16')"""))
            before = tuple(connection.execute(text("SELECT * FROM item_insights WHERE item_id=1001")).one())
        command.upgrade(config, "head")
        with engine.begin() as connection:
            assert tuple(connection.execute(text("SELECT * FROM item_insights WHERE item_id=1001")).one()) == before
            connection.execute(text("""INSERT INTO inbox_items
                (id,item_type,raw_text,processing_status,owner_user_id,created_at)
                VALUES (1002,'text','My new capture','ready',:owner,'2026-10-20')"""), {"owner": owner_id})
            connection.execute(text("""INSERT INTO item_insights
                (item_id,analysis_run_id,category,concise_title,priority,user_title,needs_review,updated_at)
                VALUES (1002,NULL,'Unorganized','My new capture','Medium','My choice',0,'2026-10-20')"""))
            assert connection.scalar(text("SELECT analysis_run_id FROM item_insights WHERE item_id=1002")) is None
            assert connection.scalar(text("SELECT COUNT(*) FROM analysis_runs")) == 1
    finally:
        engine.dispose()


def test_task_correction_migration_keeps_existing_task_and_provenance(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    path = tmp_path / "task-corrections.db"
    config = alembic_config(path)
    command.upgrade(config, "0016_pending_item_corrections")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            owner_id = connection.scalar(text("SELECT id FROM users ORDER BY id LIMIT 1"))
            connection.execute(text("""INSERT INTO inbox_items
                (id,item_type,raw_text,processing_status,owner_user_id,created_at)
                VALUES (1001,'text','Call home today','ready',:owner,'2026-10-15')"""), {"owner": owner_id})
            connection.execute(text("""INSERT INTO tasks
                (id,text,priority,status,dedup_key,owner_user_id,created_at,updated_at,timing_text,
                 due_on,deadline_status,timing_confirmed,deadline_needs_review,deadline_reference_at,
                 deadline_timezone,deadline_source_fingerprint)
                VALUES (1001,'Call home','High','completed','unique-task',:owner,'2026-10-15','2026-10-16',
                        'today','2026-10-15','confirmed',1,0,'2026-10-15T10:00:00+00:00','Asia/Kolkata','original-fingerprint')"""), {"owner": owner_id})
            connection.execute(text("INSERT INTO task_source_items(task_id,item_id) VALUES(1001,1001)"))
            before = dict(connection.execute(text("SELECT * FROM tasks WHERE id=1001")).mappings().one())
        command.upgrade(config, "head")
        with engine.connect() as connection:
            after = dict(connection.execute(text("SELECT * FROM tasks WHERE id=1001")).mappings().one())
            assert {key: after[key] for key in before} == before
            assert after["user_text"] is None
            assert after["user_priority"] is None
            assert tuple(connection.execute(text("SELECT task_id,item_id FROM task_source_items WHERE task_id=1001")).one()) == (1001, 1001)
    finally:
        engine.dispose()
