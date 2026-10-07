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


def test_binance_urls_drop_pasted_newlines():
    from app.config import Settings

    settings = Settings(
        _env_file=None,
        binance_spot_rest_url="https://data-api.binance.vision\n",
        binance_spot_ws_url="wss://data-stream.binance.vision/stream\r\n",
    )
    assert settings.binance_spot_rest_url == "https://data-api.binance.vision"
    assert settings.binance_spot_ws_url == "wss://data-stream.binance.vision/stream"
    assert "\n" not in settings.binance_spot_rest_url
