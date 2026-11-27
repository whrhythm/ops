"""Shared admission policies. LiteLLM remains the only paid debit authority."""
import base64
import io
import json
import struct
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, text

from .daily_free import BEIJING, billing_day
from .model_call_guard import nanos, stop
from .models import ModelCallReservation, now


def lock_pool(db):
    if db.bind.dialect.name == 'postgresql':
        db.execute(text('SELECT pg_advisory_xact_lock(1935828001, 1)'))
    else:
        conn=db.connection()
        if not conn.connection.driver_connection.in_transaction:
            conn.execute(text('BEGIN IMMEDIATE'))


def bounded_image_tokens(part, profile):
    """Only bound inline images; never fetch caller-supplied network addresses."""
    value=part.get('image_url',{})
    url=value.get('url') if isinstance(value,dict) else value
    if not isinstance(url,str) or not url.startswith(('data:image/png;base64,','data:image/jpeg;base64,','data:image/webp;base64,')):
        raise ValueError('inline_image_required')
    encoded=url.split(',',1)[1]
    if len(encoded)>8*1024*1024:raise ValueError('image_too_large')
    data=base64.b64decode(encoded,validate=True)
    w=h=0
    if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data)>=24:
        w,h=struct.unpack('>II',data[16:24])
    elif data.startswith(b'\xff\xd8'):
        i=2
        while i+4<len(data):
            if data[i]!=255:raise ValueError('invalid_jpeg')
            marker=data[i+1];i+=2
            if marker in (216,217):continue
            size=int.from_bytes(data[i:i+2],'big')
            if size<2:raise ValueError('invalid_jpeg')
            if marker in (192,193,194,195,197,198,199,201,202,203,205,206,207):
                h,w=struct.unpack('>HH',data[i+3:i+7]);break
            i+=size
    elif data[:4]==b'RIFF' and data[8:12]==b'WEBP':
        if data[12:16]==b'VP8X' and len(data)>=30:
            w=int.from_bytes(data[24:27],'little')+1;h=int.from_bytes(data[27:30],'little')+1
        elif data[12:16]==b'VP8 ' and len(data)>=30 and data[23:26]==b'\x9d\x01\x2a':
            w,h=(n&16383 for n in struct.unpack('<HH',data[26:30]))
        elif data[12:16]==b'VP8L' and len(data)>=25 and data[20]==47:
            bits=int.from_bytes(data[21:25],'little');w=(bits&16383)+1;h=((bits>>14)&16383)+1
    if not (0<w<=profile.get('max_image_dimension',2048) and 0<h<=profile.get('max_image_dimension',2048)):
        raise ValueError('image_dimensions_exceeded')
    return int(profile['image_token_bound'])


def base_policy(settings):
    try:
        models=json.loads(settings.managed_chat_profiles)
        if not isinstance(models,dict) or not models:raise ValueError()
        return {'models':models,'max_output_tokens':settings.managed_chat_max_output,
                'starts_at':'2020-01-01T00:00:00+00:00','ends_at':'2100-01-01T00:00:00+00:00'}
    except (ValueError,TypeError):stop('model_budget_invalid_policy')


def free_policy(settings, stamp=None):
    stamp=stamp or now()
    day=billing_day(stamp)
    start=datetime.fromisoformat(day).replace(tzinfo=BEIJING)
    return {**base_policy(settings),'id':'free:'+day,'funding':'free','pool_day':day,'shared_pool':True,
        'starts_at':start.isoformat(),'ends_at':(start+timedelta(days=1)).isoformat(),
        'usd_limit':settings.managed_chat_free_daily_usd,
        'max_output_tokens':min(settings.managed_chat_max_output,settings.daily_free_max_output)}


async def paid_policy(db,env,settings,client):
    from .litellm import model_user_id
    try:
        response=await client.get(settings.litellm_url.rstrip('/')+'/user/info',
            headers={'Authorization':'Bearer '+settings.litellm_master_key},
            params={'user_id':model_user_id(env.issuer,env.subject)},timeout=5)
        if response.status_code!=200:raise ValueError()
        info=response.json()['user_info']
        capital=nanos(info['max_budget']);spent=nanos(info['spend'])
        if capital<=spent:stop('model_turn_budget_exhausted')
        # Original native consumption is the anchor; this ledger only reserves, never debits.
        from decimal import Decimal
        policy={**base_policy(settings),'id':'managed-paid','usd_limit':str(Decimal(min(capital-spent,nanos(100)))/Decimal(10**9)),
            'funding':'paid','managed_wallet':True,'capital_nanos':capital,'opening_spend':spent,
            'max_call_nanos':min(nanos(settings.managed_chat_max_call_usd),capital-spent)}
        return policy
    except (httpx.HTTPError,KeyError,ValueError,TypeError,ArithmeticError):stop('model_budget_balance_verification_unavailable')
