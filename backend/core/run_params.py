"""Validate queue inputs before durable work is created (API, scheduler and CLI)."""
import inspect


def validate(kind, name, params):
    from backend.core import registry, fullrun, browser, llm
    if not isinstance(params, dict): raise ValueError('Run params must be an object')
    params = dict(params)
    if kind == 'fullrun':
        allowed = {'platforms', 'apply'}
        choices = fullrun._platform_keys()
        selection = params.get('platforms')
        if selection is not None and (not isinstance(selection, list) or not selection or any(not isinstance(s, str) or s not in choices for s in selection)):
            raise ValueError('Choose a nonempty list of supported platforms, or null for all')
    elif kind == 'login':
        if name not in browser.LOGIN_MARKERS: raise ValueError('Unknown login platform')
        allowed = set()
    elif kind == 'collectors':
        if name != 'collect_all': raise ValueError('Unknown collector group')
        allowed = set()
    else:
        catalog = {'collector': registry.COLLECTORS, 'pipeline': registry.PIPELINES, 'skill': registry.SKILLS, 'service': registry.SERVICES}.get(kind, {})
        fn = catalog.get(name)
        if fn is None: raise ValueError('Unknown run kind/name')
        if kind == 'service':
            allowed = {k for k, p in inspect.signature(fn).parameters.items() if k != 'ctx'}
            if set(params) - allowed: raise ValueError('Unsupported parameters: ' + ', '.join(sorted(set(params) - allowed)))
            if 'question_id' in params and (type(params['question_id']) is not int or params['question_id'] < 1):
                raise ValueError('question_id must be a positive integer')
            return params
        if kind == 'skill':
            fn = fn.run if name == 'ats_apply' else fn.execute
        allowed = {k for k, p in inspect.signature(fn).parameters.items() if k not in ('ctx', 'self', 'page') and p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)}
        if kind == 'skill' and 'mode' in params:
            modes = next((p.get('modes', []) for p in registry.PLATFORMS if p['key'] == name), [])
            if params['mode'] not in modes: raise ValueError('Unsupported skill mode')
        if kind == 'skill' and name == 'ats_apply' and params.get('mode') == 'run':
            params.pop('mode')
    if set(params) - allowed: raise ValueError('Unsupported parameters: ' + ', '.join(sorted(set(params) - allowed)))
    bounds = {'application_id': (1, 2147483647), 'limit': (1, 1000), 'score_limit': (1, 1000), 'prepare_limit': (1, 500), 'workers': (1, 8), 'batch': (1, 40), 'max_pages': (1, 20), 'min_score': (0, 100), 'min_desc_len': (100, 20000)}
    for key, value in params.items():
        if key in bounds:
            lo, hi = bounds[key]
            if type(value) is not int or not lo <= value <= hi: raise ValueError(f'{key} must be an integer from {lo} to {hi}')
        if key in ('apply', 'dry_run', 'remote_only', 'only_filtered') and type(value) is not bool:
            raise ValueError(f'{key} must be a boolean')
        if key == 'provider' and value is not None and value not in llm.PROVIDERS: raise ValueError('Unknown provider')
        if key in ('sources', 'queries', 'job_ids') and value is not None:
            if not isinstance(value, list) or not value or len(value) > 100: raise ValueError(f'{key} must be a nonempty list of at most 100 items')
            if key == 'job_ids':
                if any(type(v) is not int or v < 1 for v in value): raise ValueError('Invalid job ID')
            elif any(not isinstance(v, str) or not v.strip() or len(v) > 300 for v in value): raise ValueError(f'Invalid {key}')
            if key == 'sources' and any(v not in fullrun._platform_keys() for v in value): raise ValueError('Unknown source')
    if kind == 'pipeline':
        required = {k for k, p in inspect.signature(registry.PIPELINES[name]).parameters.items()
                    if k != 'ctx' and p.default is inspect.Parameter.empty and p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)}
        if required - set(params): raise ValueError('Missing required parameters: ' + ', '.join(sorted(required - set(params))))
    return params
