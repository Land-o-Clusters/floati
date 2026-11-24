"""Opt-in self-tests for the bus-scale census instrument and its fixtures."""


MODULES = (
    "tests.harbor_bus_scale_arm_clock",
    "tests.harbor_bus_scale_arm_clock_materialization",
    "tests.harbor_bus_scale_attention_clock",
    "tests.harbor_bus_scale_attention_proof",
    "tests.harbor_bus_scale_attention_retry",
    "tests.harbor_bus_scale_bank_atomicity",
    "tests.harbor_bus_scale_case_seeds",
    "tests.harbor_bus_scale_commands",
    "tests.harbor_bus_scale_entry",
    "tests.harbor_bus_scale_equivalence",
    "tests.harbor_bus_scale_exhaustion_family",
    "tests.harbor_bus_scale_expanded_observation",
    "tests.harbor_bus_scale_history_defaults",
    "tests.harbor_bus_scale_history_output",
    "tests.harbor_bus_scale_instrument",
    "tests.harbor_bus_scale_integration",
    "tests.harbor_bus_scale_io",
    "tests.harbor_bus_scale_matrix",
    "tests.harbor_bus_scale_metrics",
    "tests.harbor_bus_scale_producer",
    "tests.harbor_bus_scale_refresh_contract",
    "tests.harbor_bus_scale_refresh_followup",
    "tests.harbor_bus_scale_refresh_population",
    "tests.harbor_bus_scale_routine_entry_policy",
    "tests.harbor_bus_scale_routine_lifecycle",
    "tests.harbor_bus_scale_routine_reads",
    "tests.harbor_bus_scale_routine_writes",
    "tests.harbor_bus_scale_ruled_native_refusal",
    "tests.harbor_bus_scale_runner",
    "tests.harbor_bus_scale_seat_teardown_observation",
    "tests.harbor_bus_scale_seat_teardown_refusal_observation",
    "tests.harbor_bus_scale_seat_teardown_scope",
    "tests.harbor_bus_scale_seeds",
    "tests.harbor_bus_scale_teardown_host_observation",
    "tests.harbor_bus_scale_teardown_instrument_boundaries",
    "tests.harbor_bus_scale_teardown_product_proof",
    "tests.harbor_bus_scale_verify_entry_policy",
    "tests.harbor_bus_scale_workloads",
)


def load_tests(loader, standard_tests, pattern):
    return loader.loadTestsFromNames(MODULES)
