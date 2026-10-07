from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.enums import OperatingMode, SlippageModelName, TargetMode


def discover_config_dir() -> Path:
    env = __import__("os").environ.get("CONFIG_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "packages" / "configs"
        if candidate.exists():
            return candidate
    return here.parents[3] / "packages" / "configs"


@dataclass(frozen=True)
class BaselineWeightConfig:
    version: str = "baseline_weights_v1"
    trend: float = 0.25
    momentum: float = 0.20
    volume: float = 0.10
    orderflow: float = 0.20
    structure: float = 0.15
    volatility: float = 0.0
    liquidity: float = 0.10
    min_abs_score: float = 0.45
    max_spread_bps: float = 8.0
    min_volume_ratio: float = 0.40
    extreme_atr_normalized: float = 0.05
    high_atr_normalized: float = 0.02
    slope_scale: float = 0.001
    price_vs_ema_scale: float = 0.005
    roc_scale: float = 0.004
    imbalance_strong: float = 0.25
    volume_z_strong: float = 1.0


@dataclass(frozen=True)
class CombinationConfig:
    baseline_version: str = "baseline_score_v1"
    jev_rule_version: str = "jev_veto_only_v1"
    openai_rule_version: str = "openai_jev_veto_only_v1"
    failure_policy: str = "NO_TRADE"
    min_trend_continuation: float = 0.55
    max_reversal: float = 0.50
    max_false_breakout: float = 0.55
    min_pressure_edge: float = 0.05
    max_liquidity_sweep: float = 0.75
    baseline_weight: float = 0.65
    jev_weight: float = 0.35
    max_reversal_risk: float = 0.65
    max_anomaly: float = 0.80
    min_openai_confidence: float = 0.45
    block_long_regimes: tuple[str, ...] = (
        "bearish_expansion",
        "bearish_exhaustion",
        "breakdown",
    )
    block_short_regimes: tuple[str, ...] = ("bullish_expansion", "breakout")


@dataclass
class StrategySettings:
    key: str
    enabled: bool
    mode: OperatingMode
    max_signal_age_ms: int
    call_model: bool = False


@dataclass(frozen=True)
class RiskLimits:
    target_mode: TargetMode = TargetMode.STRUCTURE
    target_fallback: str = "none"
    fixed_target_pct: float = 0.01
    rr_target_multiple: float = 3.0
    atr_buffer_mult: float = 0.10
    atr_stop_mult: float = 1.5
    max_stop_pct: float = 0.02
    min_stop_pct: float = 0.0008
    min_entry_interval_seconds: int = 60
    allow_pyramiding: bool = False
    max_leverage: float = 1.0
    apply_funding: bool = True
    expected_hold_minutes: int = 15
    funding_interval_minutes: int = 480
    order_style: str = "market"
    allow_partial_entry: bool = False
    limit_ttl_ms: int = 60_000
    risk_per_trade: float = 0.005
    max_risk_per_trade: float = 0.01
    max_daily_loss: float = 0.03
    max_daily_drawdown: float = 0.03
    max_total_exposure: float = 1.0
    max_symbol_exposure: float = 0.40
    max_open_positions: int = 3
    min_net_rr: float = 3.0


@dataclass(frozen=True)
class FeeConfig:
    maker_fee_rate: float = 0.0002
    taker_fee_rate: float = 0.0005
    entry_liquidity: str = "taker"
    exit_liquidity: str = "taker"


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def load_file_config(config_dir: Path | None = None) -> dict:
    directory = config_dir or discover_config_dir()
    return {
        "strategies": _load_yaml(directory / "strategies.yaml"),
        "baseline": _load_yaml(directory / "baseline_weights.yaml"),
        "risk": _load_yaml(directory / "risk.yaml"),
        "fees": _load_yaml(directory / "fees.yaml"),
    }


def baseline_from_file(payload: dict) -> BaselineWeightConfig:
    weights = payload.get("weights") or {}
    return BaselineWeightConfig(
        version=payload.get("version", "baseline_weights_v1"),
        trend=float(weights.get("trend", 0.25)),
        momentum=float(weights.get("momentum", 0.20)),
        volume=float(weights.get("volume", 0.10)),
        orderflow=float(weights.get("orderflow", 0.20)),
        structure=float(weights.get("structure", 0.15)),
        volatility=float(weights.get("volatility", 0.0)),
        liquidity=float(weights.get("liquidity", 0.10)),
        min_abs_score=float(payload.get("min_abs_score", 0.45)),
        max_spread_bps=float(payload.get("max_spread_bps", 8.0)),
        min_volume_ratio=float(payload.get("min_volume_ratio", 0.40)),
        extreme_atr_normalized=float(payload.get("extreme_atr_normalized", 0.05)),
        high_atr_normalized=float(payload.get("high_atr_normalized", 0.02)),
        slope_scale=float(payload.get("slope_scale", 0.001)),
        price_vs_ema_scale=float(payload.get("price_vs_ema_scale", 0.005)),
        roc_scale=float(payload.get("roc_scale", 0.004)),
        imbalance_strong=float(payload.get("imbalance_strong", 0.25)),
        volume_z_strong=float(payload.get("volume_z_strong", 1.0)),
    )


def combination_from_file(payload: dict) -> CombinationConfig:
    block_long = payload.get("block_long_regimes") or []
    block_short = payload.get("block_short_regimes") or []
    return CombinationConfig(
        baseline_version=payload.get("baseline_version", "baseline_score_v1"),
        jev_rule_version=payload.get("jev_rule_version", "jev_veto_only_v1"),
        openai_rule_version=payload.get("openai_rule_version", "openai_jev_veto_only_v1"),
        failure_policy=payload.get("failure_policy", "NO_TRADE"),
        min_trend_continuation=float(payload.get("min_trend_continuation", 0.55)),
        max_reversal=float(payload.get("max_reversal", 0.50)),
        max_false_breakout=float(payload.get("max_false_breakout", 0.55)),
        min_pressure_edge=float(payload.get("min_pressure_edge", 0.05)),
        max_liquidity_sweep=float(payload.get("max_liquidity_sweep", 0.75)),
        baseline_weight=float(payload.get("baseline_weight", 0.65)),
        jev_weight=float(payload.get("jev_weight", 0.35)),
        max_reversal_risk=float(payload.get("max_reversal_risk", 0.65)),
        max_anomaly=float(payload.get("max_anomaly", 0.80)),
        min_openai_confidence=float(payload.get("min_openai_confidence", 0.45)),
        block_long_regimes=tuple(block_long),
        block_short_regimes=tuple(block_short),
    )


def strategies_from_file(payload: dict) -> dict[str, StrategySettings]:
    raw = payload.get("strategies") or {}
    defaults = {
        "baseline": StrategySettings("baseline", True, OperatingMode.PAPER, 2000, False),
        "baseline_jev": StrategySettings("baseline_jev", True, OperatingMode.SHADOW, 4000, True),
        "baseline_openai_jev": StrategySettings("baseline_openai_jev", True, OperatingMode.SHADOW, 8000, False),
    }
    for key, current in defaults.items():
        item = raw.get(key) or {}
        defaults[key] = StrategySettings(
            key=key,
            enabled=bool(item.get("enabled", current.enabled)),
            mode=OperatingMode(item.get("mode", current.mode.value)),
            max_signal_age_ms=int(item.get("max_signal_age_ms", current.max_signal_age_ms)),
            call_model=bool(item.get("call_model", current.call_model)),
        )
    return defaults


def risk_from_file(payload: dict, settings: Settings) -> RiskLimits:
    return RiskLimits(
        target_mode=TargetMode(payload.get("target_mode", "structure")),
        target_fallback=str(payload.get("target_fallback", "none")),
        fixed_target_pct=float(payload.get("fixed_target_pct", 0.01)),
        rr_target_multiple=float(payload.get("rr_target_multiple", 3.0)),
        atr_buffer_mult=float(payload.get("atr_buffer_mult", 0.10)),
        atr_stop_mult=float(payload.get("atr_stop_mult", 1.5)),
        max_stop_pct=float(payload.get("max_stop_pct", 0.02)),
        min_stop_pct=float(payload.get("min_stop_pct", 0.0008)),
        min_entry_interval_seconds=int(payload.get("min_entry_interval_seconds", 60)),
        allow_pyramiding=bool(payload.get("allow_pyramiding", False)),
        max_leverage=float(payload.get("max_leverage", 1)),
        apply_funding=bool(payload.get("apply_funding", True)),
        expected_hold_minutes=int(payload.get("expected_hold_minutes", 15)),
        funding_interval_minutes=int(payload.get("funding_interval_minutes", 480)),
        order_style=str(payload.get("order_style", "market")),
        allow_partial_entry=bool(payload.get("allow_partial_entry", False)),
        limit_ttl_ms=int(payload.get("limit_ttl_ms", 60000)),
        risk_per_trade=settings.default_risk_per_trade,
        max_risk_per_trade=settings.max_risk_per_trade,
        max_daily_loss=settings.max_daily_loss,
        max_daily_drawdown=settings.max_daily_drawdown,
        max_total_exposure=settings.max_total_exposure,
        max_symbol_exposure=settings.max_symbol_exposure,
        max_open_positions=settings.max_open_positions,
        min_net_rr=settings.min_net_rr,
    )


def fees_from_file(payload: dict, settings: Settings) -> FeeConfig:
    return FeeConfig(
        maker_fee_rate=float(payload.get("maker_fee_rate", settings.maker_fee_rate)),
        taker_fee_rate=float(payload.get("taker_fee_rate", settings.taker_fee_rate)),
        entry_liquidity=str(payload.get("entry_liquidity", "taker")),
        exit_liquidity=str(payload.get("exit_liquidity", "taker")),
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    database_url: str = ""
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_futures_rest_url: str = "https://fapi.binance.com"
    binance_spot_rest_url: str = "https://api.binance.com"
    binance_futures_ws_url: str = "wss://fstream.binance.com/stream"
    binance_spot_ws_url: str = "wss://stream.binance.com:9443/stream"
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    openai_prompt_version: str = "market_interpreter_v1"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_timeout_s: float = 12.0
    openai_input_usd_per_1m: float | None = None
    openai_output_usd_per_1m: float | None = None
    jev_provider: str = "auto"
    jev_api_key: str = ""
    jev_base_url: str = ""
    jev_model: str = ""
    jev_prompt_version: str = "jev_market_v1"
    jev_failure_policy: str = "NO_TRADE"
    engine_api_secret: str = ""
    environment: str = "development"
    trading_engine_enabled: bool = True
    trading_live_enabled: bool = False
    allow_real_orders: bool = False
    market_type: str = "futures"
    symbols: str = "BTCUSDT,ETHUSDT,SOLUSDT"
    snapshot_trigger: str = "1m_close"
    default_risk_per_trade: float = 0.005
    max_risk_per_trade: float = 0.01
    max_daily_loss: float = 0.03
    max_daily_drawdown: float = 0.03
    max_total_exposure: float = 1.0
    max_symbol_exposure: float = 0.40
    max_open_positions: int = 3
    min_net_rr: float = 3.0
    initial_paper_equity: float = 10_000
    maker_fee_rate: float = 0.0002
    taker_fee_rate: float = 0.0005
    slippage_model: str = SlippageModelName.ORDERBOOK_BASED.value
    fixed_slippage_bps: float = 1.0
    stale_after_ms: int = 5000
    max_signal_age_ms: int = 8000
    paper_seed: int = 1
    log_level: str = "INFO"
    log_json: bool = False
    host: str = "0.0.0.0"
    port: int = Field(default=8000, validation_alias="PORT")
    web_origin: str = "http://localhost:3000"
    evolution_engine_enabled: bool = True
    auto_research: bool = True
    auto_build: bool = False
    auto_shadow_promotion: bool = False
    auto_paper_promotion: bool = False
    max_daily_experiments: int = 3
    min_experiment_sample_size: int = 100
    coding_agent_provider: str = "mock"
    human_approval_required: bool = True
    openai_research_model: str = ""
    openai_research_prompt_version: str = "evolution_researcher_v1"

    @property
    def symbol_list(self) -> list[str]:
        return [item.strip().upper() for item in self.symbols.split(",") if item.strip()]

    @property
    def live_armed(self) -> bool:
        return bool(self.trading_live_enabled and self.allow_real_orders)


_cached: Settings | None = None


def get_settings() -> Settings:
    global _cached
    if _cached is None:
        _cached = Settings()
    return _cached


def reset_settings() -> None:
    global _cached
    _cached = None
