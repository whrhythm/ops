"""LiteLLM plugin. Loaded only for a configured shadow deployment."""
import asyncio
import os
from uuid import UUID

from litellm.integrations.custom_logger import CustomLogger

from .shadow_metering import ShadowMeter


class ShadowCorrelation(CustomLogger):
    def __init__(self):
        super().__init__()
        self.meter = ShadowMeter(os.environ['STARBRIDGE_SHADOW_DB'])

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if call_type not in ('completion', 'acompletion'):
            return None
        call_id = str(UUID(data['litellm_call_id']))
        model = data.get('model')
        owner = user_api_key_dict.user_id
        await asyncio.to_thread(self.meter.call, call_id, owner, model, data.get('stream') is True)
        return None

    async def async_pre_call_deployment_hook(self, kwargs, call_type):
        base = kwargs.get('api_base')
        if not isinstance(base, str) or not base.startswith('http://127.0.0.1:3318/'):
            return None
        raw = kwargs.get('litellm_call_id') or getattr(kwargs.get('litellm_logging_obj'), 'litellm_call_id', '')
        call_id = str(UUID(raw))
        return {**kwargs, 'extra_headers': {**(kwargs.get('extra_headers') or {}),
                                            'x-starbridge-call-id': call_id}}


callback = ShadowCorrelation()
