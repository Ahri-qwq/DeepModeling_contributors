"""Durable report and model-stage checkpoints for the two scheduled rounds.

The shell holds a report lock across the process. A send-started marker deliberately
favours no duplicate delivery over retrying an ambiguous webhook timeout.
"""
import hashlib
import json
import logging
import os
import threading
from pathlib import Path

from . import feishu

LOG = logging.getLogger(__name__)
_lock = threading.RLock()
_job = None


class Job:
    def __init__(self, kind, day):
        self.round = int(os.environ['REPORT_ROUND'])
        if self.round not in (1, 2):
            raise ValueError('REPORT_ROUND must be 1 or 2')
        self.key = f'{kind}-{day}'
        self.failed = False
        self.path = Path(os.environ.get('REPORT_STATE_DIR', 'data/report-delivery')) / (self.key + '.json')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {'stages': {}}

    def save(self):
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.state, ensure_ascii=False), encoding='utf-8')
        with tmp.open('rb') as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)


def start(kind, day):
    global _job
    _job = Job(kind, day) if os.environ.get('REPORT_ROUND') else None
    if _job:
        LOG.warning('report=%s round=%s start', _job.key, _job.round)
        if _job.state.get('send_started') or _job.state.get('complete'):
            LOG.warning('report=%s skip: delivery already attempted or report complete', _job.key)
            return False
        if _job.round == 2 and not _job.path.exists():
            LOG.warning('report=%s skip: no first-round checkpoint', _job.key)
            return False
        _job.save()
    return True


def defer(unavailable):
    unavailable = unavailable or failed()
    if unavailable and _job:
        LOG.warning('report=%s round=%s summary unavailable; action=%s',
                    _job.key, _job.round, 'defer' if _job.round == 1 else 'statistics')
        return _job.round == 1
    return False


def failed():
    return bool(_job and _job.failed)


def model_attempt(key):
    if key is None:
        return 1
    with _lock:
        attempts = _job.state['stages'][key].setdefault('attempts', {})
        label = str(_job.round)
        if attempts.get(label, 0) >= 3:
            return False
        attempts[label] = attempts.get(label, 0) + 1
        _job.save()
        return attempts[label]


def model_failure():
    if _job:
        _job.failed = True


def complete():
    if _job:
        _job.state['complete'] = True
        _job.save()


def snapshot(name, value):
    """Freeze prepared evidence so retries use the same batches and validators."""
    if not _job:
        return value
    with _lock:
        snapshots = _job.state.setdefault('snapshots', {})
        if name not in snapshots:
            snapshots[name] = json.loads(json.dumps(value))
            _job.save()
        return json.loads(json.dumps(snapshots[name]))


def model_stage(prompt, data):
    """Pin model input and reuse successful JSON, including empty selections.

Record identities separate parallel period batches. Full input is pinned on the
first attempt, so a later round resumes the same evidence instead of redrafting.
"""
    if not _job:
        return None, data, None
    ids = sorted(str(r.get('id', '')) for r in data.get('records', []))
    key = hashlib.sha256(json.dumps([prompt, ids], ensure_ascii=False).encode()).hexdigest()
    with _lock:
        stage = _job.state['stages'].setdefault(key, {'input': data})
        _job.save()
        if 'output' in stage:
            LOG.warning('report=%s round=%s stage=%s reuse', _job.key, _job.round, key[:12])
        return key, stage['input'], stage.get('output')


def model_success(key, output):
    if key is not None:
        with _lock:
            _job.state['stages'][key]['output'] = output
            _job.save()


def context():
    return f'report={_job.key} round={_job.round}' if _job else 'report=manual'


def send(payload):
    if not _job:
        return feishu.send(payload)
    _job.state['payload'] = payload
    _job.save()
    _job.state['send_started'] = True
    _job.save()
    LOG.warning('%s stage=send attempt=1 start', context())
    try:
        feishu.send(payload, attempts=1)
    except Exception as exc:
        LOG.error('%s stage=send result=unknown_or_failed error=%s; no automatic resend', context(), type(exc).__name__)
        raise
    _job.state['sent'] = True
    _job.save()
    LOG.warning('%s stage=send success', context())
