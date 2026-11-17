import argparse
import asyncio
import logging
import os
from pathlib import Path

from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.sql import text

ROOT = Path(__file__).resolve().parents[1]

logger = logging.getLogger(__name__)


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


DDL = [
    """
    CREATE TABLE IF NOT EXISTS profile_news_semantics_bd (
        id BIGSERIAL PRIMARY KEY,
        public_identifier TEXT NOT NULL,
        profile_es_id TEXT NULL,
        profile_fingerprint TEXT NOT NULL,
        rubric_version TEXT NOT NULL,
        semantic_query_text TEXT NOT NULL,
        semantic_keywords JSONB NOT NULL DEFAULT '[]'::jsonb,
        event_type_weights JSONB NOT NULL DEFAULT '{}'::jsonb,
        semantic_embedding JSONB NOT NULL DEFAULT '[]'::jsonb,
        semantic_model TEXT NOT NULL,
        rubric JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (
            public_identifier,
            profile_fingerprint,
            rubric_version,
            semantic_model
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS profile_news_seen_bd (
        reference_public_identifier TEXT NOT NULL,
        news_id TEXT NOT NULL,
        seen BOOLEAN NOT NULL DEFAULT FALSE,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (reference_public_identifier, news_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_profile_news_seen_bd_seen_true
    ON profile_news_seen_bd (reference_public_identifier, news_id)
    WHERE seen = TRUE
    """,
]


def _database_url() -> URL:
    missing = [
        name
        for name in ("DB_USERNAME", "DB_PASSWORD", "DB_HOST", "DB_DATABASE")
        if not os.getenv(name)
    ]
    if missing:
        raise RuntimeError(
            f"Missing database environment variables: {', '.join(missing)}"
        )

    return URL.create(
        drivername="postgresql+asyncpg",
        username=os.getenv("DB_USERNAME"),
        password=os.getenv("DB_PASSWORD"),
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT") or "5432"),
        database=os.getenv("DB_DATABASE"),
    )


async def setup_tables(*, dry_run: bool = False) -> None:
    if dry_run:
        for statement in DDL:
            print(statement.strip() + ";\n")
        return

    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    try:
        async with engine.begin() as conn:
            for statement in DDL:
                await conn.execute(text(statement))
        logger.info("News runtime tables are ready.")
    except Exception:
        logger.exception("Failed to set up news runtime tables.")
        raise
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create runtime tables used by semantic news fetching."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print SQL without executing it.",
    )
    parser.add_argument(
        "--skip-dotenv",
        action="store_true",
        help="Do not load environment variables from .env.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    if not args.skip_dotenv:
        _load_dotenv()
    asyncio.run(setup_tables(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
