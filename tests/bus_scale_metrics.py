"""Pure paired metric comparison and contract-bound RED-map controls.

These functions validate supplied numbers and parser testimony, not native
scenario success, instrument completeness, or permission to mark a particular
history-output scenario INFO. Those boundaries belong to the census runner.
"""

from __future__ import annotations

import math
from fractions import Fraction

from tests.bus_scale_commands import _signature, classify_contract


_METRICS = ("cpu_seconds", "read_bytes", "ledger_hold_seconds")
_ROW_FIELDS = frozenset({"subcommand", "metric", "ratio", "classification", "contract"})


def _number(value: object, *, positive: bool = False) -> None:
    if (
        type(value) not in (int, float)
        or (type(value) is float and not math.isfinite(value))
        or (value <= 0 if positive else value < 0)
    ):
        raise ValueError("metric must be a finite nonboolean number in its permitted range")


def _ratio(small: int | float, large: int | float) -> float:
    if small == 0:
        if large != 0:
            raise ValueError("zero-to-positive metric ratio is unresolved")
        return 1.0
    try:
        ratio = float(large / small)
    except (OverflowError, ZeroDivisionError) as exc:
        raise ValueError("metric ratio is unresolved") from exc
    if not math.isfinite(ratio) or (ratio == 0 and large != 0):
        raise ValueError("metric ratio is outside finite nonzero reporting precision")
    return ratio


def _classification(small: int | float, large: int | float, ratio: float) -> str:
    # Compare by multiplication without overflow or an epsilon. Fraction keeps
    # the exact supplied integer/binary-float aggregate values at this boundary.
    exceeds = 2 * Fraction(large) > 3 * Fraction(small)
    if exceeds != (ratio > 1.5):
        raise ValueError("metric ratio cannot represent its threshold classification")
    return "red" if exceeds else "pass"


def compare_pairs(pairs: list[dict], *, history_output: bool = False) -> dict:
    """Compare exactly five predeclared alternating 1x/10x pairs.

    Retain aggregate information, but classify by at least four of five
    individual pair verdicts. A three/two split remains ambiguous.
    """
    return _compare_pairs(pairs, history_output=history_output, count=5)


def compare_first_pass(pairs: list[dict], *, history_output: bool = False) -> dict:
    """Architect's two-pair verdict; disagreement stays ambiguous."""
    return _compare_pairs(pairs, history_output=history_output, count=2)


def compare_output_proportional_pairs(pairs: list[dict]) -> dict:
    """Judge CPU and read work per census-measured emitted byte; hold stays absolute.

    Eligibility is the caller's parser-help-bound census contract. This helper
    accepts no missing output measurements and retains all five raw pairs.
    Legacy historical INFO comparisons above are not used by this judgment.
    """
    if type(pairs) is not list or len(pairs) != 5:
        raise ValueError("output_proportional requires exactly five paired repeats")
    stripped = []
    for pair in pairs:
        if type(pair) is not dict or set(pair) != {"order", "1", "10"}:
            raise ValueError("paired repeat has missing or extra fields")
        row = {"order": pair["order"]}
        for arm in ("1", "10"):
            sample = pair[arm]
            if type(sample) is not dict or set(sample) != set(_METRICS) | {"bytes_emitted"}:
                raise ValueError("output_proportional requires measured bytes_emitted")
            if type(sample["bytes_emitted"]) is not int or sample["bytes_emitted"] <= 0:
                raise ValueError("bytes_emitted must be a positive nonboolean integer")
            row[arm] = {key: sample[key] for key in _METRICS}
        stripped.append(row)
    result = compare_pairs(stripped)
    emitted = {arm: sum(pair[arm]["bytes_emitted"] for pair in pairs) for arm in ("1", "10")}
    for metric, basis in (("cpu_seconds", "cpu_seconds / bytes_emitted"),
                          ("read_bytes", "bytes_read / bytes_emitted")):
        row = result["metrics"][metric]
        row["raw_ratio"] = row["ratio"]
        # Exact cross products preserve the 1.5 boundary for integer bytes and
        # binary-float CPU testimony without rounding each arm's quotient.
        small = Fraction(row["1"]) * emitted["10"]
        large = Fraction(row["10"]) * emitted["1"]
        ratio = _ratio(small, large)
        aggregate_classification = _classification(small, large, ratio)
        ratios = []
        for pair in pairs:
            one = Fraction(pair["1"][metric]) * pair["10"]["bytes_emitted"]
            ten = Fraction(pair["10"][metric]) * pair["1"]["bytes_emitted"]
            paired_ratio = _ratio(one, ten)
            _classification(one, ten, paired_ratio)
            ratios.append(paired_ratio)
        classification, classes = _pair_verdict(ratios)
        row.update(ratio=ratio, classification=classification,
                   aggregate_classification=aggregate_classification,
                   pair_ratios=ratios, pair_classifications=classes,
                   bytes_emitted=emitted, basis=basis)
    return result


