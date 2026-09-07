"""Built-dashboard smoke checks with mocked APIs and an isolated headless browser."""
import functools
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
import pytest
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
CHROME = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')

@pytest.fixture
def dashboard():
    if not CHROME.exists() or not (ROOT/'frontend/dist/index.html').exists():
        pytest.skip('Build the frontend and install Chrome to run dashboard smoke checks')
    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            if not Path(self.translate_path(self.path)).is_file(): self.path = '/index.html'
            super().do_GET()
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(ROOT/'frontend/dist')))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        mutations = []
        def route(request):
            parsed = urlparse(request.request.url)
            path = parsed.path
            if parsed.hostname != '127.0.0.1': request.abort(); return
            if not path.startswith('/api/'): request.continue_(); return
            if request.request.method == 'POST':
                mutations.append((path, request.request.post_data_json)); data = {'ok': True, 'run_id': 3}
            elif path == '/api/stats/overview': data = {'totals': {}, 'today': {'touches': 0}, 'review_mode': True, 'active_runs': 0, 'paused_runs': 1}
            elif path == '/api/applications/review' or path == '/api/applications':
                data = {'total': 1, 'items': [{'id': 1, 'job_id': 1, 'company': 'Example', 'title': 'AI Engineer', 'platform': 'ashby', 'method': 'ats_form', 'status': 'needs_human', 'status_options': ['approved', 'submitted', 'rejected_by_user'], 'created_at': '2026-09-07T10:00:00', 'answers': {'factcheck': {'ok': True, 'violations': []}}}]}
            elif path == '/api/applications/facets': data = {'country': {}, 'platform': {}, 'status': {}, 'country_submitted': {}}
            elif path == '/api/runs/health': data = {'worker_active': False, 'queued': 1, 'browser_handoffs': [2], 'browser': {'profile': 'shared', 'busy': False}}
            elif path == '/api/runs': data = [{'id': 2, 'kind': 'fullrun', 'name': 'full:linkedin', 'status': 'paused_for_human', 'started_at': '2026-09-07T10:00:00', 'ended_at': '2026-09-07T10:15:00', 'stats': {}, 'error': None}]
            elif path in ('/api/runs/events', '/api/runs/fullrun/platforms', '/api/platforms'): data = []
            elif path == '/api/platforms/llm': data = {}
            else: data = {}
            request.fulfill(status=200, content_type='application/json', body=json.dumps(data))
        page.route('**/*', route)
        yield page, f'http://127.0.0.1:{server.server_port}', mutations, errors
        browser.close()
    server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_review_requires_evidence_before_retry(dashboard, tmp_path):
    page, base, mutations, errors = dashboard
    page.goto(base+'/review')
    page.get_by_label('Update application 1').select_option('approved')
    expect(page.get_by_role('button', name='Save', exact=True)).to_be_disabled()
    page.get_by_label('Check employer history and explain why retrying is safe').fill('Checked employer history: no application received.')
    page.get_by_role('button', name='Save', exact=True).click()
    expect(page.get_by_label('Update application 1')).to_have_value('')
    assert mutations == [('/api/applications/1/status', {'status': 'approved', 'note': 'Checked employer history: no application received.'})]
    assert not errors
    page.screenshot(path=str(tmp_path/'review.png'), full_page=True)


def test_runs_show_worker_handoff_and_retry_without_nested_buttons(dashboard, tmp_path):
    page, base, mutations, errors = dashboard
    page.goto(base+'/runs')
    expect(page.get_by_text('Queued tasks will wait until the worker starts.')).to_be_visible()
    expect(page.get_by_text('Browser work is held for run', exact=False)).to_be_visible()
    assert page.locator('button button').count() == 0
    page.get_by_role('button', name='Retry unfinished work').click()
    assert mutations == [('/api/runs/2/retry', None)]
    assert not errors
    page.screenshot(path=str(tmp_path/'runs.png'), full_page=True)
