import json
from unittest.mock import Mock

import pytest
import requests

from contributors.notify import community, delivery, feishu


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('REPORT_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('REPORT_ROUND', '1')
    monkeypatch.setattr(delivery, '_job', None)
    monkeypatch.setattr(community.time, 'sleep', lambda _: None)


def call(prompt='select'):
    return community._model_json('secret', 'model', prompt,
        {'records': [{'id': 'repo#1'}]}, thinking=False, deadline=-100,
        validate=lambda value: value['items'])


@pytest.mark.parametrize('kind', ['daily', 'weekly', 'monthly'])
def test_six_attempts_two_rounds_and_no_early_delivery(kind, monkeypatch):
    post = Mock(side_effect=requests.ConnectionError())
    monkeypatch.setattr(community.requests, 'post', post)
    delivery.start(kind, '2026-11-23')
    with pytest.raises(requests.ConnectionError):
        call()
    assert post.call_count == 3
    assert delivery.defer(True)
    # Restart in the same round must not grant a fresh budget.
    delivery.start(kind, '2026-11-23')
    with pytest.raises(RuntimeError):
        call()
    assert post.call_count == 3
    monkeypatch.setenv('REPORT_ROUND', '2')
    delivery.start(kind, '2026-11-23')
    with pytest.raises(requests.ConnectionError):
        call()
    assert post.call_count == 6
    assert not delivery.defer(True)


def test_successful_stage_reused_after_restart(monkeypatch):
    response = Mock()
    response.json.return_value = {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"items": []}'}}]}
    post = Mock(return_value=response)
    monkeypatch.setattr(community.requests, 'post', post)
    delivery.start('daily', '2026-11-23')
    assert call() == []
    monkeypatch.setenv('REPORT_ROUND', '2')
    delivery.start('daily', '2026-11-23')
    assert call() == []
    assert post.call_count == 1
    assert 'secret' not in delivery._job.path.read_text()


def test_ambiguous_send_is_not_replayed(monkeypatch):
    send = Mock(side_effect=feishu.FeishuError('timeout'))
    monkeypatch.setattr(feishu, 'send', send)
    delivery.start('weekly', '2026-11-23')
    with pytest.raises(feishu.FeishuError):
        delivery.send({'card': 'frozen'})
    monkeypatch.setenv('REPORT_ROUND', '2')
    assert not delivery.start('weekly', '2026-11-23')
    assert send.call_count == 1
    assert send.call_args.kwargs == {'attempts': 1}
    assert delivery._job.state['payload'] == {'card': 'frozen'}
    assert not delivery._job.state.get('sent')


def test_second_round_without_first_is_noop(monkeypatch):
    monkeypatch.setenv('REPORT_ROUND', '2')
    assert not delivery.start('monthly', '2026-12-01')


def test_successful_selection_is_not_repeated_when_writing_resumes(monkeypatch):
    response = Mock()
    response.json.return_value = {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"items": []}'}}]}
    post = Mock(side_effect=[response, *[requests.Timeout()] * 3, response])
    monkeypatch.setattr(community.requests, 'post', post)
    delivery.start('daily', '2026-11-23')
    assert call('select') == []
    with pytest.raises(requests.Timeout):
        call('write')
    monkeypatch.setenv('REPORT_ROUND', '2')
    delivery.start('daily', '2026-11-23')
    assert call('select') == []
    assert call('write') == []
    assert post.call_count == 5
    assert not delivery.failed()


def test_prepared_evidence_stays_frozen_across_restart(monkeypatch):
    delivery.start('weekly', '2026-11-23')
    first = delivery.snapshot('evidence', {'records': [{'id': 'repo#1'}]})
    first['records'].append({'id': 'accidental-mutation'})
    monkeypatch.setenv('REPORT_ROUND', '2')
    delivery.start('weekly', '2026-11-23')
    assert delivery.snapshot('evidence', {'records': [{'id': 'repo#2'}]}) == {'records': [{'id': 'repo#1'}]}


def test_daily_first_defers_second_sends_statistics_once(tmp_path, monkeypatch):
    from contextlib import closing
    from contributors import main
    from contributors.events import Event
    from contributors.store import EventStore, UpsertResult
    from test_integration import mk_cfg
    cfg = mk_cfg(tmp_path, notify=True, notify_empty=True, date='2026-11-23')
    with closing(EventStore(cfg.events_db)) as store:
        store.init_schema()
        store.finish_run(store.begin_run(), repos_processed=1)
    pending = UpsertResult([Event('pr', 'project', 1, None, 'change',
        'https://github.com/deepmodeling/project/pull/1', 'dev', '2026-11-22T00:00:00+00:00', 'open')], [])
    monkeypatch.setattr(EventStore, 'pending_in_window', lambda *args: pending)
    monkeypatch.setattr(community, 'enabled', lambda: True)
    monkeypatch.setattr(community, 'daily_summary', lambda *args: (community.UNAVAILABLE, None))
    monkeypatch.setattr(feishu, 'load_env', lambda: None)
    send = Mock()
    monkeypatch.setattr(feishu, 'send', send)
    meta = {'window': {'since': '', 'until': ''}}
    main._record_and_notify([], meta, cfg, 0, [])
    assert send.call_count == 0
    monkeypatch.setenv('REPORT_ROUND', '2')
    main._record_and_notify([], meta, cfg, 0, [])
    assert send.call_count == 1
    body = json.dumps(send.call_args.args[0], ensure_ascii=False)
    assert '1 项新提案' in body
    assert '未生成' not in body and '失败' not in body
    main._record_and_notify([], meta, cfg, 0, [])
    assert send.call_count == 1


@pytest.mark.parametrize('monthly', [False, True])
def test_period_first_defers_second_sends_statistics_once(tmp_path, monkeypatch, monthly):
    from types import SimpleNamespace
    from contributors import main
    from contributors.notify.period_summary import PeriodSummary
    cfg = SimpleNamespace(monthly=monthly, date='2026-11-23', events_db=str(tmp_path/'events.db'),
                          notify_dry_run=False, notify_empty=True, org='deepmodeling')
    monkeypatch.setattr(main.community, 'enabled', lambda: True)
    monkeypatch.setattr(main.feishu, 'load_env', lambda: None)
    monkeypatch.setattr(main.period_summary, 'summarize', lambda *a: PeriodSummary(unavailable=True))
    send = Mock()
    monkeypatch.setattr(feishu, 'send', send)
    main._notify_period(cfg)
    assert send.call_count == 0
    monkeypatch.setenv('REPORT_ROUND', '2')
    main._notify_period(cfg)
    assert send.call_count == 1
    assert '未生成' not in json.dumps(send.call_args.args[0], ensure_ascii=False)
    main._notify_period(cfg)
    assert send.call_count == 1
