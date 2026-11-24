# Codex issue 15 observation

The product probe uses accelerated clocks and a fake queue. Queue acceptance does not prove a model turn.

```json
{
  "product_rearm": "new_stop_invocation_has_a_fresh_window",
  "daemon": "no_request_without_mail_queued_for_mail",
  "derived_stop_windows_per_day": {
    "basis": "derived",
    "value": 1.0011587485515643,
    "formula": "86400 / 86300; assumes immediate invocation renewal"
  },
  "model_turns_per_day": {
    "basis": "unknown",
    "value": null
  },
  "native_rearm_needs_turn": "unknown",
  "native_rearm_needs_turn_basis": "unknown",
  "native_rearm_configuration": null,
  "native_measurement_reason": "native_trace_source_unavailable"
}
```

Owner observations require the prepared scratch HOME, native Stop re-trust, and an explicit supported native rollout export. A short interval yields a derived daily rate. An observed intervening turn establishes sequence, not that such a turn is logically necessary. Native banks remain available for owner inspection.
