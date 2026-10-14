"""Forward paper comparison. This module has no live execution adapter."""
from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import datetime
from uuid import uuid4

from app.accounts import AccountBook
from app.domain.enums import Action, OperatingMode, OrderType
from app.domain.mathutil import utcnow
from app.execution.paper import PaperExecutionProvider, OrderIntent, position_exit
from app.execution.slippage import simulate_fill
from app.providers.jev.schemas import JevShadowDeferred
from app.risk.engine import RiskContext, RiskEngine
from app.strategies.baseline_score import score_baseline
from app.strategies.runners import BaselineJevStrategy, StrategyContext

VERSION = "policy_paper_v1"
LABELS = {"control": "Política atual", "historical_break": "Rompimento histórico",
          "net_rr_1": "R:R líquido 1,0", "structural_stop": "Stop estrutura / ATR",
          "combined": "Histórico + R:R 1,0 + estrutura"}


class SharedAssessment:
    """Reuse only an identical request within one paired observation."""
    def __init__(self, provider, scheduler):
        self.provider, self.scheduler = provider, scheduler
        self.cache = {}
        self.calls = self.reused = 0

    async def evaluate_market_state(self, request):
        key = json.dumps(request.model_dump(mode="json", exclude={"evaluation_timeout_s"}), sort_keys=True)
        if key in self.cache:
            self.reused += 1
            return self.cache[key]
        result = await self.scheduler.research_assessment(self.provider, request)
        self.calls += 1
        self.cache[key] = result
        return result