def _pair_verdict(ratios):
    if type(ratios) is not list or len(ratios) not in (2, 5):
        raise ValueError("pair testimony requires exactly two or five ratios")
    for ratio in ratios:
        _number(ratio)
    required = 2 if len(ratios) == 2 else 4
    classes = ["red" if ratio > 1.5 else "pass" for ratio in ratios]
    verdict = ("red" if classes.count("red") >= required else
               "pass" if classes.count("pass") >= required else "ambiguous")
    return verdict, classes


def _compare_pairs(pairs, *, history_output, count):
    if type(history_output) is not bool:
        raise ValueError("history_output must be an actual boolean")
    if type(pairs) is not list or len(pairs) != count:
        raise ValueError("metric comparison requires exactly " + str(count) + " paired repeats")
    for index, pair in enumerate(pairs):
        if type(pair) is not dict or set(pair) != {"order", "1", "10"}:
            raise ValueError("paired repeat has missing or extra fields")
        order = pair["order"]
        expected_order = [1, 10] if index % 2 == 0 else [10, 1]
        if (
            type(order) is not list or len(order) != 2
            or any(type(value) is not int for value in order)
            or order != expected_order
        ):
            raise ValueError("paired repeats do not follow the declared alternating order")
        for arm in ("1", "10"):
            sample = pair[arm]
            if type(sample) is not dict or set(sample) != set(_METRICS):
                raise ValueError("paired metric sample has missing or extra metrics")
            _number(sample["cpu_seconds"], positive=True)
            _number(sample["ledger_hold_seconds"])
            if type(sample["read_bytes"]) is not int or sample["read_bytes"] < 0:
                raise ValueError("logical read bytes must be a nonnegative nonboolean integer")

    metrics: dict[str, dict] = {}
    for metric in _METRICS:
        aggregates: dict[str, int | float] = {}
        for arm in ("1", "10"):
            values = [pair[arm][metric] for pair in pairs]
            try:
                if metric == "cpu_seconds":
                    aggregate = math.fsum(values)
                elif metric == "read_bytes":
                    aggregate = sum(values)
                else:
                    aggregate = max(values)
            except (OverflowError, ValueError) as exc:
                raise ValueError("metric aggregate is unresolved") from exc
            _number(aggregate, positive=metric == "cpu_seconds")
            aggregates[arm] = aggregate
        small, large = aggregates["1"], aggregates["10"]
        ratio = _ratio(small, large)
        aggregate_classification = _classification(small, large, ratio)
        pair_ratios = [_ratio(pair["1"][metric], pair["10"][metric]) for pair in pairs]
        for pair, pair_ratio in zip(pairs, pair_ratios):
            _classification(pair["1"][metric], pair["10"][metric], pair_ratio)
        classification, pair_classes = _pair_verdict(pair_ratios)
        if history_output and metric != "ledger_hold_seconds":
            classification = "info"
        metrics[metric] = {
            "1": small, "10": large, "ratio": ratio,
            "classification": classification,
            "aggregate_classification": aggregate_classification,
            "pair_ratios": pair_ratios, "pair_classifications": pair_classes,
        }
    return {"metrics": metrics}


