"""Explicit FastAPI routes calling the tenant-scoped service layer."""
from __future__ import annotations
from urllib.parse import parse_qs, urlparse
from fastapi import APIRouter, Depends, Path
from .api import ActionService, CSVIngestor, XLSXIngestor, REPORT_SPECS, SUPPORTED_REPORT_TYPES, WeeklyOpsCouncil
from .errors import NotFoundError, ValidationError
from .http_context import HTTPContext, get_context
from .http_schemas import AgentRunRequest, FollowUpRequest
router = APIRouter()

@router.get('/v1/me', summary='Read the authenticated principal', tags=['me'])
def get_v1_me(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    tenant = ctx.app.db.get_tenant(principal.tenant_id)
    return ctx._json(200, {'tenant_id': principal.tenant_id, 'tenant_name': tenant['name'], 'tenant_mode': tenant['mode'], 'user_id': principal.user_id, 'email': principal.email, 'role': principal.role}, request_id)

@router.get('/v1/users', summary='List users owned by the authenticated tenant', tags=['users'])
def get_v1_users(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    return ctx._json(200, {'users': ctx.app.auth.list_users(principal)}, request_id)

@router.get('/v1/api-keys', summary='List tenant API-key metadata', tags=['api-keys'])
def get_v1_api_keys(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    ctx.app.auth.require(principal, 'admin')
    return ctx._json(200, {'keys': ctx.app.db.list_api_keys(principal.tenant_id)}, request_id)

@router.get('/v1/connectors', summary='List tenant-owned marketplace connector accounts', tags=['connectors'])
def get_v1_connectors(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    return ctx._json(200, {'connectors': ctx.app.accounts.list(principal)}, request_id)

@router.get('/v1/connectors/{accountId}', summary='Read one tenant marketplace connector account', tags=['connectors'])
def get_v1_connectors_accountId(ctx: HTTPContext=Depends(get_context), accountId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    account_id = accountId
    return ctx._json(200, ctx.app.accounts.get(principal, account_id), request_id)

@router.get('/v1/provider-smoke-tests', summary='List tenant-owned provider connectivity tests', tags=['provider-smoke-tests'])
def get_v1_provider_smoke_tests(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    parsed = urlparse(ctx.path)
    params = parse_qs(parsed.query, keep_blank_values=True)
    unknown = set(params) - {'limit'}
    if unknown:
        raise ValidationError('unknown query fields: ' + ', '.join(sorted(unknown)))
    return ctx._json(200, {'provider_smoke_tests': ctx.app.provider_smoke.list(principal, ctx._query_int(params, 'limit', default=100, minimum=1, maximum=200))}, request_id)

@router.get('/v1/provider-smoke-tests/{smokeTestId}', summary='Read one tenant-owned provider connectivity test', tags=['provider-smoke-tests'])
def get_v1_provider_smoke_tests_smokeTestId(ctx: HTTPContext=Depends(get_context), smokeTestId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    smoke_test_id = smokeTestId
    return ctx._json(200, ctx.app.provider_smoke.get(principal, smoke_test_id), request_id)

@router.get('/v1/report-recipes', summary='List tenant-owned Amazon report recipes', tags=['report-recipes'])
def get_v1_report_recipes(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    return ctx._json(200, {'report_recipes': ctx.app.report_recipes.list(principal)}, request_id)

@router.get('/v1/report-recipes/{recipeId}', summary='Read one tenant-owned Amazon report recipe', tags=['report-recipes'])
def get_v1_report_recipes_recipeId(ctx: HTTPContext=Depends(get_context), recipeId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    recipe_id = recipeId
    return ctx._json(200, ctx.app.report_recipes.get(principal, recipe_id), request_id)

@router.get('/v1/report-syncs', summary='List tenant-owned Amazon report syncs', tags=['report-syncs'])
def get_v1_report_syncs(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    parsed = urlparse(ctx.path)
    limit = int(parse_qs(parsed.query).get('limit', [100])[0])
    return ctx._json(200, {'report_syncs': ctx.app.report_syncs.list(principal, limit)}, request_id)

@router.get('/v1/report-syncs/{syncId}', summary='Read one tenant-owned Amazon report sync', tags=['report-syncs'])
def get_v1_report_syncs_syncId(ctx: HTTPContext=Depends(get_context), syncId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    sync_id = syncId
    return ctx._json(200, ctx.app.report_syncs.get(principal, sync_id), request_id)

@router.get('/v1/ads-capability-gates', summary='List tenant-owned Amazon Ads capability gates', tags=['ads-capability-gates'])
def get_v1_ads_capability_gates(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    parsed = urlparse(ctx.path)
    params = parse_qs(parsed.query, keep_blank_values=True)
    limit = ctx._query_int(params, 'limit', default=100, minimum=1, maximum=200)
    return ctx._json(200, {'ads_capability_gates': ctx.app.ads_gates.list(principal, limit)}, request_id)

@router.get('/v1/ads-capability-gates/{gateId}', summary='Read one tenant-owned Amazon Ads capability gate', tags=['ads-capability-gates'])
def get_v1_ads_capability_gates_gateId(ctx: HTTPContext=Depends(get_context), gateId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    gate_id = gateId
    return ctx._json(200, ctx.app.ads_gates.get(principal, gate_id), request_id)

@router.get('/v1/ads-adapter-status', summary='Read the conditional Amazon Ads adapter status', tags=['ads-adapter-status'])
def get_v1_ads_adapter_status(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    parsed = urlparse(ctx.path)
    params = parse_qs(parsed.query, keep_blank_values=True)
    unknown = set(params) - {'connector_account_id'}
    if unknown:
        raise ValidationError('unknown query fields: ' + ', '.join(sorted(unknown)))
    connector_account_id = params.get('connector_account_id', [None])[0]
    return ctx._json(200, ctx.app.ads_adapter_status.get(principal, connector_account_id), request_id)

@router.get('/v1/proposals', summary='List tenant-owned durable proposals', tags=['proposals'])
def get_v1_proposals(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    parsed = urlparse(ctx.path)
    params = parse_qs(parsed.query, keep_blank_values=True)
    return ctx._json(200, {'proposals': ctx.app.proposals.list(principal, status=params.get('status', [None])[0] or None, limit=ctx._query_int(params, 'limit', default=100, minimum=1, maximum=200))}, request_id)

@router.get('/v1/proposals/{proposalId}', summary='Read a tenant-owned proposal and its local decisions', tags=['proposals'])
def get_v1_proposals_proposalId(ctx: HTTPContext=Depends(get_context), proposalId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    proposal_id = proposalId
    return ctx._json(200, ctx.app.proposals.get(principal, proposal_id), request_id)

@router.get('/v1/proposal-executions', summary='List tenant-owned proposal executions', tags=['proposal-executions'])
def get_v1_proposal_executions(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    parsed = urlparse(ctx.path)
    params = parse_qs(parsed.query, keep_blank_values=True)
    return ctx._json(200, {'executions': ctx.app.proposals.list_executions(principal, proposal_id=params.get('proposal_id', [None])[0] or None, limit=ctx._query_int(params, 'limit', default=100, minimum=1, maximum=200))}, request_id)

@router.get('/v1/proposal-executions/{executionId}', summary='Read one tenant-owned proposal execution', tags=['proposal-executions'])
def get_v1_proposal_executions_executionId(ctx: HTTPContext=Depends(get_context), executionId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    execution_id = executionId
    return ctx._json(200, ctx.app.proposals.get_execution(principal, execution_id), request_id)

@router.get('/v1/evidence-imports', summary='List tenant-owned CSV evidence imports', tags=['evidence-imports'])
def get_v1_evidence_imports(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    parsed = urlparse(ctx.path)
    limit = int(parse_qs(parsed.query).get('limit', [100])[0])
    return ctx._json(200, {'imports': ctx.app.evidence_imports.list(principal, limit)}, request_id)

@router.get('/v1/evidence-imports/{importId}', summary='Read tenant-owned evidence-import metadata', tags=['evidence-imports'])
def get_v1_evidence_imports_importId(ctx: HTTPContext=Depends(get_context), importId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    import_id = importId
    return ctx._json(200, ctx.app.evidence_imports.get(principal, import_id), request_id)

@router.get('/v1/metric-observations', summary='List persisted metric observations owned by the authenticated tenant', tags=['metric-observations'])
def get_v1_metric_observations(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    parsed = urlparse(ctx.path)
    params = parse_qs(parsed.query, keep_blank_values=True)
    return ctx._json(200, ctx.app.metric_observations.list_observations(principal, limit=ctx._query_int(params, 'limit', default=100, minimum=1, maximum=200), cursor=params.get('cursor', [None])[0], evidence_import_id=params.get('evidence_import_id', [None])[0], platform=params.get('platform', [None])[0], metric_key=params.get('metric_key', [None])[0], currency=params.get('currency', [None])[0]), request_id)

@router.get('/v1/metric-observations/{observationId}', summary='Read one persisted metric observation owned by the authenticated tenant', tags=['metric-observations'])
def get_v1_metric_observations_observationId(ctx: HTTPContext=Depends(get_context), observationId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    observation_id = observationId
    return ctx._json(200, ctx.app.metric_observations.get_observation(principal, observation_id), request_id)

@router.get('/v1/metric-materializations', summary='List persisted metric materialization attempts owned by the authenticated tenant', tags=['metric-materializations'])
def get_v1_metric_materializations(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    parsed = urlparse(ctx.path)
    params = parse_qs(parsed.query, keep_blank_values=True)
    return ctx._json(200, ctx.app.metric_observations.list_materializations(principal, limit=ctx._query_int(params, 'limit', default=100, minimum=1, maximum=200), cursor=params.get('cursor', [None])[0], evidence_import_id=params.get('evidence_import_id', [None])[0], status=params.get('status', [None])[0]), request_id)

@router.get('/v1/agent-runs', summary='List tenant-owned multi-agent workflow runs', tags=['agent-runs'])
def get_v1_agent_runs(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    parsed = urlparse(ctx.path)
    limit = int(parse_qs(parsed.query).get('limit', [50])[0])
    return ctx._json(200, {'runs': ctx.app.agent_runs.list(principal, limit)}, request_id)

@router.get('/v1/daily-ops-schedules', summary='List tenant-owned Daily Ops schedules', tags=['daily-ops-schedules'])
def get_v1_daily_ops_schedules(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    return ctx._json(200, {'schedules': ctx.app.daily_ops.list_schedules(principal)}, request_id)

@router.get('/v1/daily-ops-schedules/{scheduleId}', summary='Read a tenant-owned Daily Ops schedule', tags=['daily-ops-schedules'])
def get_v1_daily_ops_schedules_scheduleId(ctx: HTTPContext=Depends(get_context), scheduleId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    schedule_id = scheduleId
    return ctx._json(200, ctx.app.daily_ops.get_schedule(principal, schedule_id), request_id)

@router.get('/v1/daily-ops-runs', summary='List tenant-owned Daily Ops occurrences', tags=['daily-ops-runs'])
def get_v1_daily_ops_runs(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    parsed = urlparse(ctx.path)
    params = parse_qs(parsed.query, keep_blank_values=True)
    return ctx._json(200, {'runs': ctx.app.daily_ops.list_runs(principal, schedule_id=params.get('schedule_id', [None])[0], limit=ctx._query_int(params, 'limit', default=50, minimum=1, maximum=200))}, request_id)

@router.get('/v1/daily-ops-runs/{runId}', summary='Read a tenant-owned Daily Ops occurrence', tags=['daily-ops-runs'])
def get_v1_daily_ops_runs_runId(ctx: HTTPContext=Depends(get_context), runId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    run_id = runId
    return ctx._json(200, ctx.app.daily_ops.get_run(principal, run_id), request_id)

@router.get('/v1/daily-ops-runs/{runId}/brief', summary='Read the persisted eligible Daily Ops brief', tags=['daily-ops-runs'])
def get_v1_daily_ops_runs_runId_brief(ctx: HTTPContext=Depends(get_context), runId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    run_id = runId
    return ctx._json(200, ctx.app.daily_ops.get_brief(principal, run_id), request_id)

@router.get('/v1/agent-graphs', summary='List tenant-owned agent graph definitions', tags=['agent-graphs'])
def get_v1_agent_graphs(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    return ctx._json(200, {'graphs': ctx.app.agent_graphs.list(principal)}, request_id)

@router.get('/v1/agent-graphs/{graphId}', summary='Read a tenant-owned agent graph and versions', tags=['agent-graphs'])
def get_v1_agent_graphs_graphId(ctx: HTTPContext=Depends(get_context), graphId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    graph_id = graphId
    return ctx._json(200, ctx.app.agent_graphs.get(principal, graph_id), request_id)

@router.get('/v1/agent-graph-versions/{versionId}', summary='Read one tenant-owned agent graph version', tags=['agent-graph-versions'])
def get_v1_agent_graph_versions_versionId(ctx: HTTPContext=Depends(get_context), versionId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    version_id = versionId
    return ctx._json(200, ctx.app.agent_graphs.get_version(principal, version_id), request_id)

@router.get('/v1/agent-runs/{runId}', summary='Read a tenant-owned run with tasks, artifacts, and events', tags=['agent-runs'])
def get_v1_agent_runs_runId(ctx: HTTPContext=Depends(get_context), runId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    run_id = runId
    return ctx._json(200, ctx.app.agent_runs.get(principal, run_id), request_id)

@router.get('/v1/agent-runs/{runId}/trace', summary='Read plans, bounded memory snapshots, context assembly and tool-call metadata', tags=['agent-runs'])
def get_v1_agent_runs_runId_trace(ctx: HTTPContext=Depends(get_context), runId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    run_id = runId
    return ctx._json(200, ctx.app.agent_runs.trace(principal, run_id), request_id)

@router.get('/v1/agent-runs/{runId}/events', summary='Read an incremental event page for a run', tags=['agent-runs'])
def get_v1_agent_runs_runId_events(ctx: HTTPContext=Depends(get_context), runId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    parsed = urlparse(ctx.path)
    run_id = runId
    params = parse_qs(parsed.query)
    ctx.app.auth.require(principal, 'viewer')
    return ctx._json(200, ctx.app.db.list_agent_events_after(principal.tenant_id, run_id, after=params.get('after', [None])[0], limit=int(params.get('limit', [100])[0])), request_id)

@router.get('/v1/agent-runs/{runId}/evaluations', summary='List persisted evaluations for a run', tags=['agent-runs'])
def get_v1_agent_runs_runId_evaluations(ctx: HTTPContext=Depends(get_context), runId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    run_id = runId
    return ctx._json(200, {'evaluations': ctx.app.evaluator.list(principal, run_id)}, request_id)

@router.get('/v1/jobs', summary='List tenant-owned durable jobs', tags=['jobs'])
def get_v1_jobs(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    parsed = urlparse(ctx.path)
    limit = int(parse_qs(parsed.query).get('limit', [100])[0])
    return ctx._json(200, {'jobs': ctx.app.jobs.list(principal, limit)}, request_id)

@router.get('/v1/jobs/{jobId}', summary='Read one tenant-owned durable job', tags=['jobs'])
def get_v1_jobs_jobId(ctx: HTTPContext=Depends(get_context), jobId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    job_id = jobId
    return ctx._json(200, ctx.app.jobs.get(principal, job_id), request_id)

@router.get('/v1/schedules', summary='List tenant-owned interval schedules', tags=['schedules'])
def get_v1_schedules(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    return ctx._json(200, {'schedules': ctx.app.schedules.list(principal)}, request_id)

@router.get('/v1/approvals', summary='List requested actions awaiting a second actor', tags=['approvals'])
def get_v1_approvals(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    ctx.app.auth.require(principal, 'admin')
    return ctx._json(200, {'actions': ctx.app.db.list_actions(principal.tenant_id, status='requested', limit=100)}, request_id)

@router.get('/v1/mission-control', summary="Read a tenant's operational control-plane snapshot", tags=['mission-control'])
def get_v1_mission_control(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    ctx.app.auth.require(principal, 'viewer')
    return ctx._json(200, ctx.app.db.mission_control(principal.tenant_id), request_id)

@router.get('/v1/pilot-status', summary='Read tenant-scoped Pilot readiness and local worker health', tags=['pilot-status'])
def get_v1_pilot_status(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    return ctx._json(200, ctx.app.pilot.status(principal), request_id)

@router.get('/v1/assurance-runs', summary='List tenant-scoped durable assurance runs', tags=['assurance-runs'])
def get_v1_assurance_runs(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    parsed = urlparse(ctx.path)
    params = parse_qs(parsed.query, keep_blank_values=True)
    unknown = set(params) - {'limit'}
    if unknown:
        raise ValidationError('unknown query fields: ' + ', '.join(sorted(unknown)))
    return ctx._json(200, {'runs': ctx.app.assurance.list(principal, ctx._query_int(params, 'limit', default=100, minimum=1, maximum=200))}, request_id)

@router.get('/v1/assurance-runs/{assuranceRunId}', summary='Read one tenant-scoped assurance run', tags=['assurance-runs'])
def get_v1_assurance_runs_assuranceRunId(ctx: HTTPContext=Depends(get_context), assuranceRunId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    return ctx._json(200, ctx.app.assurance.get(principal, assuranceRunId), request_id)

@router.get('/v1/briefing', summary='Read an evidence-backed operating brief for one marketplace', tags=['briefing'])
def get_v1_briefing(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    parsed = urlparse(ctx.path)
    platform = parse_qs(parsed.query).get('platform', ['amazon'])[0]
    return ctx._json(200, ctx.app.briefing.get(principal, platform), request_id)

@router.get('/v1/catalog', summary='Read the live platform, report, workflow, and action catalog', tags=['catalog'])
def get_v1_catalog(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    ctx.app.auth.require(principal, 'viewer')
    platforms = sorted(ctx.app.agent_runs.platform_registry.entries().values(), key=lambda item: item['id'])
    connector_catalog = ctx.app.accounts.catalog()
    return ctx._json(200, {'platforms': platforms, 'report_types': sorted(REPORT_SPECS), 'workflows': [WeeklyOpsCouncil.WORKFLOW], 'action_operations': sorted(ActionService.OPERATIONS), 'report_recipe_types': ctx.app.report_recipes.catalog(), 'metric_materialization_report_types': sorted(SUPPORTED_REPORT_TYPES), **connector_catalog}, request_id)

@router.get('/v1/audit', summary='Read tenant audit events', tags=['audit'])
def get_v1_audit(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    parsed = urlparse(ctx.path)
    ctx.app.auth.require(principal, 'viewer')
    limit = int(parse_qs(parsed.query).get('limit', [100])[0])
    return ctx._json(200, {'events': ctx.app.db.list_audit(principal.tenant_id, limit)}, request_id)

@router.get('/v1/records', summary='Read tenant-owned connector records', tags=['records'])
def get_v1_records(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    parsed = urlparse(ctx.path)
    ctx.app.auth.require(principal, 'viewer')
    provider = parse_qs(parsed.query).get('provider', [None])[0]
    return ctx._json(200, {'records': ctx.app.db.list_records(principal.tenant_id, provider)}, request_id)

@router.get('/v1/metrics', summary='Read disposable process counters', tags=['metrics'])
def get_v1_metrics(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    ctx.app.auth.require(principal, 'viewer')
    return ctx._json(200, {'counters': ctx.app.metrics.snapshot()}, request_id)

@router.post('/v1/users', summary='Create a tenant user', tags=['users'])
def post_v1_users(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    body = ctx._body_fields(required={'email', 'role'})
    user = ctx.app.auth.create_user(principal, str(body['email']), str(body['role']))
    ctx.app.db.append_audit(principal.tenant_id, principal.user_id, request_id, 'user.create', 'user', str(user['id']), 'succeeded', {'role': user['role']})
    return ctx._json(201, {'user': user}, request_id)

@router.post('/v1/api-keys', summary='Issue a one-time API key for a tenant user', tags=['api-keys'])
def post_v1_api_keys(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    body = ctx._body_fields(required={'user_id'})
    user_id = str(body['user_id'])
    key = ctx.app.auth.issue_for_user(principal, user_id)
    ctx.app.db.append_audit(principal.tenant_id, principal.user_id, request_id, 'api_key.issue', 'api_key', None, 'succeeded', {'user_id': user_id})
    return ctx._json(201, {'api_key': key, 'user_id': user_id}, request_id)

@router.post('/v1/api-keys/rotate', summary="Rotate the current caller's API key", tags=['api-keys'])
def post_v1_api_keys_rotate(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    replacement = ctx.app.auth.rotate_current(principal)
    ctx.app.db.append_audit(principal.tenant_id, principal.user_id, request_id, 'api_key.rotate', 'api_key', principal.api_key_id, 'succeeded', {})
    return ctx._json(200, {'api_key': replacement}, request_id)

@router.post('/v1/api-keys/{keyId}/revoke', summary='Revoke a tenant API key', tags=['api-keys'])
def post_v1_api_keys_keyId_revoke(ctx: HTTPContext=Depends(get_context), keyId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    key_id = keyId
    ctx.app.auth.revoke(principal, key_id)
    ctx.app.db.append_audit(principal.tenant_id, principal.user_id, request_id, 'api_key.revoke', 'api_key', key_id, 'succeeded', {})
    return ctx._json(200, {'revoked': True, 'key_id': key_id}, request_id)

@router.post('/v1/connectors', summary='Create a tenant marketplace connector account', tags=['connectors'])
def post_v1_connectors(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    body = ctx._body_fields(required={'provider', 'external_account_id', 'config'})
    account = ctx.app.accounts.create(principal, provider=body['provider'], external_account_id=body['external_account_id'], config=body['config'], request_id=request_id)
    return ctx._json(201, account, request_id)

@router.post('/v1/connectors/{accountId}/health-check', summary='Run and persist a connector health check', tags=['connectors'])
def post_v1_connectors_accountId_health_check(ctx: HTTPContext=Depends(get_context), accountId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    body = ctx._body_fields(required=set(), allowed=set())
    account_id = accountId
    return ctx._json(200, ctx.app.accounts.health_check(principal, account_id, request_id=request_id), request_id)

@router.post('/v1/provider-smoke-tests', summary='Run an audited live provider connectivity test', tags=['provider-smoke-tests'])
def post_v1_provider_smoke_tests(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    body = ctx._body_fields(required={'provider'}, allowed={'provider', 'connector_account_id'})
    idempotency_key = ctx.headers.get('Idempotency-Key', '')
    if not idempotency_key:
        raise ValidationError('Idempotency-Key header is required')
    return ctx._json(201, ctx.app.provider_smoke.execute(principal, provider=body['provider'], connector_account_id=body.get('connector_account_id'), idempotency_key=idempotency_key, request_id=request_id), request_id)

@router.post('/v1/report-recipes', summary='Persist an Amazon report recipe', tags=['report-recipes'])
def post_v1_report_recipes(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    fields = {'connector_account_id', 'name', 'recipe_key', 'marketplace_ids', 'interval_minutes', 'lookback_days', 'enabled', 'next_run_at'}
    body = ctx._body_fields(required=fields)
    return ctx._json(201, ctx.app.report_recipes.create(principal, connector_account_id=body['connector_account_id'], name=body['name'], recipe_key=body['recipe_key'], marketplace_ids=body['marketplace_ids'], interval_minutes=body['interval_minutes'], lookback_days=body['lookback_days'], enabled=body['enabled'], next_run_at=body['next_run_at'], request_id=request_id), request_id)

@router.post('/v1/report-recipes/{recipeId}/sync', summary='Enqueue an idempotent Amazon report synchronization', tags=['report-recipes'])
def post_v1_report_recipes_recipeId_sync(ctx: HTTPContext=Depends(get_context), recipeId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    ctx._body_fields(required=set(), allowed=set())
    recipe_id = recipeId
    return ctx._json(202, ctx.app.report_syncs.enqueue(principal, recipe_id, ctx.headers.get('Idempotency-Key', ''), request_id), request_id)

@router.post('/v1/ads-capability-gates', summary='Run a read-only Amazon Ads capability gate', tags=['ads-capability-gates'])
def post_v1_ads_capability_gates(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    body = ctx._body_fields(required={'connector_account_id'}, allowed={'connector_account_id', 'attestation_reference'})
    return ctx._json(201, ctx.app.ads_gates.check(principal, body['connector_account_id'], body.get('attestation_reference'), ctx.headers.get('Idempotency-Key', ''), request_id), request_id)

@router.post('/v1/actions', summary='Request an idempotent action', tags=['actions'])
def post_v1_actions(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    body = ctx._body()
    result = ctx.app.actions.request(principal, str(body.get('operation', '')), body.get('payload', {}), ctx.headers.get('Idempotency-Key', ''), request_id)
    return ctx._json(200, result, request_id)

@router.post('/v1/actions/{actionId}/approve', summary='Approve a requested action as a second actor', tags=['actions'])
def post_v1_actions_actionId_approve(ctx: HTTPContext=Depends(get_context), actionId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    action_id = actionId
    return ctx._json(200, ctx.app.actions.approve(principal, action_id, request_id), request_id)

@router.post('/v1/actions/{actionId}/execute', summary='Execute an approved action', tags=['actions'])
def post_v1_actions_actionId_execute(ctx: HTTPContext=Depends(get_context), actionId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    action_id = actionId
    return ctx._json(200, ctx.app.actions.execute(principal, action_id, request_id), request_id)

@router.post('/v1/actions/{actionId}/retry', summary='Re-queue a failed or expired execution without bypassing approval', tags=['actions'])
def post_v1_actions_actionId_retry(ctx: HTTPContext=Depends(get_context), actionId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    action_id = actionId
    return ctx._json(200, ctx.app.actions.retry(principal, action_id, request_id), request_id)

@router.post('/v1/proposals', summary='Derive an idempotent proposal from an approved Daily Ops priority', tags=['proposals'])
def post_v1_proposals(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    body = ctx._body_fields(required={'daily_ops_run_id', 'priority_rank', 'operation', 'payload', 'risk', 'rollback_plan', 'expires_at'}, allowed={'daily_ops_run_id', 'priority_rank', 'operation', 'payload', 'risk', 'rollback_plan', 'expires_at'})
    return ctx._json(201, ctx.app.proposals.create(principal, daily_ops_run_id=body['daily_ops_run_id'], priority_rank=body['priority_rank'], operation=body['operation'], payload=body['payload'], risk=body['risk'], rollback_plan=body['rollback_plan'], idempotency_key=ctx.headers.get('Idempotency-Key', ''), expires_at=body['expires_at'], request_id=request_id), request_id)

@router.post('/v1/proposals/{proposalId}/submit', summary='Submit a proposal to local human review', tags=['proposals'])
def post_v1_proposals_proposalId_submit(ctx: HTTPContext=Depends(get_context), proposalId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    proposal_id = proposalId
    body = ctx._body_fields(required={'expected_version'}, allowed={'expected_version'})
    return ctx._json(200, ctx.app.proposals.submit(principal, proposal_id, request_id=request_id, **body), request_id)

@router.post('/v1/proposals/{proposalId}/decisions', summary='Record a local human review decision optimistically', tags=['proposals'])
def post_v1_proposals_proposalId_decisions(ctx: HTTPContext=Depends(get_context), proposalId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    proposal_id = proposalId
    body = ctx._body_fields(required={'expected_version', 'decision', 'comment'}, allowed={'expected_version', 'decision', 'comment'})
    return ctx._json(200, ctx.app.proposals.decide(principal, proposal_id, request_id=request_id, **body), request_id)

@router.post('/v1/proposals/{proposalId}/execute', summary='Idempotently execute an approved safe-read proposal', tags=['proposals'])
def post_v1_proposals_proposalId_execute(ctx: HTTPContext=Depends(get_context), proposalId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    proposal_id = proposalId
    body = ctx._body_fields(required={'expected_version'}, allowed={'expected_version'})
    return ctx._json(201, ctx.app.proposals.execute(principal, proposal_id, idempotency_key=ctx.headers.get('Idempotency-Key', ''), request_id=request_id, **body), request_id)

@router.post('/v1/proposals/{proposalId}/retry', summary='Retry a failed proposal execution idempotently on its existing durable execution row', tags=['proposals'])
def post_v1_proposals_proposalId_retry(ctx: HTTPContext=Depends(get_context), proposalId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    proposal_id = proposalId
    body = ctx._body_fields(required={'expected_version'}, allowed={'expected_version'})
    return ctx._json(201, ctx.app.proposals.retry(principal, proposal_id, idempotency_key=ctx.headers.get('Idempotency-Key', ''), request_id=request_id, **body), request_id)

@router.post('/v1/evidence-imports', summary='Parse and persist real marketplace CSV evidence', tags=['evidence-imports'])
def post_v1_evidence_imports(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    content_type = ctx.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
    csv_types = {'text/csv', 'text/tab-separated-values', 'application/csv'}
    xlsx_type = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    common = {'platform': ctx.headers.get('X-Evidence-Platform', ''), 'report_type': ctx.headers.get('X-Evidence-Type', ''), 'filename': ctx.headers.get('X-Evidence-Filename', ''), 'observed_at': ctx.headers.get('X-Evidence-Observed-At', ''), 'idempotency_key': ctx.headers.get('Idempotency-Key', ''), 'request_id': request_id}
    if content_type in csv_types:
        imported = ctx.app.evidence_imports.import_csv(principal, raw=ctx._raw_body(max_bytes=CSVIngestor.MAX_RAW_BYTES), media_type=content_type, **common)
    elif content_type == xlsx_type:
        imported = ctx.app.evidence_imports.import_xlsx(principal, raw=ctx._raw_body(max_bytes=XLSXIngestor.MAX_RAW_BYTES), sheet_name=ctx.headers.get('X-Evidence-Sheet') or None, **common)
    else:
        raise ValidationError('Content-Type must be CSV, TSV, or XLSX')
    return ctx._json(200, imported, request_id)

@router.post('/v1/evidence-imports/{importId}/metric-materialization', summary='Materialize durable metric observations from one tenant-owned Evidence import', tags=['evidence-imports'])
def post_v1_evidence_imports_importId_metric_materialization(ctx: HTTPContext=Depends(get_context), importId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    ctx._body_fields(required=set(), allowed=set())
    import_id = importId
    return ctx._json(200, ctx.app.metric_observations.materialize(principal, import_id, ctx.headers.get('Idempotency-Key', ''), request_id), request_id)

@router.post('/v1/metric-materializations/backfill', summary='Materialize a bounded page of existing tenant Evidence imports', tags=['metric-materializations'])
def post_v1_metric_materializations_backfill(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    body = ctx._body_fields(required={'limit'}, allowed={'limit', 'cursor'})
    return ctx._json(200, ctx.app.metric_observations.backfill(principal, limit=body['limit'], cursor=body.get('cursor'), request_id=request_id), request_id)

@router.post('/v1/agent-runs', summary='Create an idempotent Weekly Ops Council run', tags=['agent-runs'])
def post_v1_agent_runs(payload: AgentRunRequest, ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    body = payload.model_dump(exclude_unset=True)
    can_revise = getattr(ctx.app.agent_runs.provider, 'supports_requirement_revisions', True)
    revision_support = body.get('revision_support', can_revise)
    if revision_support and not can_revise:
        raise ValidationError('this provider cannot interpret requirement revisions; use a real LLM provider')
    run = ctx.app.agent_runs.request(principal, str(body['workflow']), body['objective'], body.get('evidence'), ctx.headers.get('Idempotency-Key', ''), request_id, evidence_import_ids=body.get('evidence_import_ids'), graph_version_id=body.get('graph_version_id'), metric_observation_ids=body.get('metric_observation_ids'), revision_support=revision_support)
    return ctx._json(200, run, request_id)

@router.post('/v1/daily-ops-schedules', summary='Create a Daily Ops schedule', tags=['daily-ops-schedules'])
def post_v1_daily_ops_schedules(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    body = ctx._body_fields(required={'name', 'platform', 'objective', 'timezone_name', 'local_time', 'graph_version_id', 'evidence_selectors'}, allowed={'name', 'platform', 'objective', 'timezone_name', 'local_time', 'graph_version_id', 'evidence_selectors', 'max_source_age_hours', 'enabled'})
    return ctx._json(201, ctx.app.daily_ops.create(principal, name=body['name'], platform=body['platform'], objective=body['objective'], timezone_name=body['timezone_name'], local_time=body['local_time'], graph_version_id=body['graph_version_id'], evidence_selectors=body['evidence_selectors'], max_source_age_hours=body.get('max_source_age_hours', 48), enabled=body.get('enabled', True), request_id=request_id), request_id)

@router.post('/v1/daily-ops-schedules/{scheduleId}/trigger', summary='Create or return the local-date Daily Ops occurrence', tags=['daily-ops-schedules'])
def post_v1_daily_ops_schedules_scheduleId_trigger(ctx: HTTPContext=Depends(get_context), scheduleId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    body = ctx._body_fields(required=set(), allowed={'local_date'})
    idempotency_key = ctx.headers.get('Idempotency-Key', '')
    if not idempotency_key:
        raise ValidationError('Idempotency-Key header is required')
    schedule_id = scheduleId
    return ctx._json(200, ctx.app.daily_ops.trigger(principal, schedule_id, request_id, local_date=body.get('local_date')), request_id)

@router.post('/v1/daily-ops-runs/{runId}/execute', summary='Execute one queued Daily Ops occurrence', tags=['daily-ops-runs'])
def post_v1_daily_ops_runs_runId_execute(ctx: HTTPContext=Depends(get_context), runId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    ctx._body_fields(required=set(), allowed=set())
    run_id = runId
    return ctx._json(200, ctx.app.daily_ops.execute(principal, run_id, request_id), request_id)

@router.post('/v1/daily-ops-runs/{runId}/retry', summary='Retry one failed or blocked Daily Ops occurrence', tags=['daily-ops-runs'])
def post_v1_daily_ops_runs_runId_retry(ctx: HTTPContext=Depends(get_context), runId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    ctx._body_fields(required=set(), allowed=set())
    idempotency_key = ctx.headers.get('Idempotency-Key', '')
    if not idempotency_key:
        raise ValidationError('Idempotency-Key header is required')
    run_id = runId
    return ctx._json(200, ctx.app.daily_ops.retry(principal, run_id, request_id), request_id)

@router.post('/v1/agent-graphs', summary='Create an immutable-versioned agent graph definition', tags=['agent-graphs'])
def post_v1_agent_graphs(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    body = ctx._body_fields(required={'name', 'definition'})
    return ctx._json(201, ctx.app.agent_graphs.create(principal, body['name'], body['definition'], request_id), request_id)

@router.post('/v1/agent-graphs/{graphId}/versions', summary='Create a new draft graph version', tags=['agent-graphs'])
def post_v1_agent_graphs_graphId_versions(ctx: HTTPContext=Depends(get_context), graphId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    graph_id = graphId
    body = ctx._body_fields(required={'definition'})
    return ctx._json(201, ctx.app.agent_graphs.create_version(principal, graph_id, body['definition'], request_id), request_id)

@router.post('/v1/agent-graph-versions/{versionId}/publish', summary='Publish a graph version after validation and review', tags=['agent-graph-versions'])
def post_v1_agent_graph_versions_versionId_publish(ctx: HTTPContext=Depends(get_context), versionId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    version_id = versionId
    ctx._body_fields(required=set(), allowed=set())
    version = ctx.app.agent_graphs.get_version(principal, version_id)
    return ctx._json(200, ctx.app.agent_graphs.publish(principal, version['graph_id'], version_id, request_id), request_id)

@router.post('/v1/agent-graphs/{graphId}/versions/{versionId}/publish', summary='Publish a graph version using the nested compatibility route', tags=['agent-graphs'])
def post_v1_agent_graphs_graphId_versions_versionId_publish(ctx: HTTPContext=Depends(get_context), graphId: str=Path(...), versionId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    ctx._body_fields(required=set(), allowed=set())
    return ctx._json(200, ctx.app.agent_graphs.publish(principal, graphId, versionId, request_id), request_id)

@router.post('/v1/agent-runs/{runId}/follow-ups', summary='Create a new run from feedback on a completed and approved parent', tags=['agent-runs'])
def post_v1_agent_runs_runId_follow_ups(payload: FollowUpRequest, ctx: HTTPContext=Depends(get_context), runId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    run_id = runId
    body = payload.model_dump(exclude_unset=True)
    return ctx._json(200, ctx.app.agent_runs.follow_up(principal, run_id, body['feedback'], ctx.headers.get('Idempotency-Key', ''), request_id, objective=body.get('objective'), evidence=body.get('evidence'), evidence_import_ids=body.get('evidence_import_ids')), request_id)

@router.post('/v1/agent-runs/{runId}/execute', summary='Execute the bound Domain Agent Graph and independent Reviewer', tags=['agent-runs'])
def post_v1_agent_runs_runId_execute(ctx: HTTPContext=Depends(get_context), runId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    run_id = runId
    return ctx._json(200, ctx.app.agent_runs.execute(principal, run_id, request_id), request_id)

@router.post('/v1/agent-runs/{runId}/evaluate', summary='Evaluate a completed Weekly Ops run with deterministic graders', tags=['agent-runs'])
def post_v1_agent_runs_runId_evaluate(ctx: HTTPContext=Depends(get_context), runId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    run_id = runId
    return ctx._json(200, ctx.app.evaluator.evaluate(principal, run_id, request_id), request_id)

@router.post('/v1/jobs', summary='Enqueue an agent run for background execution', tags=['jobs'])
def post_v1_jobs(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    body = ctx._body_fields(required={'run_id'}, allowed={'run_id', 'max_attempts'})
    return ctx._json(200, ctx.app.jobs.enqueue_agent_run(principal, str(body['run_id']), ctx.headers.get('Idempotency-Key', ''), request_id, max_attempts=int(body.get('max_attempts', 3))), request_id)

@router.post('/v1/schedules', summary='Schedule repeated Weekly Ops runs', tags=['schedules'])
def post_v1_schedules(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    body = ctx._body_fields(required={'name', 'objective', 'interval_minutes', 'next_run_at'}, allowed={'name', 'objective', 'evidence_import_ids', 'evidence_selectors', 'interval_minutes', 'next_run_at'})
    return ctx._json(201, ctx.app.schedules.create(principal, name=str(body['name']), objective=str(body['objective']), evidence_import_ids=body.get('evidence_import_ids', []), evidence_selectors=body.get('evidence_selectors', []), interval_minutes=int(body['interval_minutes']), next_run_at=str(body['next_run_at']), request_id=request_id), request_id)

@router.post('/v1/assurance-runs', summary='Run a tenant assurance evaluation or security check', tags=['assurance-runs'])
def post_v1_assurance_runs(ctx: HTTPContext=Depends(get_context)):
    principal = ctx.principal
    request_id = ctx.request_id
    body = ctx._body_fields(required={'kind'}, allowed={'kind'})
    key = ctx.headers.get('Idempotency-Key', '')
    if not key:
        raise ValidationError('Idempotency-Key header is required')
    return ctx._json(201, ctx.app.assurance.run(principal, str(body['kind']), key, request_id), request_id)

@router.patch('/v1/users/{userId}', summary="Change a tenant user's role", tags=['users'])
def patch_v1_users_userId(ctx: HTTPContext=Depends(get_context), userId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    user_id = userId
    body = ctx._body_fields(required={'role'})
    user = ctx.app.auth.update_user_role(principal, user_id, str(body['role']))
    ctx.app.db.append_audit(principal.tenant_id, principal.user_id, request_id, 'user.role_update', 'user', user_id, 'succeeded', {'role': user['role']})
    return ctx._json(200, {'user': user}, request_id)

@router.patch('/v1/connectors/{accountId}', summary='Update a tenant marketplace connector account', tags=['connectors'])
def patch_v1_connectors_accountId(ctx: HTTPContext=Depends(get_context), accountId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    account_id = accountId
    body = ctx._body_fields(required={'config'}, allowed={'external_account_id', 'config'})
    existing = ctx.app.accounts.get(principal, account_id)
    return ctx._json(200, ctx.app.accounts.update(principal, account_id, external_account_id=body.get('external_account_id', existing['external_account_id']), config=body['config'], request_id=request_id), request_id)

@router.patch('/v1/report-recipes/{recipeId}', summary='Update a tenant-owned Amazon report recipe', tags=['report-recipes'])
def patch_v1_report_recipes_recipeId(ctx: HTTPContext=Depends(get_context), recipeId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    recipe_id = recipeId
    fields = {'name', 'recipe_key', 'marketplace_ids', 'interval_minutes', 'lookback_days', 'enabled', 'next_run_at'}
    body = ctx._body_fields(required=fields)
    return ctx._json(200, ctx.app.report_recipes.update(principal, recipe_id, name=body['name'], recipe_key=body['recipe_key'], marketplace_ids=body['marketplace_ids'], interval_minutes=body['interval_minutes'], lookback_days=body['lookback_days'], enabled=body['enabled'], next_run_at=body['next_run_at'], request_id=request_id), request_id)

@router.patch('/v1/proposals/{proposalId}', summary='Revise a draft or revision-required proposal optimistically', tags=['proposals'])
def patch_v1_proposals_proposalId(ctx: HTTPContext=Depends(get_context), proposalId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    proposal_id = proposalId
    body = ctx._body_fields(required={'expected_version'}, allowed={'expected_version', 'title', 'rationale', 'expected_impact', 'rollback_plan', 'operation', 'payload', 'risk', 'expires_at'})
    return ctx._json(200, ctx.app.proposals.revise(principal, proposal_id, request_id=request_id, **body), request_id)

@router.patch('/v1/daily-ops-schedules/{scheduleId}', summary='Replace a Daily Ops schedule configuration', tags=['daily-ops-schedules'])
def patch_v1_daily_ops_schedules_scheduleId(ctx: HTTPContext=Depends(get_context), scheduleId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    schedule_id = scheduleId
    body = ctx._body_fields(required={'name', 'platform', 'objective', 'timezone_name', 'local_time', 'graph_version_id', 'evidence_selectors', 'max_source_age_hours', 'enabled'}, allowed={'name', 'platform', 'objective', 'timezone_name', 'local_time', 'graph_version_id', 'evidence_selectors', 'max_source_age_hours', 'enabled'})
    return ctx._json(200, ctx.app.daily_ops.update(principal, schedule_id, name=body['name'], platform=body['platform'], objective=body['objective'], timezone_name=body['timezone_name'], local_time=body['local_time'], graph_version_id=body['graph_version_id'], evidence_selectors=body['evidence_selectors'], max_source_age_hours=body['max_source_age_hours'], enabled=body['enabled'], request_id=request_id), request_id)

@router.patch('/v1/schedules/{scheduleId}', summary='Enable or disable a schedule', tags=['schedules'])
def patch_v1_schedules_scheduleId(ctx: HTTPContext=Depends(get_context), scheduleId: str=Path(...)):
    principal = ctx.principal
    request_id = ctx.request_id
    schedule_id = scheduleId
    body = ctx._body_fields(required={'enabled'})
    if not isinstance(body['enabled'], bool):
        raise ValidationError('enabled must be boolean')
    return ctx._json(200, ctx.app.schedules.set_enabled(principal, schedule_id, body['enabled'], request_id), request_id)