class PolicyPaperLab:
    def __init__(self, slippage, now, initial_equity=20.0):
        self.book = AccountBook(list(LABELS), initial_equity, now)
        self.slippage = slippage
        self.initial_equity = initial_equity
        self.last_sample_at = None
        self.sample_count = self.provider_calls = self.reused_calls = self.deferred = 0
        self.recent = []
        self.costs = {}
        self.coverage = {}
        self.trade_coverage = {}
        self.stats = {key: {"decisions": 0, "accepted": 0, "closed": 0, "wins": 0,
                           "net_pnl": 0.0, "fees": 0.0, "interest": 0.0, "rejects": {},
                           "verified_closed": 0, "verified_wins": 0}
                      for key in LABELS}
        self.dirty = False
        self.running = False

    def due(self, snapshot, weights):
        now = utcnow()
        return (not self.running and (self.last_sample_at is None
                or (now-self.last_sample_at).total_seconds() >= 120)
                and score_baseline(snapshot, weights).action is not Action.NO_TRADE)

    @staticmethod
    def variants(limits):
        return {"control": limits,
                "historical_break": replace(limits, jev_expectancy_gate=False),
                "net_rr_1": replace(limits, min_net_rr=1.0),
                "structural_stop": replace(limits, min_stop_pct=0.0),
                "combined": replace(limits, jev_expectancy_gate=False, min_net_rr=1.0, min_stop_pct=0.0)}

    async def sample(self, snapshot, weights, combination, limits, fees, intelligence,
                     provider, scheduler, quote, rules):
        self.running = True
        shared = SharedAssessment(provider, scheduler)

        async def operation():
            self.last_sample_at = utcnow()
            self.sample_count += 1
            for policy, policy_limits in self.variants(limits).items():
                context = StrategyContext(correlation_id=uuid4(), opportunity_id=uuid4(),
                    mode=OperatingMode.PAPER, now=snapshot.timestamp, weights=weights,
                    combination=replace(combination, review_below_threshold=False),
                    call_model=True, jev=shared, risk=policy_limits,
                    round_trip_fee=fees.taker*2, intelligence=intelligence)
                decision = await BaselineJevStrategy().evaluate(snapshot, snapshot.features, context)
                decision = decision.model_copy(update={"strategy": policy, "mode": OperatingMode.PAPER})
                reasons = list(decision.reason_codes)
                accepted = False
                risk_reasons = []
                if decision.action is not Action.NO_TRADE:
                    accepted, risk_reasons = self.enter(policy, decision, snapshot, policy_limits, fees, quote(), rules)
                    reasons += risk_reasons
                row = {"sample": self.sample_count, "policy": policy, "symbol": snapshot.symbol,
                    "baseline_version": weights.version, "prompt_version": decision.prompt_version,
                    "snapshot_id": str(snapshot.snapshot_id), "decision_id": str(decision.decision_id),
                    "snapshot_at": snapshot.timestamp.isoformat(), "assessed_at": utcnow().isoformat(),
                    "action": decision.action.value, "accepted": accepted, "reason_codes": reasons,
                    "probability": decision.metadata.get("jev_continuation"),
                    "reversal_probability": decision.metadata.get("jev_reversal"),
                    "false_breakout_probability": decision.metadata.get("jev_false_breakout"),
                    "plan": decision.metadata.get("jev_trade_plan"),
                    "observations": {key:snapshot.features.get(key) for key in (
                        "ema_alignment","return_5m","return_15m","return_60m","return_240m",
                        "context_15m_slope","volume_ratio","taker_flow_1m","taker_flow_5m",
                        "range_60m","range_240m","atr")},
                    "is_mock": decision.metadata.get("jev_is_mock")}
                self.recent.append(row)
                self.recent = self.recent[-100:]
                stat = self.stats[policy]
                stat["decisions"] += 1
                stat["accepted"] += accepted
                if not accepted:
                    rejection_reasons = risk_reasons or [r for r in reasons if r.startswith(
                        ("JEV_","MARKET_RANGE_","TRADE_PLAN_","TARGET_PLAN_","BASELINE_NO_TRADE",
                         "EXPIRED_","INTELLIGENCE_","STRATEGY_ERROR"))]
                    for reason in rejection_reasons:
                        stat["rejects"][reason] = stat["rejects"].get(reason, 0)+1
                self.dirty = True
        try:
            await scheduler.research_batch(operation)
        except JevShadowDeferred:
            self.deferred += 1
            self.last_sample_at = utcnow()
            self.dirty = True
        finally:
            self.provider_calls += shared.calls
            self.reused_calls += shared.reused
            self.running = False

    def enter(self, policy, decision, source, limits, fees, quote, rules):
        now = utcnow()
        if (quote is None or quote["bid"] is None or quote["ask"] is None
                or quote["book_at"] is None or (now-quote["book_at"]).total_seconds() > 5
                or (now-source.timestamp).total_seconds() > 30):
            return False, ["PAPER_STALE_EXECUTION_QUOTE"]
        snapshot = source.model_copy(update={"timestamp": now, "best_bid": quote["bid"],
            "best_ask": quote["ask"], "price": (quote["bid"]+quote["ask"])/2})
        marks = quote["marks"]
        account = self.book.accounts[policy]
        self.book.roll_day(policy, now, marks)
        losses = 0
        for trade in reversed(account.trades):
            if trade.closed_at.date() != now.date() or trade.net_pnl >= 0:
                break
            losses += 1
        context = RiskContext(equity=account.equity(marks), cash=account.cash,
            day_start_equity=account.day_start_equity, realized_pnl_today=account.realized_pnl_today,
            open_positions=len(account.positions), symbol_exposure_notional=account.symbol_exposure(snapshot.symbol, marks),
            total_exposure_notional=account.exposure(marks),
            has_position_on_symbol=any(p.symbol==snapshot.symbol for p in account.positions.values()),
            last_entry_at=account.last_entry_by_symbol.get(snapshot.symbol), now=now,
            # Pure risk calculation applies the production guards to virtual
            # balances. Execution below is always PaperExecutionProvider.
            trading_enabled=True, persistence_ok=True, live_requested=True, live_armed=True,
            rules_required=True,
            market_type=snapshot.market_type.value, step_size=rules.get("step_size"),
            min_notional=rules.get("min_notional"), quote_free=account.cash,
            last_stop_at=account.last_stop_at.get(snapshot.symbol), consecutive_live_losses=losses,
            open_stop_risk=sum(p.initial_net_risk for p in account.positions.values()))
        risk_engine = RiskEngine(limits, self.slippage)
        risk = risk_engine.evaluate(decision, snapshot, context, fees, quote["book"])
        if not risk.accepted:
            return False, risk.reject_reasons
        econ = risk.economics
        intent = OrderIntent(decision_id=decision.decision_id, risk_id=risk.risk_id,
            strategy=policy, symbol=snapshot.symbol, side="BUY" if decision.action is Action.LONG else "SELL",
            order_type=OrderType.MARKET, quantity=econ.quantity, limit_price=None, mode="paper",
            created_at=now, best_bid=quote["bid"], best_ask=quote["ask"], book=quote["book"], fee_rate=fees.taker)
        _, fills = PaperExecutionProvider().submit_market(intent,self.slippage)
        if not fills:
            return False, ["PAPER_NO_FILL"]
        fill = fills[0]
        position = self.book.open_position(strategy=policy,symbol=snapshot.symbol,side=decision.action,
            quantity=fill.quantity,entry_price=fill.price,stop=econ.stop,target=econ.target,
            opened_at=now,decision_id=decision.decision_id,entry_fee=fill.fee,
            initial_net_risk=econ.net_risk,margin=fill.price*fill.quantity/max(limits.max_leverage,1),
            exit_fee_rate=fees.taker,fee_source=fees.source,mode="paper")
        plan = decision.metadata["jev_trade_plan"]
        position.forecast_horizon_minutes = int(plan["horizon_minutes"])
        position.max_hold_minutes = int(plan.get("max_hold_minutes") or plan["horizon_minutes"])
        self.costs[str(position.position_id)] = (max(plan.get("borrow_hourly_rate") or 0,plan.get("borrow_stress_rate") or 0)
                                                 if snapshot.market_type.value == "margin" else 0)
        self.stats[policy]["fees"] += fill.fee
        self.coverage[str(position.position_id)] = {"last_observed_at":now.isoformat(),
            "complete":True,"max_gap_seconds":0.0}
        return True, []

    def observe(self, symbol, quote):
        if not quote or not quote["bid"] or not quote["ask"]:
            return
        now = utcnow()
        if quote["book_at"] is None or (now-quote["book_at"]).total_seconds() > 5:
            return
        for policy, account in self.book.accounts.items():
            for identity, p in list(account.positions.items()):
                if p.symbol != symbol:
                    continue
                coverage = self.coverage.setdefault(identity, {"last_observed_at":p.opened_at.isoformat(),
                    "complete":False,"max_gap_seconds":0.0})
                gap = (now-datetime.fromisoformat(coverage["last_observed_at"])).total_seconds()
                coverage["max_gap_seconds"] = max(coverage["max_gap_seconds"],gap)
                if gap > 90 or gap < 0:
                    coverage["complete"] = False
                coverage["last_observed_at"] = now.isoformat()
                hold = (now-p.opened_at).total_seconds()/60
                reason = position_exit(p.side,quote["bid"],quote["ask"],p.stop,p.target,hold,p.max_hold_minutes or 60)
                if reason is None:
                    continue
                preview = simulate_fill(side="SELL" if p.side is Action.LONG else "BUY",quantity=p.quantity,
                    best_bid=quote["bid"],best_ask=quote["ask"],book=quote["book"],config=self.slippage)
                if preview.filled_quantity <= 0:
                    continue
                fee = preview.estimated_fill_price*preview.filled_quantity*p.exit_fee_rate
                interest = p.entry_price*preview.filled_quantity*self.costs.get(identity,0)*max(1,math.ceil(hold/60))
                trade = self.book.close_position(strategy=policy,position_id=identity,
                    exit_price=preview.estimated_fill_price,closed_at=now,exit_fee=fee,
                    slippage=abs(preview.estimated_fill_price-preview.expected_price)*preview.filled_quantity,
                    funding=-interest,exit_reason=reason,quantitative_regime=None,market_regime=None,
                    quantity=preview.filled_quantity)
                s = self.stats[policy]
                s["closed"] += 1
                s["wins"] += trade.net_pnl > 0
                self.trade_coverage[str(trade.trade_id)] = dict(coverage)
                if coverage["complete"]:
                    s['verified_closed'] += 1
                    s['verified_wins'] += trade.net_pnl > 0
                s["net_pnl"] += trade.net_pnl
                s["fees"] += fee
                s["interest"] += interest
                if identity not in account.positions:
                    self.costs.pop(identity,None)
                    self.coverage.pop(identity,None)
                account.trades = account.trades[-200:]
                account.equity_points = account.equity_points[-300:]
                retained = {str(t.trade_id) for a in self.book.accounts.values() for t in a.trades}
                self.trade_coverage = {key:value for key,value in self.trade_coverage.items() if key in retained}
                self.dirty = True

    def export(self):
        return {"version": VERSION,"accounts": self.book.export_state(),"stats": self.stats,
                "costs": self.costs,"recent": self.recent,"sample_count": self.sample_count,
                "provider_calls": self.provider_calls,"reused_calls": self.reused_calls,
                "deferred": self.deferred,
                "coverage":self.coverage,"trade_coverage":self.trade_coverage,
                "last_sample_at": self.last_sample_at.isoformat() if self.last_sample_at else None}

    def restore(self, payload):
        if not payload or payload.get("version") != VERSION:
            return
        self.book.restore_state(payload.get("accounts") or {})
        self.stats = payload.get("stats") or self.stats
        for stat in self.stats.values():
            stat.setdefault('verified_closed',0)
            stat.setdefault('verified_wins',0)
        self.costs = payload.get("costs") or {}
        self.coverage = payload.get("coverage") or {}
        self.trade_coverage = payload.get("trade_coverage") or {}
        self.recent = payload.get("recent") or []
        self.sample_count = payload.get("sample_count",0)
        self.provider_calls = payload.get("provider_calls",0)
        self.reused_calls = payload.get("reused_calls",0)
        self.deferred = payload.get("deferred",0)
        self.last_sample_at = datetime.fromisoformat(payload["last_sample_at"]) if payload.get("last_sample_at") else None

    def report(self, marks):
        closed = sorted((trade for account in self.book.accounts.values() for trade in account.trades),
                        key=lambda trade:trade.closed_at,reverse=True)[:100]
        return {"version": VERSION,"mode": "PAPER_ONLY","initial_equity": self.initial_equity,
            "sample_count": self.sample_count,"provider_calls": self.provider_calls,
            "reused_identical_assessments": self.reused_calls,"deferred_samples": self.deferred,
            "running": self.running,"last_sample_at": self.last_sample_at.isoformat() if self.last_sample_at else None,
            "policies": [{"id": key,"label": LABELS[key],**self.stats[key],
                "equity": account.equity(marks),"open_positions": len(account.positions),
                "unverified_closed":self.stats[key]['closed']-self.stats[key]['verified_closed'],
                "verified_win_rate":(self.stats[key]['verified_wins']/self.stats[key]['verified_closed']
                                     if self.stats[key]['verified_closed'] else None),
                "win_rate": self.stats[key]["wins"]/self.stats[key]["closed"] if self.stats[key]["closed"] else None}
                for key,account in self.book.accounts.items()],"recent": list(reversed(self.recent)),
            "open_positions": [p.model_dump(mode="json") for a in self.book.accounts.values() for p in a.positions.values()],
            "closed_records": [{**trade.model_dump(mode="json"),
                "observation_coverage":self.trade_coverage.get(str(trade.trade_id),{"complete":False})} for trade in closed],
            "limitations": ["Forward simulation, not live realized PnL or a profitability guarantee.",
                "Order book fills estimate fees and slippage; margin interest uses a conservative stress rate.",
                "Partial closures count as separate records; paired coverage and rejections are reported.",
                "A gap over 90 seconds invalidates observed win-rate coverage; its PnL remains in financial totals.",
                "Legacy records without observation tracking are excluded from verified win rate."]}
