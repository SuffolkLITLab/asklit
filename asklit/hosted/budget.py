"""Atomic monthly model-spend reservations shared by API processes and workers."""
import math
import time
from datetime import datetime, timezone
from fastapi import HTTPException
from sqlalchemy import func, select, case
from .models import RateBucket, UsageEvent, DeletedOrganizationSpend


class BillingError(HTTPException):
    code = "ai_billing_error"


class ModelPricingMissing(BillingError):
    code = "ai_model_pricing_missing"

    def __init__(self):
        super().__init__(503, "AI billing configuration is incomplete. Ask the LIT Lab to configure model pricing. Reference: ai_model_pricing_missing.")


class MonthlyBudgetExceeded(BillingError):
    code = 'monthly_ai_budget_exceeded'

    def __init__(self):
        super().__init__(429, 'AI spending limit reached: AskLit has used its monthly AI allowance. New AI requests are paused until the next UTC calendar month or the LIT Lab raises the limit. Reference: monthly_ai_budget_exceeded.')


def month_start():
    return datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def committed_cost(db, start):
    # Unknown historic calls predate reservations: conservatively retain $1 each.
    retained = db.get(DeletedOrganizationSpend, start.strftime("%Y-%m"))
    return (retained.amount if retained else 0) + float(db.scalar(select(func.coalesce(func.sum(func.coalesce(
        UsageEvent.estimated_cost_usd, UsageEvent.budget_reserved_usd, 1.0
    )), 0)).where(UsageEvent.created >= start.timestamp())) or 0)


def budget_report(db, settings):
    start = month_start()
    used = committed_cost(db, start)
    retained = db.get(DeletedOrganizationSpend, start.strftime("%Y-%m"))
    limit = settings.monthly_ai_budget_usd
    known, held, legacy = db.execute(select(
        func.coalesce(func.sum(UsageEvent.estimated_cost_usd), 0),
        func.coalesce(func.sum(case((UsageEvent.estimated_cost_usd.is_(None), UsageEvent.budget_reserved_usd), else_=0)), 0),
        func.count().filter(UsageEvent.estimated_cost_usd.is_(None), UsageEvent.budget_reserved_usd.is_(None)),
    ).where(UsageEvent.created >= start.timestamp())).one()
    return {'month': start.strftime('%Y-%m'), 'limit_usd': limit,
            'deleted_organization_spend_usd': retained.amount if retained else 0,
            'estimated_spend_usd': float(known), 'held_usd': float(held), 'legacy_unknown_calls': legacy,
            'committed_usd': used, 'remaining_usd': max(0, limit-used) if limit is not None else None,
            'note': 'Includes anonymous spending totals from deleted organizations. UTC calendar month. Includes conservative reservations for pending or unknown-cost calls; excludes hosting fees and calls outside AskLit.'}


def reserve_budget(db, settings, model, messages):
    if settings.monthly_ai_budget_usd is None:
        return None
    rates = [model.get('budget_input_usd_per_million', model.get('input_usd_per_million')), model.get('budget_output_usd_per_million', model.get('output_usd_per_million'))]
    if messages is None or any(not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 0 for x in rates):
        raise ModelPricingMissing()
    # Text-only requests: UTF-8 bytes conservatively bound text tokens. Allow
    # framing overhead, long-context/cache-write premiums and maximum output.
    # This is a conservative estimate, not a guarantee of the provider invoice.
    input_bound = 2048 + sum(len(m['content'].encode('utf-8')) + 256 for m in messages)
    output_bound = min(int(model.get('max_tokens', 1500)), 4000)
    reservation = math.ceil((input_bound*rates[0]*2.5 + output_bound*rates[1]*1.5)*1.1) / 1_000_000
    start = month_start()
    key = 'monthly-ai-budget:' + start.strftime('%Y-%m')
    if db.bind.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    db.execute(insert(RateBucket).values(key=key, count=0, expires=time.time()+70*86400).on_conflict_do_nothing())
    # The lock stays held until the UsageEvent reservation commits. A second
    # worker cannot inspect the balance and spend the same remainder concurrently.
    db.scalar(select(RateBucket).where(RateBucket.key == key).with_for_update())
    if committed_cost(db, start) + reservation > settings.monthly_ai_budget_usd:
        raise MonthlyBudgetExceeded()
    return reservation


class ProviderAccessDenied(BillingError):
    code = 'ai_provider_access_denied'

    def __init__(self):
        super().__init__(503, 'AWS has blocked AI model access. The LIT Lab should check the AWS monthly budget action and model permissions. Reference: ai_provider_access_denied.')


class ProviderQuotaExceeded(BillingError):
    code = 'ai_provider_quota_exceeded'

    def __init__(self):
        super().__init__(429, 'The AI provider usage quota has been reached. The LIT Lab should check the AWS service quota. Reference: ai_provider_quota_exceeded.')
