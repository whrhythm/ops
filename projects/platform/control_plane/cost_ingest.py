"""Candidate relay outbox -> platform shadow journal; never emits a second metering stream."""
import os
import time
import httpx
from .shadow_metering import ShadowMeter


def main():
    meter = ShadowMeter(os.environ['STARBRIDGE_SHADOW_DB'])
    owner = os.environ['SHADOW_COST_OWNER']
    url = os.environ['SHADOW_COST_URL'].rstrip('/') + '/internal/v1/metering/receipts'
    token = os.environ['SHADOW_COST_TOKEN']
    with httpx.Client(timeout=5, follow_redirects=False) as client:
        while True:
            for event in meter.pending_usage(owner, limit=100):
                if event.get('channel') != os.environ['SHADOW_COST_CHANNEL']:
                    continue
                try:
                    result = client.post(url, headers={'X-Meter-Token': token}, json={'event': event})
                    result.raise_for_status()
                    if result.json().get('event_id') != event['event_id']:
                        break
                    meter.acknowledge_usage(event['event_id'])
                except (httpx.HTTPError, ValueError):
                    break
            time.sleep(10)


if __name__ == '__main__':
    main()
