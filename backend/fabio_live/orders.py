"""Order placement, trims, and exits via Moomoo OpenAPI."""

from __future__ import annotations

import datetime
import time

import pandas as pd
from moomoo import OrderStatus, OrderType, TimeInForce, TrdSide
try:
    from moomoo import ModifyOrderOp
except Exception:  # pragma: no cover - depends on installed moomoo version
    try:
        from moomoo.common.constant import ModifyOrderOp
    except Exception:
        ModifyOrderOp = None

from fabio_live.constants import (
    ENTRY_FILL_WAIT_SEC,
    ENTRY_MAX_ATTEMPTS,
    OPTIONS_ONLY_EXECUTION,
    RESEARCH_RISK_CAP_MULTIPLIER,
    STRATEGY_CAPITAL,
    TRIM_MULTIPLE,
    TRIM_PCT,
)


def build_option_code(symbol: str, expiry: str, direction: str, strike: float) -> str:
    """Moomoo-style OCC code: US.IBM261016C00250000. Never an equity share code."""
    sym = str(symbol or "").strip().upper()
    exp = str(expiry or "").strip().replace("-", "")
    if len(exp) == 8:
        exp = exp[2:]
    right = "P" if str(direction).upper() == "PUT" else "C"
    strike_int = int(round(float(strike) * 1000))
    return f"US.{sym}{exp}{right}{strike_int:08d}"


