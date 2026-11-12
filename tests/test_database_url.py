from app.database import normalize_database_url


def test_bare_postgres_urls_use_psycopg3():
    assert (
        normalize_database_url("postgresql://u:p@h:5432/db")
        == "postgresql+psycopg://u:p@h:5432/db"
    )
    assert (
        normalize_database_url("postgres://u:p@h/db")
        == "postgresql+psycopg://u:p@h/db"
    )


def test_explicit_driver_and_sqlite_unchanged():
    assert (
        normalize_database_url("postgresql+psycopg://u:p@h/db")
        == "postgresql+psycopg://u:p@h/db"
    )
    assert normalize_database_url("sqlite:///x.db") == "sqlite:///x.db"
