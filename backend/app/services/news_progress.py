"""Persist bounded, admin-only progress without retaining provider credentials."""
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import threading

from backend import database_news

current_progress = ContextVar('news_progress', default=None)


class NewsProgress:
    def __init__(self, owner):
        self.owner = owner
        self.lock = threading.RLock()
        self.value = {
            'run_id': owner, 'status': 'running', 'stage': 'collect',
            'started_at': database_news.now_iso(), 'finished_at': None, 'elapsed_ms': 0,
            'last_attempt_at': database_news.now_iso(), 'error': None,
            'candidate_count': 0, 'score_failures': 0, 'selected_count': 0,
            'trace_id': None, 'trace_url': None, 'trace_status': 'disabled',
            'counts': {key: 0 for key in ('sources_total', 'sources_completed', 'fetched', 'candidates', 'scored', 'cached', 'failed', 'selected', 'filtered')},
            'progress': {'completed': 0, 'total': 0, 'percent': 0},
            'source_health': {}, 'items': [],
        }

    def update(self, *, counts=None, **updates):
        with self.lock:
            self.value.update(updates)
            if counts:
                self.value['counts'].update(counts)
            self._save()

    def _save(self):
        now = datetime.now(timezone.utc)
        self.value['elapsed_ms'] = round((now - datetime.fromisoformat(self.value['started_at'])).total_seconds() * 1000)
        counts, stage = self.value['counts'], self.value['stage']
        if stage == 'collect':
            completed, total = counts['sources_completed'], counts['sources_total']
        elif stage == 'score':
            completed, total = counts['scored'] + counts['failed'], counts['candidates']
        else:
            completed, total = (1 if stage == 'complete' else 0), 1
        self.value['progress'] = {'completed': completed, 'total': total,
                                  'percent': round(completed * 100 / total, 1) if total else 0}
        database_news.save_processing_run(self.owner, self.value)
        database_news.update_state('news', self.value, owner=self.owner)

    def source(self, key, health, count):
        with self.lock:
            self.value['source_health'][key] = {**health, 'item_count': count}
            self.value['counts']['sources_completed'] += 1
            self.value['counts']['fetched'] += count
            self._save()

    def candidates(self, candidates):
        rows = []
        for index, item in enumerate(candidates):
            identity = hashlib.sha256(str(item.get('id') or item.get('url') or item['title']).encode()).hexdigest()[:16]
            rows.append({'item_id': f'{index}-{identity}', 'title': item['title'], 'source': item.get('source'),
                         'source_id': item.get('source_id'), 'status': 'pending', 'score': None,
                         'cached': False, 'error': None, 'error_code': None, 'run_id': None,
                         'trace_url': None, 'published_at': item.get('published_at')})
        self.update(stage='score', items=rows, counts={'candidates': len(rows)}, candidate_count=len(rows))

    def item(self, index, *, status, scoring=None, error=None):
        from backend.utils.jev import JevError
        with self.lock:
            row = self.value['items'][index]
            row['status'] = status
            if scoring:
                for key in ('score', 'cached', 'run_id', 'trace_url', 'trace_id', 'attempts', 'weighted_score', 'score_rounding_delta'):
                    if key in scoring:
                        row[key] = scoring[key]
                self.value['counts']['scored'] += 1
                self.value['counts']['cached'] += int(bool(scoring.get('cached')))
            if error:
                row['error'] = str(error) if isinstance(error, JevError) else 'Jev scoring request failed'
                row['error_code'] = error.code if isinstance(error, JevError) else 'scoring_error'
                if isinstance(error, JevError):
                    row.update({key: value for key, value in error.details.items()
                                if key in {'run_id', 'trace_url', 'trace_id', 'attempts', 'raw_score', 'weighted_score', 'score_lower_bound', 'score_upper_bound', 'http_status'}})
                self.value['counts']['failed'] += 1
            self._save()

    def selected(self, selected_indexes):
        with self.lock:
            for index, row in enumerate(self.value['items']):
                if row['status'] == 'scored':
                    row['status'] = 'selected' if index in selected_indexes else 'filtered'
            counts = self.value['counts']
            counts['selected'] = len(selected_indexes)
            counts['filtered'] = counts['scored'] - len(selected_indexes)
            self.value['stage'] = 'summarize'
            self._save()

    def finish(self, status, error=None):
        self.update(status=status, error=error, finished_at=database_news.now_iso(),
                    **({'stage': 'complete'} if status in {'success', 'degraded'} else {}))