def derive_red_map(rows: list[dict], contracts: dict[str, dict]) -> dict[tuple[str, str], int | float]:
    """Derive only RED rows, bound to exact reviewed full parser contracts.

    The supplied contract population may be a subset; the caller separately
    owns full scenario/command coverage. Every supplied contract must still be
    known and unchanged. Duplicate metric rows refuse even when they are INFO
    or pass, and INFO is never allowed for ledger hold.
    """
    if type(rows) is not list:
        raise ValueError("metric rows must be a list")
    if classify_contract(contracts)["unknown"]:
        raise ValueError("metric contract population contains unknown or stale rows")
    seen: set[tuple[str, str]] = set()
    red: dict[tuple[str, str], int | float] = {}
    for row in rows:
        if type(row) is not dict or set(row) not in (_ROW_FIELDS, _ROW_FIELDS | {"pair_ratios"}):
            raise ValueError("metric row has missing or extra fields")
        path, metric = row["subcommand"], row["metric"]
        if type(path) is not str or path not in contracts:
            raise ValueError("metric row names an unknown subcommand")
        if type(metric) is not str or metric not in _METRICS:
            raise ValueError("metric row names an unknown metric")
        if _signature(row["contract"]) != _signature(contracts[path]):
            raise ValueError("metric row has a stale full parser contract")
        key = (path, metric)
        if key in seen:
            raise ValueError("metric rows contain a duplicate subcommand/metric pair")
        seen.add(key)
        ratio = row["ratio"]
        _number(ratio)
        # Reject ratios beyond finite reporting range without rounding away a
        # supplied integer ratio when retaining the exact map value.
        try:
            finite_ratio = float(ratio)
        except OverflowError as exc:
            raise ValueError("metric ratio cannot be represented finitely") from exc
        if not math.isfinite(finite_ratio):
            raise ValueError("metric ratio cannot be represented finitely")
        classification = row["classification"]
        if type(classification) is not str:
            raise ValueError("metric classification must be a string")
        expected = (_pair_verdict(row["pair_ratios"])[0] if "pair_ratios" in row
                    else "red" if ratio > 1.5 else "pass")
        if classification == "info":
            if metric == "ledger_hold_seconds":
                raise ValueError("ledger hold cannot be informational")
        elif classification != expected:
            raise ValueError("metric ratio and classification disagree")
        if classification == "red":
            # A minority outlier may invert the aggregate. The RED map retains
            # a measured above-budget pair ratio; complete aggregates and every
            # individual ratio remain in the accompanying comparison artifact.
            red[key] = max(row["pair_ratios"]) if "pair_ratios" in row else ratio
    return red


def _validate_red_map(red: dict[tuple[str, str], float]) -> None:
    if type(red) is not dict:
        raise ValueError("RED map must be a dictionary")
    for key, ratio in red.items():
        if (
            type(key) is not tuple or len(key) != 2
            or type(key[0]) is not str or not key[0]
            or key[0] != " ".join(key[0].split())
            or type(key[1]) is not str or key[1] not in _METRICS
        ):
            raise ValueError("RED map key must name a canonical subcommand and known metric")
        _number(ratio, positive=True)
        try:
            finite_ratio = float(ratio)
        except OverflowError as exc:
            raise ValueError("RED map ratio cannot be represented finitely") from exc
        if not math.isfinite(finite_ratio) or ratio <= 1.5:
            raise ValueError("RED map must contain only finite ratios above 1.5")


def require_red_nonexpansion(
    baseline_map: dict[tuple[str, str], float],
    current_map: dict[tuple[str, str], float],
) -> None:
    """Permit only removals or nonincreasing ratios at existing RED keys.

    Maps must come from contract-bound derivation; this function validates their
    numerical shape and nonexpansion, not a second parser or scenario census.
    """
    _validate_red_map(baseline_map)
    _validate_red_map(current_map)
    if not set(current_map) <= set(baseline_map):
        raise ValueError("RED map expanded to a new subcommand or metric")
    for key, ratio in current_map.items():
        if ratio > baseline_map[key]:
            raise ValueError("RED map ratio increased above its prior measured value")
