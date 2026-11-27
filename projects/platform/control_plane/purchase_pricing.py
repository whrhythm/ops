"""Versioned supplier unit prices, context bands and daily price schedules."""
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

Price = Annotated[Decimal, Field(ge=0, le=100000, decimal_places=6, allow_inf_nan=False)]


class UnitPrices(BaseModel):
    model_config = ConfigDict(extra='forbid')
    input_per_million: Price
    output_per_million: Price
    cached_per_million: Price | None = None
    cache_write_per_million: Price | None = None


class ContextBand(UnitPrices):
    max_input_tokens: int | None = Field(ge=1, le=10**9, strict=True)


class TimeBand(UnitPrices):
    starts_at: str = Field(pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')


class PricingRule(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['fixed', 'context', 'time']
    fixed: UnitPrices | None = None
    bands: list[ContextBand] = Field(default_factory=list, max_length=30)
    timezone: str | None = Field(default=None, max_length=64)
    time_bands: list[TimeBand] = Field(default_factory=list, max_length=30)

    @model_validator(mode='after')
    def validate_bands(self):
        if self.mode == 'time':
            if self.fixed is not None or self.bands or not self.timezone or len(self.time_bands) < 2:
                raise ValueError('time_bands_and_timezone_required')
            try:
                ZoneInfo(self.timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError('invalid_pricing_timezone') from None
            starts = [band.starts_at for band in self.time_bands]
            if any(a >= b for a, b in zip(starts, starts[1:])):
                raise ValueError('time_bands_must_increase')
            return self
        if self.timezone is not None or self.time_bands:
            raise ValueError('time_bands_require_time_pricing')
        if self.mode == 'fixed':
            if self.fixed is None or self.bands:
                raise ValueError('fixed_price_required')
            return self
        if self.fixed is not None or len(self.bands) < 2 or self.bands[-1].max_input_tokens is not None:
            raise ValueError('context_bands_must_cover_all_input_lengths')
        previous = 0
        for band in self.bands[:-1]:
            if band.max_input_tokens is None or band.max_input_tokens <= previous:
                raise ValueError('context_bounds_must_increase')
            previous = band.max_input_tokens
        return self


def quote(rule: PricingRule, tokens, occurred_at: datetime | None = None):
    """Return exact amounts or explicit missing fields. None means unknown, not zero."""
    price = rule.fixed
    band = None
    time_band = None
    if rule.mode == 'time':
        if occurred_at is None or occurred_at.utcoffset() is None:
            return {'amount': None, 'band': None, 'time_band': None, 'items': [], 'missing': ['occurred_at']}
        local = occurred_at.astimezone(ZoneInfo(rule.timezone))
        clock = local.strftime('%H:%M')
        index = max((i for i, candidate in enumerate(rule.time_bands) if candidate.starts_at <= clock),
                    default=len(rule.time_bands) - 1)
        price = rule.time_bands[index]
        end = rule.time_bands[(index + 1) % len(rule.time_bands)].starts_at
        time_band = {'starts_at': price.starts_at, 'ends_at': end,
                     'timezone': rule.timezone, 'local_time': local.isoformat()}
    if rule.mode == 'context':
        input_total = tokens.get('input_tokens')
        if input_total is None:
            return {'amount': None, 'band': None, 'time_band': None, 'items': [], 'missing': ['input_tokens']}
        lower = 0
        for number, candidate in enumerate(rule.bands, start=1):
            upper = candidate.max_input_tokens
            if upper is None or input_total <= upper:
                price = candidate
                band = {'number': number, 'min_input_tokens': lower, 'max_input_tokens': upper}
                break
            lower = upper + 1
    columns = (
        ('uncached_input_tokens', price.input_per_million),
        ('cache_read_tokens', price.cached_per_million),
        ('cache_write_tokens', price.cache_write_per_million),
        ('output_tokens', price.output_per_million),
    )
    items, missing = [], []
    total = Decimal(0)
    for field, unit in columns:
        quantity = tokens.get(field)
        amount = None
        if quantity is None:
            missing.append(field)
        elif quantity == 0:
            amount = Decimal(0)
        elif unit is None:
            missing.append(f'{field}_price')
        else:
            amount = quantity * unit / 1_000_000
        if amount is not None:
            total += amount
        items.append({'field': field, 'tokens': quantity, 'per_million': str(unit) if unit is not None else None,
                      'amount': str(amount) if amount is not None else None})
    return {'amount': None if missing else str(total), 'band': band, 'time_band': time_band, 'items': items, 'missing': missing}