class OrderManager:
    """
    Moomoo OpenD implementation of ``fabio_live.execution.ExecutionPort``.

    Entry : limit at ask price; retries at fresh ask every 60 s until fully
            filled or ENTRY_MAX_ATTEMPTS exhausted.
    Trim  : market sell of TRIM_PCT of remaining each time option doubles (ORBIT).
    Exit  : market sell of full remaining position.
    Tracks entry price and rolling P&L for circuit-breaker reporting.

    Default ORB wiring stays here. Tradier paper ``place_order`` is a separate
    client (``brokers.tradier``) and must not share ``self.positions``.
    """

    def __init__(self, trade_ctx, quote_ctx, trd_env):
        self.ctx = trade_ctx
        self.quote_ctx = quote_ctx
        self.trd_env = trd_env
        self.positions = {}

    @staticmethod
    def _looks_like_option_code(code: str) -> bool:
        # Moomoo option symbols are long instrument codes, e.g. US.SPY250509C00530000.
        # A plain stock symbol looks like US.SPY and should never be bought by this bot.
        if not isinstance(code, str):
            return False
        if not code.startswith("US."):
            return False
        if len(code) <= 12:
            return False
        return ("C" in code or "P" in code) and any(ch.isdigit() for ch in code)

    def _option_code(self, symbol: str, direction: str, price: float) -> str | None:
        code = f"US.{symbol}"

        ret, dates = self.quote_ctx.get_option_expiration_date(code)
        if ret != 0 or dates.empty:
            print(f"   ✗ Could not fetch expiry dates for {symbol}: {dates}")
            return None

        date_col = next(
            (
                c
                for c in dates.columns
                if any(k in c.lower() for k in ["time", "date", "expir"])
            ),
            None,
        )
        if date_col is None:
            print(f"   ✗ Unexpected expiry columns: {list(dates.columns)}")
            return None

        today = datetime.date.today()
        exp_dates = sorted(pd.to_datetime(dates[date_col]).dt.date.tolist())
        future_dates = [d for d in exp_dates if d >= today]
        if not future_dates:
            print(f"   ✗ No future expiry dates found for {symbol}")
            return None
        nearest = future_dates[0]
        nearest_str = nearest.strftime("%Y-%m-%d")

        opt_str = "PUT" if direction == "PUT" else "CALL"
        ret, chain = self.quote_ctx.get_option_chain(
            code,
            index_option_type="NORMAL",
            start=nearest_str,
            end=nearest_str,
            option_type=opt_str,
        )
        if ret != 0 or chain.empty:
            print(f"   ✗ Option chain empty for {symbol} {direction} {nearest_str}: {chain}")
            return None

        chain = chain.copy()
        chain["diff"] = (chain["strike_price"] - price).abs()
        atm = chain.loc[chain["diff"].idxmin()]
        opt_code = atm["code"]
        print(f"   Option: {opt_code} | Strike={atm['strike_price']} | Expiry={nearest_str}")
        return opt_code

    def _get_ask_price(self, opt_code: str, fallback: float) -> float:
        ret, data = self.quote_ctx.get_market_snapshot([opt_code])
        if ret != 0 or data.empty:
            print(f"   ⚠  Ask fetch failed — using fallback ${fallback:.2f}")
            return fallback
        ask = float(data["ask_price"].iloc[0])
        return ask if ask > 0 else fallback

    def _get_last_price(self, opt_code: str) -> float:
        ret, data = self.quote_ctx.get_market_snapshot([opt_code])
        if ret != 0 or data.empty:
            return 0.0
        bid = float(data["bid_price"].iloc[0])
        last = float(data["last_price"].iloc[0])
        return bid if bid > 0 else last

    @staticmethod
    def _is_working_status(status) -> bool:
        name = str(getattr(status, "name", status) or "").upper()
        return name in {
            "UNSUBMITTED",
            "WAITING_SUBMIT",
            "SUBMITTING",
            "SUBMITTED",
            "FILLED_PART",
        }

    def _cancel(self, order_id: str, label: str = "") -> bool:
        """Cancel via Moomoo modify_order(CANCEL); OpenSecTradeContext has no cancel_order."""
        ret = -1
        err: Exception | None = None
        if not order_id:
            err = ValueError("empty order_id")
        elif ModifyOrderOp is None:
            err = RuntimeError("ModifyOrderOp unavailable — upgrade moomoo or check install")
        elif not hasattr(self.ctx, "modify_order"):
            err = RuntimeError("trade context has no modify_order")
        else:
            try:
                # Matches moomoo examples: modify_order(ModifyOrderOp.CANCEL, order_id, 0, 0)
                ret, _ = self.ctx.modify_order(
                    ModifyOrderOp.CANCEL,
                    str(order_id),
                    0,
                    0,
                    trd_env=self.trd_env,
                )
            except Exception as e:
                err = e
        tag = f" ({label})" if label else ""
        ok = ret == 0
        if ok:
            print(f"   ✓ Cancel{tag}: {order_id}")
            return True
        if err is not None:
            print(f"   ✗ Cancel{tag} failed: {order_id} | {err}")
        else:
            print(f"   ✗ Cancel{tag} failed: {order_id}")
        return False

    def _sweep_working_entry_orders(self, code: str) -> tuple[int, int]:
        """
        Best-effort cleanup for leftover working BUY entries on the same option code.
        Bounded to one order-list query + targeted cancel calls.
        """
        if not code:
            return 0, 0
        try:
            ret, od = self.ctx.order_list_query(trd_env=self.trd_env)
        except TypeError:
            ret, od = self.ctx.order_list_query(order_id="", trd_env=self.trd_env)
        except Exception as e:
            print(f"   ⚠  Working-order sweep query failed for {code}: {e}")
            return 0, 0
        if ret != 0 or od is None or od.empty:
            return 0, 0

        sweep = od.copy()
        if "code" in sweep.columns:
            sweep = sweep[sweep["code"].astype(str) == str(code)]
        if "trd_side" in sweep.columns:
            sweep = sweep[sweep["trd_side"].astype(str).str.upper() == str(TrdSide.BUY).upper()]
        if "order_status" in sweep.columns:
            sweep = sweep[sweep["order_status"].apply(self._is_working_status)]
        if sweep.empty or "order_id" not in sweep.columns:
            return 0, 0

        cancelled = 0
        for oid in sweep["order_id"].astype(str).tolist():
            if self._cancel(oid, "post-entry-sweep"):
                cancelled += 1
        return len(sweep), cancelled

    def _sell(self, code: str, qty: int, label: str = "") -> bool:
        ret, data = self.ctx.place_order(
            price=0,
            qty=qty,
            code=code,
            trd_side=TrdSide.SELL,
            order_type=OrderType.MARKET,
            trd_env=self.trd_env,
            time_in_force=TimeInForce.DAY,
        )
        if ret == 0:
            print(f"   ✓ Sell order placed{(' — ' + label) if label else ''}")
            return True
        print(f"   ✗ Sell failed{(' — ' + label) if label else ''}: {data}")
        return False

    def enter(
        self,
        symbol: str,
        direction: str,
        price: float,
        risk_pct: float,
        portfolio_val: float,
    ):
        risk_base = min(portfolio_val, STRATEGY_CAPITAL * RESEARCH_RISK_CAP_MULTIPLIER)
        risk_dollars = risk_base * risk_pct
        opt_code = self._option_code(symbol, direction, price)
        if opt_code is None:
            print(f"   ✗ [{symbol}] Could not find valid option code — skipping.")
            return
        if OPTIONS_ONLY_EXECUTION and not self._looks_like_option_code(opt_code):
            print(
                f"   ✗ [{symbol}] Options-only safety blocked BUY for non-option code: "
                f"{opt_code}"
            )
            return
        fallback_prem = price * 0.01
        self._fill_buy(
            symbol,
            direction,
            opt_code,
            risk_dollars,
            fallback_prem,
            risk_pct=risk_pct,
        )

    def enter_option_contract(
        self,
        symbol: str,
        direction: str,
        *,
        strike: float,
        expiry: str,
        premium: float | None,
        risk_pct: float,
        portfolio_val: float,
        source: str = "mr",
    ):
        """Buy a named expiry/strike (MR paper). Does not map to ATM.

        Closes are market sell-to-close via ``_sell`` / ``exit``; never exercise.
        """
        risk_base = min(portfolio_val, STRATEGY_CAPITAL * RESEARCH_RISK_CAP_MULTIPLIER)
        risk_dollars = risk_base * risk_pct
        opt_code = build_option_code(symbol, expiry, direction, strike)
        if OPTIONS_ONLY_EXECUTION and not self._looks_like_option_code(opt_code):
            print(
                f"   ✗ [{symbol}] Options-only safety blocked BUY for non-option code: "
                f"{opt_code}"
            )
            return
        fallback_prem = float(premium) if premium and float(premium) > 0 else max(
            float(strike) * 0.01, 0.05
        )
        self._fill_buy(
            symbol,
            direction,
            opt_code,
            risk_dollars,
            fallback_prem,
            strike=float(strike),
            expiry=str(expiry),
            source=source,
            risk_pct=risk_pct,
        )

    def _fill_buy(
        self,
        symbol: str,
        direction: str,
        opt_code: str,
        risk_dollars: float,
        fallback_prem: float,
        *,
        strike: float | None = None,
        expiry: str = "",
        source: str = "",
        risk_pct: float | None = None,
    ):
        total_filled = 0
        entry_price = 0.0
        risk_tag = f" ({risk_pct*100:.2f}%)" if risk_pct is not None else ""

        print(
            f"\n → [{symbol}] ENTER {direction} | {opt_code} | "
            f"Risk=${risk_dollars:.0f}{risk_tag}"
        )

        for attempt in range(1, ENTRY_MAX_ATTEMPTS + 1):
            ask = self._get_ask_price(opt_code, fallback=fallback_prem)
            contract_cost = ask * 100
            total_needed = max(1, int(risk_dollars / contract_cost))
            remaining_qty = total_needed - total_filled

            if remaining_qty <= 0:
                print(f"   ✓ Full position achieved ({total_filled} contracts).")
                break

            print(
                f"   Attempt {attempt}/{ENTRY_MAX_ATTEMPTS} | "
                f"Ask=${ask:.2f} | Need={remaining_qty} more "
                f"(filled={total_filled}/{total_needed})"
            )

            ret, data = self.ctx.place_order(
                price=ask,
                qty=remaining_qty,
                code=opt_code,
                trd_side=TrdSide.BUY,
                order_type=OrderType.NORMAL,
                trd_env=self.trd_env,
                time_in_force=TimeInForce.DAY,
            )

            if ret != 0:
                print(f"   ✗ Order placement failed: {data}")
                break

            order_id = data["order_id"].iloc[0]
            print(
                f"   ✓ Limit order {order_id} @ ${ask:.2f} — "
                f"waiting {ENTRY_FILL_WAIT_SEC}s..."
            )
            time.sleep(ENTRY_FILL_WAIT_SEC)

            ret2, od = self.ctx.order_list_query(order_id=order_id, trd_env=self.trd_env)

            if ret2 != 0 or od.empty:
                print("   ⚠  Cannot query order — cancelling.")
                if not self._cancel(order_id, "status-unknown"):
                    print("   ⚠  Cancel failure on unknown-status order; stale working risk.")
                continue

            status = od["order_status"].iloc[0]
            dealt_qty = int(od["dealt_qty"].iloc[0])

            if dealt_qty > 0 and entry_price == 0.0:
                entry_price = ask

            total_filled += dealt_qty

            if status == OrderStatus.FILLED_ALL:
                print(f"   ✓ Fully filled: {dealt_qty} contracts @ ${ask:.2f}")
                break

            unfilled = remaining_qty - dealt_qty
            if dealt_qty > 0:
                print(
                    f"   ⚠  Partial: {dealt_qty}/{remaining_qty} filled. "
                    f"Cancelling {unfilled} — retrying remainder..."
                )
            else:
                print("   ↩  No fill — refreshing ask...")

            if not self._cancel(order_id, "partial/unfilled"):
                print("   ⚠  Cancel failure on partial/unfilled order; stale working risk.")

        if total_filled > 0:
            rec = {
                "direction": direction,
                "code": opt_code,
                "original_qty": total_filled,
                "remaining_qty": total_filled,
                "entry_option_price": entry_price,
                "trim_level": 0,
                "realized_trim_pnl": 0.0,
            }
            if strike is not None:
                rec["strike"] = float(strike)
            if expiry:
                rec["expiry"] = str(expiry)
            if source:
                rec["source"] = source
            self.positions[symbol] = rec
            print(
                f"   ✓ Position recorded: {symbol} {direction} "
                f"× {total_filled} contracts @ ${entry_price:.2f}"
            )
            targeted, cancelled = self._sweep_working_entry_orders(opt_code)
            if targeted > 0:
                print(
                    f"   {'✓' if cancelled == targeted else '⚠'} Post-entry sweep: "
                    f"cancelled {cancelled}/{targeted} working BUY order(s)"
                )
        else:
            print(f"   ✗ [{symbol}] No fill after {ENTRY_MAX_ATTEMPTS} attempts — skipped.")

    def check_profit_trim(self, symbol: str) -> dict:
        """
        Profit-taking partial sells. Returns a dict (always), never a bare float.

        Keys:
          qty_sold, pnl_leg, remaining_after, closed_fully, position_total_pnl
        When closed_fully, position is removed and position_total_pnl is realized P&L.
        """
        empty: dict = {
            "qty_sold": 0,
            "pnl_leg": 0.0,
            "remaining_after": 0,
            "closed_fully": False,
            "position_total_pnl": 0.0,
        }
        if symbol not in self.positions:
            return empty
        pos = self.positions[symbol]
        if pos["remaining_qty"] < 2:
            return empty

        current_price = self._get_last_price(pos["code"])
        if current_price <= 0:
            return empty

        target = pos["entry_option_price"] * (TRIM_MULTIPLE ** (pos["trim_level"] + 1))

        if current_price < target:
            return empty

        trim_qty = max(1, int(pos["remaining_qty"] * TRIM_PCT))
        print(
            f"\n  [{symbol}] PROFIT TRIM lvl {pos['trim_level']+1} | "
            f"Price=${current_price:.2f} ≥ target=${target:.2f} | "
            f"Selling {trim_qty}/{pos['remaining_qty']} contracts"
        )

        if not self._sell(pos["code"], trim_qty, label=f"trim-{pos['trim_level']+1}"):
            return empty

        pnl = (current_price - pos["entry_option_price"]) * trim_qty * 100
        pos["realized_trim_pnl"] += pnl
        pos["remaining_qty"] -= trim_qty
        pos["trim_level"] += 1
        rem = int(pos["remaining_qty"])
        print(f"   Trim P&L: ${pnl:+.0f} | Remaining: {rem} contracts")

        out = {
            "qty_sold": int(trim_qty),
            "pnl_leg": float(pnl),
            "remaining_after": rem,
            "closed_fully": False,
            "position_total_pnl": 0.0,
        }

        if rem <= 0:
            total = float(pos["realized_trim_pnl"])
            del self.positions[symbol]
            out["closed_fully"] = True
            out["position_total_pnl"] = total
            out["remaining_after"] = 0
            print(f"   Position flat after trim | Total P&L: ${total:+.0f}")

        return out

    def exit(self, symbol: str, reason: str = "") -> float:
        result = self.exit_result(symbol, reason)
        return float(result.get("pnl", 0.0))

    def exit_result(self, symbol: str, reason: str = "") -> dict:
        if symbol not in self.positions:
            return {
                "success": False,
                "pnl": 0.0,
                "error": "symbol_not_tracked",
                "symbol": symbol,
                "reason": reason,
            }
        pos = self.positions[symbol]

        tag = f" [{reason}]" if reason else ""
        print(
            f"\n → EXIT{tag} {pos['direction']} | {pos['code']} | "
            f"qty={pos['remaining_qty']}"
        )

        exit_price = self._get_last_price(pos["code"])
        rem = int(pos["remaining_qty"])
        trim_pnl_so_far = float(pos["realized_trim_pnl"])
        if self._sell(pos["code"], pos["remaining_qty"]):
            final_pnl = (exit_price - pos["entry_option_price"]) * rem * 100
            total_pnl = trim_pnl_so_far + final_pnl
            print(f"   Approx trade P&L: ${total_pnl:+.0f}")
            del self.positions[symbol]
            return {
                "success": True,
                "pnl": float(total_pnl),
                "pnl_final_leg": float(final_pnl),
                "pnl_from_trims": float(trim_pnl_so_far),
                "qty_final_leg": rem,
                "error": "",
                "symbol": symbol,
                "reason": reason,
            }

        return {
            "success": False,
            "pnl": 0.0,
            "error": "sell_failed",
            "symbol": symbol,
            "reason": reason,
        }

    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions

    def open_count(self) -> int:
        return len(self.positions)
