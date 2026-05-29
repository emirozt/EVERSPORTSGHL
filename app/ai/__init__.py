"""
app.ai — AI client, pricing, and budget enforcement (M7).

Public API::

    from app.ai.pricing import compute_cost, get_price, known_models
    from app.ai.client import AnthropicClient, CompletionResult, get_ai_client
    from app.ai.budget import (
        BudgetStatus,
        BudgetExceededError,
        check_budget,
        assert_budget_available,
        get_monthly_spend,
        is_essential_call,
        maybe_send_soft_cap_warning,
    )
"""
