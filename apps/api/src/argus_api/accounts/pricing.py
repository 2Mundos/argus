"""The price table behind estimated cost (cost view spec 3.2)."""

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from argus_api.errors import DomainError

DEFAULT_SOURCE = "argus_api/accounts/pricing.json"


class PricingInvalidError(DomainError):
    status = 500
    code = "pricing_invalid"


@dataclass(frozen=True)
class Price:
    """USD per 1,000,000 tokens."""

    input: float
    output: float
    cached_input: float


@dataclass(frozen=True)
class PriceTable:
    currency: str
    as_of: str
    version: str
    source: str
    models: dict[str, Price]

    def find(self, model: str | None) -> Price | None:
        """Exact key, else the longest key followed by `-` (a dated suffix)."""
        if not model:
            return None
        name = model.lower()
        if name in self.models:
            return self.models[name]
        best = max(
            (k for k in self.models if name.startswith(k + "-")), key=len, default=None
        )
        return self.models[best] if best else None

    def cost(
        self,
        model: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        cached_input_tokens: int | None,
    ) -> float | None:
        price = self.find(model)
        if price is None:
            return None
        return (
            (input_tokens or 0) * price.input
            + (output_tokens or 0) * price.output
            + (cached_input_tokens or 0) * price.cached_input
        ) / 1_000_000


def load(path: Path | None) -> PriceTable:
    """Read the file in force; any defect fails whole, naming the field."""
    source = str(path) if path else DEFAULT_SOURCE
    try:
        text = (
            path.read_text()
            if path
            else resources.files("argus_api.accounts")
            .joinpath("pricing.json")
            .read_text()
        )
        doc = json.loads(text)
        models = {
            str(name).lower(): Price(
                **{k: _rate(p[k]) for k in ("input", "output", "cached_input")}
            )
            for name, p in doc["models"].items()
        }
        return PriceTable(
            currency=str(doc["currency"]),
            as_of=str(doc["as_of"]),
            version=str(doc["version"]),
            source=source,
            models=models,
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise PricingInvalidError(
            f"price table {source} is invalid: {type(exc).__name__}: {exc}"
        ) from None


def _rate(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise ValueError("prices must be non-negative numbers")
    return float(value)
