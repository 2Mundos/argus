"""The cost view's group figures (cost view spec 3.1): one SQL column list summed per
(group, model), priced in Python. Shared by the Cost tab and task cost."""

from collections.abc import Iterable

from argus_core.cost import CostFigures

from argus_api.accounts.pricing import PriceTable

NO_MODEL = "(no model)"

# Per (group, model): runs, measured, not_reported, token sums and reported cost
# over measured rows (SUM of only NULLs is NULL, which keeps "no figure" apart
# from zero), and the measured runs that carry any token figure (the priceable ones).
FIGURES = (
    "u.model, count(*), coalesce(sum(u.measurement = 'measured'), 0),"
    " coalesce(sum(u.measurement = 'not_reported'), 0),"
    " sum(CASE WHEN u.measurement = 'measured' THEN u.input_tokens END),"
    " sum(CASE WHEN u.measurement = 'measured' THEN u.output_tokens END),"
    " sum(CASE WHEN u.measurement = 'measured' THEN u.cached_input_tokens END),"
    " sum(CASE WHEN u.measurement = 'measured' THEN u.cost_usd END),"
    " coalesce(sum(u.measurement = 'measured' AND (u.input_tokens IS NOT NULL"
    "  OR u.output_tokens IS NOT NULL OR u.cached_input_tokens IS NOT NULL)), 0)"
)


class Acc:
    """Running figures of one group; folds in one (group, model) row at a time."""

    def __init__(self) -> None:
        self.runs = self.measured = self.not_reported = self.unpriced = 0
        self.attempts = self.measured_attempts = self.not_reported_attempts = 0
        self.unpriced_models: set[str] = set()
        self.tokens: list[int | None] = [None, None, None]
        self.estimated: float | None = None
        self.reported: float | None = None

    def add(self, table: PriceTable, row: Iterable) -> None:
        model, n, measured, nr, i, o, c, reported, priceable = row
        self.runs += n
        self.measured += measured
        self.not_reported += nr
        for k, v in enumerate((i, o, c)):
            if v is not None:
                self.tokens[k] = (self.tokens[k] or 0) + v
        if reported is not None:
            self.reported = (self.reported or 0.0) + reported
        if not priceable:
            return
        cost = table.cost(model, i, o, c)
        if cost is None:
            self.unpriced += priceable
            self.unpriced_models.add(model or NO_MODEL)
        else:
            self.estimated = (self.estimated or 0.0) + cost

    def set_attempts(self, attempts: int, measured: int, not_reported: int) -> None:
        self.attempts = attempts
        self.measured_attempts = measured
        self.not_reported_attempts = not_reported

    def merge(self, other: "Acc") -> None:
        self.runs += other.runs
        self.measured += other.measured
        self.not_reported += other.not_reported
        self.attempts += other.attempts
        self.measured_attempts += other.measured_attempts
        self.not_reported_attempts += other.not_reported_attempts
        self.unpriced += other.unpriced
        self.unpriced_models |= other.unpriced_models
        for k, v in enumerate(other.tokens):
            if v is not None:
                self.tokens[k] = (self.tokens[k] or 0) + v
        if other.estimated is not None:
            self.estimated = (self.estimated or 0.0) + other.estimated
        if other.reported is not None:
            self.reported = (self.reported or 0.0) + other.reported

    def figures(self) -> CostFigures:
        return CostFigures(
            runs=self.runs,
            measured_runs=self.measured,
            not_reported_runs=self.not_reported,
            attempts=self.attempts,
            measured_attempts=self.measured_attempts,
            not_reported_attempts=self.not_reported_attempts,
            input_tokens=self.tokens[0],
            output_tokens=self.tokens[1],
            cached_input_tokens=self.tokens[2],
            estimated_cost_usd=self.estimated,
            reported_cost_usd=self.reported,
            unpriced_runs=self.unpriced,
        )
