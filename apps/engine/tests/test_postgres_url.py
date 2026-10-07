from app.db.postgres import prepare_asyncpg


def test_neon_sslmode_is_not_passed_to_asyncpg():
    url, args = prepare_asyncpg(
        "postgresql://user:secret@ep-example.c-2.sa-east-1.aws.neon.tech/neondb?sslmode=require"
    )
    assert url.startswith("postgresql+asyncpg://")
    assert "sslmode" not in url
    assert "ssl" in args


def test_pooler_disables_statement_cache_and_drops_channel_binding():
    url, args = prepare_asyncpg(
        "postgresql://user:secret@ep-example-pooler.c-2.sa-east-1.aws.neon.tech/neondb?channel_binding=require&sslmode=require"
    )
    assert "channel_binding" not in url
    assert "sslmode" not in url
    assert args["statement_cache_size"] == 0
    assert "ssl" in args
