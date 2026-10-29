import base64
import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import seed_monitor_reader_config as seed


def fixture():
    return {'MONITOR_READER_CONFIG_TARGET': 'projects/test-project/secrets/synthetic',
            'QUANT_MONITOR_READER_APP_ID': '1', 'QUANT_MONITOR_READER_APP_PRIVATE_KEY': 'synthetic-key',
            'QUANT_MONITOR_BINANCE_REPORT_ARCHIVE_URI': 'gs://synthetic/reports',
            'QUANT_MONITOR_BINANCE_STREAM_ID': 'synthetic',
            'QUANT_MONITOR_BINANCE_REPORT_SCOPE_SHA256': 'a' * 64}


def test_seed_checks_original_app_and_transfers_one_config():
    session = Mock()
    session.post.return_value.status_code = 200
    factory = Mock(return_value=session)
    with patch.object(seed, 'reader_installation_id', return_value='2'), \
         patch.object(seed, 'mint_reader_token') as mint, patch('google.auth.default', return_value=(Mock(), None)):
        assert seed.seed(fixture(), session_factory=factory) == 'created'
    mint.assert_called_once_with('1', '2', 'synthetic-key')
    assert factory.call_args.kwargs['max_refresh_attempts'] == 0
    body = session.post.call_args.kwargs['json']
    config = json.loads(base64.b64decode(body['payload']['data']))
    assert config['app_id'] == '1' and config['installation_id'] == '2'
    assert config['private_key'] == 'synthetic-key'
    assert session.post.call_count == 1
    assert session.post.call_args.kwargs['allow_redirects'] is False


def test_preflight_and_unknown_write_never_retry():
    session = Mock()
    with patch.object(seed, 'reader_installation_id', side_effect=ValueError('synthetic')):
        assert seed.seed(fixture(), session_factory=lambda *_args, **_kwargs: session) == 'preflight_failed'
    session.post.assert_not_called()
    session.post.side_effect = TimeoutError('synthetic')
    with patch.object(seed, 'reader_installation_id', return_value='2'), \
         patch.object(seed, 'mint_reader_token'), patch('google.auth.default', return_value=(Mock(), None)):
        assert seed.seed(fixture(), session_factory=lambda *_args, **_kwargs: session) == 'unconfirmed'
    assert session.post.call_count == 1


def test_seed_is_explicit_and_excludes_regular_input_job():
    workflow = (Path(__file__).resolve().parents[3] / '.github/workflows/quant-monitor-inputs.yml').read_text()
    assert 'seed_reader_config:' in workflow
    assert '!inputs.seed_reader_config' in workflow
    seed_job = workflow.split('  seed-reader-config:\n', 1)[1]
    assert "github.event_name == 'workflow_dispatch'" in seed_job
    assert 'github.run_attempt == 1' in seed_job
    assert 'MONITOR_READER_CONFIG_TARGET: ${{ secrets.QUANT_MONITOR_READER_CONFIG_TARGET }}' in seed_job
    assert 'ops/quant-monitor/scripts/seed_monitor_reader_config.py' in seed_job
