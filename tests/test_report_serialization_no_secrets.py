import io
import json
import os
import re
from unittest.mock import patch

from quant_monitor_domain.telegram_intent import deliver


def test_report_serialization_has_no_secrets():
    token = str(123456789) + ':' + 'A' * 35
    chat_id = 'synthetic-private-chat'
    for acknowledged in (True, False):
        response = io.BytesIO(json.dumps({'ok': acknowledged}).encode())
        response.status = 200
        with patch.dict(os.environ, {'TG_TOKEN': token}, clear=True):
            receipt = deliver('Synthetic health status', token=token, chat_id=chat_id,
                              opener=lambda *_args, **_kwargs: response)
        serialized = json.dumps({'side_effects': [receipt]})
        assert receipt['ok'] is acknowledged
        assert token not in serialized
        assert chat_id not in serialized
        assert re.search(r'\d{6,12}:A[\w-]{34}', serialized) is None
        for key, value in receipt.items():
            if key.lower() in {'token', 'secret', 'password', 'api_key'}:
                assert not value


def test_provider_exception_does_not_enter_outcome():
    token = str(123456789) + ':' + 'A' * 35
    with patch.dict(os.environ, {'TG_TOKEN': token}, clear=True), patch(
        'urllib.request.urlopen', side_effect=RuntimeError(token),
    ):
        receipt = deliver('Synthetic health status', token=token, chat_id='synthetic-private-chat')
    assert receipt['delivery'] == 'unknown'
    assert token not in json.dumps(receipt)
