"""Normalize provider token receipts without guessing what a missing field means."""

FORMATS = ('litellm', 'openai_chat', 'anthropic_messages', 'deepseek_chat')
TOKEN_FIELDS = (
    'input_tokens', 'uncached_input_tokens', 'cache_read_tokens', 'cache_write_tokens',
    'output_tokens', 'reasoning_tokens', 'total_tokens',
)
RAW_COUNTS = (
    'prompt_tokens', 'completion_tokens', 'total_tokens', 'input_tokens', 'output_tokens',
    'cache_read_input_tokens', 'cache_creation_input_tokens',
    'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens',
)
RAW_DETAILS = {
    'prompt_tokens_details': ('cached_tokens', 'cache_creation_tokens', 'cache_write_tokens'),
    'completion_tokens_details': ('reasoning_tokens',),
    'cache_creation': ('ephemeral_5m_input_tokens', 'ephemeral_1h_input_tokens'),
}


def count(value):
    return value if type(value) is int and 0 <= value <= 10**9 else None


def retain_usage(raw):
    if not isinstance(raw, dict):
        return {}
    result = {key: raw[key] for key in RAW_COUNTS if count(raw.get(key)) is not None}
    for name, keys in RAW_DETAILS.items():
        details = raw.get(name)
        if isinstance(details, dict):
            safe = {key: details[key] for key in keys if count(details.get(key)) is not None}
            if safe:
                result[name] = safe
    return result


def agree(*values):
    supplied = {value for value in values if value is not None}
    if len(supplied) > 1:
        raise ValueError('usage_fields_conflict')
    return next(iter(supplied), None)


def normalize_usage(raw, source_format):
    if source_format not in FORMATS:
        raise ValueError('unknown_usage_format')
    raw = retain_usage(raw)
    ttl = raw.get('cache_creation', {})
    if source_format == 'anthropic_messages':
        uncached = raw.get('input_tokens')
        read = raw.get('cache_read_input_tokens')
        write = raw.get('cache_creation_input_tokens')
        input_parts = (uncached, read, write)
        input_total = sum(input_parts) if None not in input_parts else None
        output = raw.get('output_tokens')
        reasoning = None
    else:
        prompt = raw.get('prompt_tokens_details', {})
        input_total = raw.get('prompt_tokens')
        read = agree(prompt.get('cached_tokens'), raw.get('prompt_cache_hit_tokens'))
        write = agree(prompt.get('cache_creation_tokens'), prompt.get('cache_write_tokens'))
        if source_format == 'litellm':
            read = agree(read, raw.get('cache_read_input_tokens'))
            write = agree(write, raw.get('cache_creation_input_tokens'))
        uncached = None
        if input_total is not None:
            if (read or 0) + (write or 0) > input_total:
                raise ValueError('cache_exceeds_input')
            if read is not None and write is not None:
                uncached = input_total - read - write
        if source_format == 'deepseek_chat':
            # Direct DeepSeek documents cache misses as a separate input category.
            uncached = raw.get('prompt_cache_miss_tokens')
            if uncached is not None and read is not None:
                input_total = agree(input_total, uncached + read)
        output = raw.get('completion_tokens')
        reasoning = raw.get('completion_tokens_details', {}).get('reasoning_tokens')
    if write is not None and ttl and sum(ttl.values()) > write:
        raise ValueError('cache_ttl_exceeds_write')
    if reasoning is not None and output is not None and reasoning > output:
        raise ValueError('reasoning_exceeds_output')
    total = input_total + output if input_total is not None and output is not None else None
    total = agree(total, raw.get('total_tokens'))
    return dict(zip(TOKEN_FIELDS, (input_total, uncached, read, write, output, reasoning, total)))
