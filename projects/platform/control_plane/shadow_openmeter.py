"""Publish confirmed shadow usage using OpenMeter's CloudEvents format."""

SOURCE = 'urn:starbridge:shadow:model-usage'
EVENT_TYPE = 'starbridge.model.usage.v1'


def publish_usage(meter, owner_id, send, *, limit=100):
    """The sender returns only after acceptance and raises on an unknown outcome.

    A retry reuses source and id even if the sink accepted the earlier request.
    """
    published = 0
    for event in meter.pending_usage(owner_id, limit):
        usage = event['usage']
        data = {
            'call_id': event['call_id'],
            'channel': event['channel'],
            'model': event['model'] or event['requested_model'],
            'prompt_tokens': usage['prompt_tokens'],
            'completion_tokens': usage['completion_tokens'],
        }
        if 'total_tokens' in usage:
            data['total_tokens'] = usage['total_tokens']
        send({
            'specversion': '1.0',
            'type': EVENT_TYPE,
            'source': SOURCE,
            'id': event['event_id'],
            'subject': owner_id,
            'time': event['finished_at'],
            'data': data,
        })
        meter.acknowledge_usage(event['event_id'])
        published += 1
    return published
