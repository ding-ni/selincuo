"""Business endpoints for continuous acquisition, forecasting and scenarios."""
from pathlib import Path
import json
from .common import read_json, ROOT
from . import operational_store as store

TASK_HANDLERS = {
    'lake-curve-comparison': ('lake_curves', 'run_comparison'),
    'data-sync': ('data_connections', 'sync'),
    'forecast-input-ensemble': ('forecast_inputs', 'fetch_ensemble'),
    'forecast': ('forecasts', 'run'),
    'risk-simulation': ('risk_scenarios', 'run'),
    'exposure-fetch': ('risk_scenarios', 'fetch_osm'),
    'report': ('reports', 'run'),
}


def execute(db, kind, request, folder, progress, cancelled):
    from importlib import import_module
    module, function = TASK_HANDLERS[kind]
    return getattr(import_module('.' + module, __package__), function)(db, request, folder, progress, cancelled)


def get(server, path, query):
    db = server.db
    root, _, identifier = path.partition('/')
    if path == 'runtime':
        from .operations_runtime import runtime_status
        return True, runtime_status(server)
    if path == 'station-hover':
        from .station_hover import station_summary
        return True, station_summary(db, query.get('stationId', ''))
    if path == 'map-point-series':
        from .station_hover import map_point_series
        return True, map_point_series(db, query)
    if root == 'lake-curves':
        from . import lake_curves
        return True, next((v for v in lake_curves.catalog(db)['items'] if v['id'] == identifier), None) if identifier else lake_curves.catalog(db)
    if root == 'data-connections':
        from . import data_connections
        return True, data_connections.get_connection(db, identifier) if identifier else data_connections.catalog(db)
    if root in ('forecast-inputs', 'forecasts'):
        from . import forecast_inputs, forecasts
        module = forecast_inputs if root == 'forecast-inputs' else forecasts
        if path == 'forecasts/sources':
            from .published_results import catalog, result_file
            items = []
            for row in catalog():
                try:
                    state = read_json(result_file(row['id'], 'final-state.json'))
                except (FileNotFoundError, ValueError):
                    continue
                if not state or state.get('schemaVersion') != 3:
                    continue
                items.append({'id': row['id'], 'name': row.get('name', row['id']),
                              'stateTime': state['validAt'], 'spatialVersion': state['identity']['spatialVersion'],
                              'inputId': state['inputId']})
            for row in db.rows("SELECT id,name,result FROM tasks WHERE status='succeeded' AND trashed=0 AND kind IN ('simulation','continuation','trial','calibration','two-stage-calibration')"):
                result = json.loads(row['result'] or '{}')
                if (db.workspace / 'tasks' / row['id'] / 'checkpoint.json').is_file():
                    items.append({'id': row['id'], 'name': row['name'] or row['id'],
                                  'stateTime': result.get('end'), 'spatialVersion': result.get('spatialVersion'),
                                  'inputId': result.get('inputId')})
            return True, {'items': items}
        return True, module.detail(db, identifier) if identifier else module.catalog(db)
    if root in ('risk-scenarios', 'risk-results'):
        from . import risk_scenarios
        if identifier.endswith('/frame'):
            return True, risk_scenarios.terrain_frame(db, identifier.split('/')[0], int(query.get('index', 0)))
        if root == 'risk-results' and identifier:
            return True, store.get(db, 'risk-results', identifier)
        if path == 'risk-scenarios/reference-outlets':
            import glob as _glob
            outlet_files = sorted(_glob.glob(str(ROOT / 'config/operational/漫溢溢口参考-*.json')))
            outlets = [read_json(Path(f)) for f in outlet_files]
            return True, {'items': outlets}
        return True, risk_scenarios.detail(db, identifier) if identifier else risk_scenarios.catalog(db)
    if root == 'warnings':
        from . import warnings
        if identifier == 'rules':
            rules = store.list_records(db, 'warning-rules')
            # When no rules have been saved yet, return the built-in defaults so
            # the front-end can pre-fill the form rather than showing a blank table.
            default = warnings.default_rules() if not rules else None
            return True, {'items': rules, 'default': default}
        return True, store.get(db, 'warnings', identifier) if identifier else warnings.catalog(db)
    if root == 'reports':
        from . import reports
        return True, store.get(db, 'reports', identifier) if identifier else reports.catalog(db)
    return False, None


def post(server, path, value):
    db, jobs = server.db, server.jobs
    root, _, identifier = path.partition('/')
    if root == 'runtime':
        from .operations_runtime import save_runtime, configure_autostart
        return True, configure_autostart(server, value) if identifier == 'autostart' else save_runtime(server, value)
    if root == 'lake-curves':
        from . import lake_curves
        return True, jobs.submit('lake-curve-comparison', value) if identifier == 'compare' else lake_curves.save_curve(db, value)
    if root == 'data-connections':
        from . import data_connections as module
        if identifier in ('test', 'discover', 'preview'):
            return True, getattr(module, {'test': 'test_connection', 'discover': 'discover', 'preview': 'preview'}[identifier])(db, value)
        if identifier.endswith('/sync') or identifier == 'sync':
            return True, jobs.submit('data-sync', {**value, 'connectionId': value.get('connectionId') or identifier.split('/')[0]})
        if identifier.endswith('/enabled'):
            return True, module.set_enabled(db, identifier.split('/')[0], value['enabled'])
        return True, module.save_connection(db, value, identifier or None)
    if root == 'forecast-inputs':
        from . import forecast_inputs
        if identifier == 'evaporation-preview':
            return True, forecast_inputs.preview_evaporation(db, value)
        if identifier == 'ensemble':
            return True, jobs.submit('forecast-input-ensemble', value)
        if identifier == 'hourly':
            return True, forecast_inputs.import_hourly(db, value)
        return True, forecast_inputs.create_manual(db, value)
    if root == 'forecasts':
        from . import forecasts
        return True, forecasts.preflight(db, value) if identifier == 'preflight' else jobs.submit('forecast', value)
    if root in ('risk-scenarios', 'risk-results'):
        from . import risk_scenarios
        if identifier == 'run' or identifier.endswith('/run'):
            return True, jobs.submit('risk-simulation', {**value, **({'scenarioId': identifier.split('/')[0]} if identifier != 'run' else {})})
        return True, risk_scenarios.save_scenario(db, value, identifier or None)
    if root == 'exposures':
        from . import risk_scenarios
        return True, jobs.submit('exposure-fetch', value) if identifier == 'osm' else risk_scenarios.import_exposure(db, value)
    if path == 'scenario-attachments':
        from . import risk_scenarios
        return True, risk_scenarios.attachment(db, value)
    if root == 'warnings':
        from . import warnings
        if identifier == 'rules':
            return True, warnings.save_rules(db, value, value.get('id'))
        if identifier.endswith('/publish'):
            return True, warnings.publish(db, identifier.split('/')[0], value)
        if identifier.endswith('/withdraw'):
            return True, warnings.withdraw(db, identifier.split('/')[0], value)
        return True, warnings.draft(db, value)
    if root == 'reports':
        return True, jobs.submit('report', value)
    return False, None
