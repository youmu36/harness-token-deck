"""DeepSeek account balances: apply the user-selected fixed CNY:USD 1:1 accounting convention."""

from decimal import Decimal, InvalidOperation
from typing import cast

from htd.domain.common.time import Instant
from htd.domain.remote.model import BalanceKind, BalanceObservation, RemoteFailure


def balance(payload: dict[str, object], account_id: str, at: Instant, source: str) -> BalanceObservation:
    amount: int | None = None
    original_currency: str | None = None
    rows = payload.get("balance_infos")
    if type(payload.get("is_available")) is bool and isinstance(rows, list):
        supported = [
            cast(dict[str, object], r)
            for r in cast(list[object], rows)
            if isinstance(r, dict) and cast(dict[str, object], r).get("currency") in ("USD", "CNY")
        ]
        currencies = [r["currency"] for r in supported]
        if supported and len(currencies) == len(set(currencies)):
            chosen = next((r for r in supported if r["currency"] == "USD"), supported[0])
            value = chosen.get("total_balance")
            try:
                if type(value) is not str or len(value) > 80:
                    raise ValueError("Invalid amount")
                decimal = Decimal(value)
                if (
                    not decimal.is_finite()
                    or decimal.adjusted() > 26
                    or len(decimal.as_tuple().digits) > 40
                    or abs(int(decimal.as_tuple().exponent)) > 40
                ):
                    raise ValueError("Invalid amount")
                numerator, denominator = decimal.as_integer_ratio()
                amount = (abs(numerator) * 10**12 // denominator) * (-1 if numerator < 0 else 1)
                original_currency = cast(str, chosen["currency"])
            except (ValueError, InvalidOperation, OverflowError):
                pass
    return BalanceObservation(
        account_id,
        at,
        BalanceKind.ACCOUNT_REMAINING,
        source,
        currency="USD" if amount is not None else None,
        amount_pico=amount,
        original_currency=original_currency,
        conversion_rule="fixed-CNY-USD-1:1-v1"
        if original_currency == "CNY"
        else "native-USD-v1"
        if original_currency == "USD"
        else None,
        error=RemoteFailure("invalid_response", "DeepSeek 未返回有效且唯一的 USD 或 CNY 账户余额")
        if amount is None
        else None,
    )
