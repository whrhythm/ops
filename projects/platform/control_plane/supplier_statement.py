"""Compare a supplier statement against one bounded slice of the internal journal."""
from collections import defaultdict
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from datetime import timedelta

from .model_usage import normalize_usage


class InvoiceLine(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    reference: str = Field(min_length=1, max_length=256)
    input_tokens: int = Field(strict=True, ge=0, le=10**9)
    output_tokens: int = Field(strict=True, ge=0, le=10**9)
    cache_read_tokens: int | None = Field(default=None, strict=True, ge=0, le=10**9)
    cache_write_tokens: int | None = Field(default=None, strict=True, ge=0, le=10**9)
    currency: Literal['USD','CNY']
    amount: Decimal = Field(ge=0, max_digits=28, decimal_places=18, allow_inf_nan=False)


class SupplierStatement(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    channel: str = Field(min_length=1, max_length=128)
    owner_id: str = Field(min_length=1, max_length=255)
    model: str = Field(min_length=1, max_length=200)
    start: AwareDatetime
    end: AwareDatetime
    reference_field: Literal['provider_request_id','response_id']
    lines: list[InvoiceLine] = Field(max_length=5000)

    @model_validator(mode='after')
    def bounded_period(self):
        if not timedelta(0) < self.end-self.start <= timedelta(days=31):
            raise ValueError('Choose a period of up to 31 days')
        return self


def reconcile(statement, journal):
    """Exact reference/amount matching. A trial rate or absent usage cannot certify a purchase."""
    original, recorded = defaultdict(list), defaultdict(list)
    details = []
    for line in statement.lines:
        original[line.reference].append(line)
    for row, test_rate in journal:
        ref = row.payload.get(statement.reference_field)
        if not ref:
            details.append({'receipt_id':row.id,'issues':['missing_supplier_reference']})
        else:
            recorded[ref].append((row,test_rate))
    for ref in sorted(original.keys() | recorded.keys()):
        sources, entries = original[ref], recorded[ref]
        issues = []
        if not sources:
            issues.append('missing_supplier_line')
        if not entries:
            issues.append('missing_internal_receipt')
        if len(sources)>1:
            issues.append('duplicate_supplier_line')
        if len(entries)>1:
            issues.append('duplicate_internal_receipt')
        detail = {'reference':ref,'issues':issues}
        if len(sources)==len(entries)==1:
            line, (row,test_rate) = sources[0], entries[0]
            detail.update(receipt_id=row.id, supplier_amount=str(line.amount), supplier_currency=line.currency,
                          calculated_purchase=row.cost, purchase_currency=row.currency, sale_original=row.native_spend)
            if row.native_spend is None:
                issues.append('missing_sale_original')
            if row.cost is None or not row.rate_id or test_rate is not False:
                issues.append('missing_contract_price')
            elif row.currency != line.currency:
                issues.append('purchase_currency_mismatch')
            elif Decimal(row.cost) != line.amount:
                issues.append('purchase_amount_mismatch')
            try:
                counts = normalize_usage(row.payload.get('usage'),row.payload.get('usage_format','litellm'))
            except ValueError:
                counts = {}
            for field in ('input_tokens','output_tokens','cache_read_tokens','cache_write_tokens'):
                supplied=getattr(line,field)
                if supplied is not None and counts.get(field)!=supplied:
                    issues.append(field+'_mismatch')
        details.append(detail)
    count=sum(not d['issues'] for d in details)
    return {'status':'empty' if not details else ('matched' if count==len(details) else 'differences'),
            'matched':count,'exceptions':len(details)-count,'details':details}
