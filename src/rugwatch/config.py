"""Typed loader for config.yaml. Unknown keys are an error (HANDOFF section 1)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------- collector
class PumpPortalCfg(_M):
    url: str
    streams: list[str]
    max_connections: Literal[1]
    reconnect_backoff_s: list[float]


class RpcCfg(_M):
    primary: Literal["helius"]
    fallback: Literal["alchemy"]
    max_rps: float
    batch_accounts: int
    use_websocket: Literal[False]


class BudgetCfg(_M):
    monthly_credits: int
    target_credits: int
    hard_stop_credits: int
    throttle_order: list[
        Literal["balance_cadence_x2_after_3600s", "random_rest_per_day_50", "tracked_cap_minus_20pct"]
    ]
    never_throttle: list[str]


class CollectorCfg(_M):
    timezone: Literal["UTC"]
    commitment: Literal["processed", "confirmed", "finalized"]
    pumpportal: PumpPortalCfg
    rpc: RpcCfg
    budget: BudgetCfg


class ActivityFilterCfg(_M):
    metric: Literal["real_sol_reserves"]
    tick_s: list[int]
    decide_at_s: int
    threshold_sol: float
    threshold_grid_sol: list[float]
    floor_sol: float
    freeze_after_day: int


# ----------------------------------------------------------------- tracking
class SnapshotBatchCfg(_M):
    start_at_s: int
    deadline_s: float
    parts: list[str]


class EarlyTradesCfg(_M):
    n: int
    max_signature_pages: int


class CreatorHistoryCfg(_M):
    max_signature_pages: int
    fetch_oldest_tx: bool
    cache_ttl_days: float


class DeadRuleCfg(_M):
    price_below_frac_of_peak: float
    no_reserve_change_s: float


class GraduationCfg(_M):
    m1_m2: Literal["censor_at_graduation"]
    m3: Literal["poll_pumpswap_pool"]


class TrackingCfg(_M):
    filter_pass_cap_per_day: int
    random_rest_per_day: int
    tracked_cap_per_day: int
    sampling: Literal["hash"]
    snapshot_batch: SnapshotBatchCfg
    early_trades: EarlyTradesCfg
    early_buyers_tracked: int
    creator_history: CreatorHistoryCfg
    insider_funding_link: bool
    price_cadence: list[tuple[int, int]]
    balance_cadence: list[tuple[int, int]]
    largest_holders_at_s: list[int]
    dead_rule: DeadRuleCfg
    graduation: GraduationCfg


class ScannersCfg(_M):
    enabled: bool
    list_: list[Literal["rugcheck", "goplus", "solanatracker"]] = Field(alias="list")
    benchmark_tokens_per_day: int
    eligible: Literal["filter_pass"]
    query_at_s: int
    timeout_s: float
    one_shot: Literal[True]
    score_direction: Literal["higher_is_riskier"]


# ------------------------------------------------------------------- labels
class CollapseCfg(_M):
    cross_frac_of_p0: float
    sustain_frac_of_p0: float
    sustain_s: int


class RunupCfg(_M):
    cross_frac_of_p0: float


class EventsCfg(_M):
    collapse: CollapseCfg
    runup: RunupCfg
    first_event_values: list[str]


class InsiderSetCfg(_M):
    include_creator: bool
    slot_window_after_creation: int
    direct_transfer_from_creator: bool
    funded_by_creator_or_funder: bool


class HardRugCfg(_M):
    insider_min_share_of_supply: float
    sold_frac_in_window: float
    window_s: int
    price_drop_frac: float
    drop_within_s: int
    no_recovery_above_frac: float
    no_recovery_s: int


class SoftRugCfg(_M):
    insider_min_share_of_supply: float
    sold_frac_over_horizon: float
    min_separate_sell_polls: int
    final_price_below_peak_frac: float


class DiedOrganicCfg(_M):
    final_price_below_peak_frac: float
    or_no_reserve_change_s: int


class SurvivedCfg(_M):
    final_price_min_frac_of_p0: float
    reserve_change_within_s: int


class ClassesCfg(_M):
    hard_rug: HardRugCfg
    soft_rug: SoftRugCfg
    died_organic: DiedOrganicCfg
    survived: SurvivedCfg
    fallback: Literal["indeterminate"]


class SensitivityGridCfg(_M):
    price_drop_frac: list[float]
    window_s: list[int]
    insider_min_share_of_supply: list[float]
    sold_frac: list[float]
    horizon_s: list[int]
    claim_holds_if_model_leads_in_frac_of_cells: float


class AuditCfg(_M):
    m1_sample: int
    m5_sample: int
    pass_overall: float
    pass_per_class: float
    max_adjustment_rounds: int


class LabelsCfg(_M):
    horizon_class_s: int
    horizon_event_s: int
    events: EventsCfg
    insider_set: InsiderSetCfg
    classes: ClassesCfg
    transfer_vs_sale: Literal["reserves_moved_same_poll"]
    sensitivity_grid: SensitivityGridCfg
    audit: AuditCfg


# ------------------------------------------------------------------- models
class HazardParamsCfg(_M):
    num_leaves: int
    learning_rate: float
    n_estimators: int
    min_child_samples: int


class HazardCfg(_M):
    library: Literal["lightgbm"]
    interval_s: int
    n_intervals: int
    objective: Literal["multiclass"]
    covariates_v1: str
    covariates_v2: str
    params: HazardParamsCfg
    early_stopping: Literal["inner_time_split_of_train_only"]


class CalibrationCfg(_M):
    method: Literal["isotonic"]
    fit_on: Literal["last_train_week"]


class EvLowerCfg(_M):
    method: Literal["day_block_bootstrap_models"]
    n_models: int
    quantile: float


class TradeModelCfg(_M):
    target: Literal["first_barrier"]
    ev: str
    ev_lower: EvLowerCfg


class HeadlineCfg(_M):
    target: Literal["first_event_60_is_collapse"]
    score: Literal["cumulative_incidence_collapse_3600s"]
    paired_tokens_only: Literal[True]
    best_scanner: Literal["max_over_scanners_on_test"]


class ModelsCfg(_M):
    split: Literal["walk_forward_by_launch_week"]
    min_train_weeks: int
    train_window: Literal["expanding"]
    purge: Literal["drop_train_if_label_complete_at_ge_test_start"]
    hazard: HazardCfg
    calibration: CalibrationCfg
    trade: TradeModelCfg
    baselines: list[str]
    headline: HeadlineCfg


class BootstrapCfg(_M):
    n: int
    cluster: Literal["utc_day"]
    ci: float


class LockboxCfg(_M):
    launch_weeks: list[int]
    evaluate_once_in: Literal["M5"]


class EvaluationCfg(_M):
    metrics: list[str]
    precision_at_recall: list[float]
    ece_bins: int
    bootstrap: BootstrapCfg
    forbidden_headline: Literal["accuracy"]
    lockbox: LockboxCfg
    feature_freeze_end_of_week: int


# -------------------------------------------------------- costs / strategy
class StressCfg(_M):
    fee_mult: float
    priority_mult: float
    extra_delay_ticks: int


class CostsCfg(_M):
    fee_bps_per_side: float
    fee_source: Literal["per_token_if_observed_else_default"]
    slippage: Literal["constant_product_on_virtual_reserves_at_fill_tick"]
    priority_fee_sol_per_tx: float
    fill_delay_ticks: int
    entry_fail_prob: float
    exit_extra_tick_prob: float
    rng: Literal["sha256_mint_seed"]
    stress: StressCfg


class HazardExitCfg(_M):
    enabled: bool
    window_s: int
    threshold: float


class StrategyCfg(_M):
    candidates: Literal["filter_pass_tracked_with_snapshot_complete"]
    entry_rule: Literal["ev_lower_gt_0"]
    target_frac: float
    stop_frac: float
    timeout_s: int
    size_sol: float
    max_frac_of_real_sol: float
    min_size_sol: float
    max_open_positions: int
    daily_stop_consecutive_losses: int
    hazard_exit: HazardExitCfg
    on_graduation_during_hold: Literal["exit_at_last_curve_price_flagged"]
    rules_frozen: Literal["before_each_forward_week"]


class MeanWithoutTopCfg(_M):
    drop: float
    gt: float


class KillSwitchCfg(_M):
    below_paper_ci_after_trades: int
    bankroll_drawdown: float


class GoNoGoCfg(_M):
    min_forward_weeks: int
    min_trades: int
    mean_net_return_gt: float
    ci95_lower_gt: float
    mean_without_top_frac: MeanWithoutTopCfg
    min_positive_weeks: int
    stress_mean_net_return_gt: float
    earliest_decision_week: int
    real_money: Literal["requires_explicit_user_go"]
    kill_switch: KillSwitchCfg


class AcceptCfg(_M):
    delta_pr_auc_min: float
    prob_delta_gt_0_min: float
    ece_worsening_max: float


class ReviewLoopCfg(_M):
    active_weeks: tuple[int, int]
    max_proposals_per_week: int
    preregister_file: str
    accept: AcceptCfg
    retry_rejected_after_weeks: int
    misses_shown: int


class PaidDataCfg(_M):
    earliest_week: int
    monthly_cap_usd: float
    requires_user_approval: Literal[True]
    helius_developer_usd: float
    solanatracker_advanced_eur: float


class StorageCfg(_M):
    raw: str
    parsed: str
    state: str
    backup: str


class Config(_M):
    schema_version: Literal[1]
    universe_version: int
    label_version: int
    seed: int
    collector: CollectorCfg
    activity_filter: ActivityFilterCfg
    tracking: TrackingCfg
    scanners: ScannersCfg
    labels: LabelsCfg
    models: ModelsCfg
    evaluation: EvaluationCfg
    costs: CostsCfg
    strategy: StrategyCfg
    go_no_go: GoNoGoCfg
    review_loop: ReviewLoopCfg
    paid_data: PaidDataCfg
    storage: StorageCfg


def load_config(path: Path | str | None = None) -> Config:
    p = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    with open(p, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return Config.model_validate(raw)
