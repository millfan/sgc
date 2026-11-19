"""Typed configuration loaded from a TOML file (config/default.toml by default)."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "default.toml"


class RunConfig(BaseModel):
    portfolio_value: float = 100_000.0
    rebalance_frequency: Literal["monthly", "weekly"] = "monthly"
    journal_dir: str = "runs"


class DataConfig(BaseModel):
    provider: Literal["yfinance", "synthetic"] = "yfinance"
    benchmark: str = "SPY"
    lookback_days: int = 1100
    tickers: list[str] = Field(default_factory=list)


class UniverseConfig(BaseModel):
    min_price: float = 5.0
    min_avg_dollar_volume: float = 20_000_000.0
    min_market_cap: float = 5_000_000_000.0
    min_history_days: int = 252
    max_missing_fraction: float = 0.05
    max_abs_daily_return: float = 0.5


class AlphaConfig(BaseModel):
    short_list_size: int = 12
    forecast_horizon_days: int = 21
    min_train_dates: int = 24
    embargo_days: int = 21
    min_oos_ic: float = 0.0


class PortfolioConfig(BaseModel):
    max_positions: int = 10
    tilt_step: float = 0.15
    max_tilt_units: int = 2
    self_consistency_samples: int = 3
    risk_off_gross: float = 0.8
    risk_on_gross: float = 1.0


class DebateConfig(BaseModel):
    trim_fraction: float = 0.3
    add_fraction: float = 0.2
    max_change: float = 0.05


class RiskLimits(BaseModel):
    """Hard limits. Agents read them but can never change them."""

    model_config = {"frozen": True}

    max_weight: float = 0.15
    max_sector: float = 0.35
    min_holdings: int = 5
    max_volatility: float = 0.25
    max_beta: float = 1.3
    max_drawdown: float = 0.30
    max_adv_fraction: float = 0.05
    risk_off_max_gross: float = 0.8
    max_retries: int = 1


class ExecutionConfig(BaseModel):
    commission_per_share: float = 0.005
    half_spread_bps: float = 2.0
    impact_coefficient: float = 0.1
    no_trade_band: float = 0.005
    max_turnover: float = 0.5
    max_cost_bps: float = 50.0


class RoleOverride(BaseModel):
    model: str | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None


class LLMConfig(BaseModel):
    provider: Literal["anthropic", "offline"] = "anthropic"
    max_calls_per_run: int = 400
    use_refusal_fallbacks: bool = True
    roles: dict[str, RoleOverride] = Field(default_factory=dict)


class Settings(BaseModel):
    run: RunConfig = Field(default_factory=RunConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    alpha: AlphaConfig = Field(default_factory=AlphaConfig)
    portfolio: PortfolioConfig = Field(default_factory=PortfolioConfig)
    debate: DebateConfig = Field(default_factory=DebateConfig)
    risk: RiskLimits = Field(default_factory=RiskLimits)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)

    @property
    def database_url(self) -> str | None:
        return os.environ.get("DATABASE_URL") or None


def load_settings(path: str | Path | None = None) -> Settings:
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    with cfg_path.open("rb") as fh:
        raw = tomllib.load(fh)
    return Settings.model_validate(raw)
