from argus_core.model_trace import ModelTrace, ModelTraceReport
from fastapi import APIRouter

from argus_api.access import require_task_readable
from argus_api.deps import AgentDep, CallerDep, DbDep
from argus_api.modeltrace import service
from argus_api.tasks import service as tasks

router = APIRouter(prefix="/tasks", tags=["models"])


@router.post("/{task_id}/models")
async def report_models(
    task_id: str, body: ModelTraceReport, db: DbDep, agent_id: AgentDep
) -> ModelTrace:
    """Report the model executions and routing decisions of the attempt the caller
    holds (same lease fence as progress). Idempotent per `key`: a re-sent record
    completes the stored one and never overwrites it (409 on a contradiction)."""
    async with db.write() as tx:
        await tasks.check_lease(tx, task_id, body.attempt, agent_id)
        for e in body.executions:
            await service.record_execution(tx, agent_id, task_id, body.attempt, e)
        for s in body.selections:
            await service.record_selection(tx, agent_id, task_id, body.attempt, s)
        return await service.get_trace(tx.conn, task_id)


@router.get("/{task_id}/models")
async def get_models(task_id: str, db: DbDep, caller: CallerDep) -> ModelTrace:
    """Every execution (both layers, all attempts, oldest first) with its evidence,
    and the selection/switch timeline. Cards use the compact `models` summary."""
    async with db.read() as conn:
        await require_task_readable(conn, caller, task_id)
        return await service.get_trace(conn, task_id)
