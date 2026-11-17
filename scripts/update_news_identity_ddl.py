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


DDL = [
    """
    CREATE INDEX IF NOT EXISTS idx_ranked_news_bd_reference_identity
    ON ranked_news_bd (reference_public_identifier)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ranked_news_bd_reference_connection
    ON ranked_news_bd (reference_public_identifier, connection_public_identifier)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ranked_news_bd_reference_news
    ON ranked_news_bd (reference_public_identifier, news_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_profile_news_seen_bd_reference_seen
    ON profile_news_seen_bd (reference_public_identifier, news_id)
    WHERE seen = TRUE
    """,
]


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


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


async def apply_ddl(*, dry_run: bool = False) -> None:
    if dry_run:
        for statement in DDL:
            print(statement.strip() + ";\n")
        return

    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    async with engine.begin() as conn:
        for statement in DDL:
            await conn.execute(text(statement))
    await engine.dispose()
    logger.info("News identity DDL is up to date.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add indexes for news reference identity alias lookups."
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
    asyncio.run(apply_ddl(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
