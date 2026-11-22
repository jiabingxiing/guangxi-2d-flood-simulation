"""Validation and hydrologic verification module.

Implements strict mass conservation auditing and runoff inflation quantification.
"""

from .mass_balance import (
    verify_mass_balance,
    generate_mass_balance_table,
)
from .event_runoff_validation import (
    EventRunoffMetrics,
    compute_event_metrics,
    compare_runoff_inflation,
)
from .parameter_sensitivity import (
    run_parameter_sensitivity_suite,
)

__all__ = [
    "verify_mass_balance",
    "generate_mass_balance_table",
    "EventRunoffMetrics",
    "compute_event_metrics",
    "compare_runoff_inflation",
    "run_parameter_sensitivity_suite",
]
