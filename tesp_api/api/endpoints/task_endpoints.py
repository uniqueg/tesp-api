import datetime

from pymonad.maybe import Just, Nothing
from bson.objectid import ObjectId
from pymonad.promise import Promise
from fastapi.params import Depends
from fastapi import APIRouter, Body
from fastapi.responses import Response, JSONResponse

from tesp_api.api.authz import Principal, get_authz, service_id
from tesp_api.api.error import api_handle_error
from tesp_api.service.event_dispatcher import dispatch_event
from tesp_api.repository.task_repository import task_repository
from tesp_api.repository.model.task import TesTask, RegisteredTesTask, TesTaskState, TesTaskLog, TesTaskView
from tesp_api.utils.functional import maybe_of, identity_with_side_effect
from tesp_api.api.model.task_service_info import TesServiceInfo, TesServiceType, TesServiceOrganization
from tesp_api.api.model.response_models import \
    TesGetAllTasksResponseModel,\
    TesCreateTaskResponseModel, \
    RegisteredTesTaskSchema,\
    TesGetAllTasksResponseSchema
from tesp_api.api.endpoints.endpoint_utils import \
    response_from_model, \
    descriptions, \
    view_query_params, \
    get_view, \
    list_query_params, resource_not_found_response, authorized_principal

from tesp_api.utils.commons import Commons


router = APIRouter()


@router.post("/tasks",
             responses={200: {"description": "OK"}},
             response_model=TesCreateTaskResponseModel,
             description=descriptions["tasks-create"])
async def create_task(
        principal: Principal = Depends(authorized_principal),
        tes_task: TesTask = Body(...)
        ) -> Response:
    # TesTask has no id, state, logs, creation_time or author: client values are dropped.
    await get_authz().check_create(principal, tes_task)
    token_subject = principal.id
    task_to_create = RegisteredTesTask(
        **tes_task.dict(),
        state=TesTaskState.QUEUED,
        logs=[TesTaskLog(logs=[], outputs=[], system_logs=[])],
        creation_time=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        author=token_subject)
    return await task_repository.create_task(task_to_create)\
        .map(lambda task_id: identity_with_side_effect(
            task_id, lambda _task_id: dispatch_event(
                "queued_task",
                payload={
                    "task_id": _task_id,
                    "author": token_subject
                }
            )
        )).map(lambda task_id: response_from_model(TesCreateTaskResponseModel(id=str(task_id))))\
        .catch(api_handle_error)


@router.get("/tasks/{id}",
            responses={
                200: {"description": "Ok"},
                404: {"description": "Not found"}},
            response_model=RegisteredTesTaskSchema,
            description=descriptions["tasks-get"])
async def get_task(
        id: str,
        principal: Principal = Depends(authorized_principal),
        query_params: dict = Depends(view_query_params)
        ) -> Response:
    try:
        meta = await task_repository.get_task_meta(id)
    except Exception as error:
        return api_handle_error(error)
    view = query_params['view'] or TesTaskView.MINIMAL
    # Asked for missing tasks too, so that missing and foreign tasks look alike.
    allowed = await get_authz().may_read(principal, id, meta, view.value)
    if meta is None or not allowed:
        return resource_not_found_response(Just(f"Task[{id}] not found"))
    return await Promise(lambda resolve, reject: resolve((
            Nothing,
            {'_id': ObjectId(id)}
        ))).then(lambda get_tasks_args: task_repository.get_task(*get_tasks_args))\
        .map(lambda found_task: found_task.maybe(
            resource_not_found_response(Just(f"Task[{id}] not found")),
            lambda _task: response_from_model(_task, get_view(query_params['view']))
        )).catch(api_handle_error)


@router.get("/tasks",
            responses={200: {"description": "Ok"}},
            response_model=TesGetAllTasksResponseSchema,
            description=descriptions["tasks-get-all"])
async def get_tasks(
        principal: Principal = Depends(authorized_principal),
        query_params: dict = Depends(list_query_params)
        ) -> Response:
    view = query_params['view'] or TesTaskView.MINIMAL
    list_all = await get_authz().lists_all(principal, view.value)
    return await Promise(lambda resolve, reject: resolve((
            Nothing if list_all else maybe_of(principal.id),
            maybe_of(query_params['page_size']),
            maybe_of(query_params['page_token']).map(lambda _p_token: ObjectId(_p_token)),
            maybe_of(query_params['name_prefix']).map(lambda _name_prefix: {'name': {'$regex': f"^{_name_prefix}"}}))))\
        .then(lambda get_tasks_args: task_repository.get_tasks(*get_tasks_args))\
        .map(lambda tasks_and_token: response_from_model(
            TesGetAllTasksResponseModel(
                next_page_token=str(tasks_and_token[1]) if tasks_and_token[1] else None,
                tasks=list(map(lambda task: task.dict(**get_view(query_params['view'])), tasks_and_token[0])))
        )).catch(api_handle_error)


@router.post("/tasks/{id}:cancel",
             responses={200: {"description": "Ok"}},
             description=descriptions["tasks-delete"],)
async def cancel_task(
        id: str,
        principal: Principal = Depends(authorized_principal),
        ) -> Response:
    try:
        meta = await task_repository.get_task_meta(id)
    except Exception as error:
        return api_handle_error(error)
    await get_authz().check_cancel(principal, id, meta)
    return await Promise(lambda resolve, reject: resolve((
            Nothing,
            ObjectId(id)
    ))).then(lambda get_tasks_args: task_repository.cancel_task(*get_tasks_args))\
        .map(lambda task_id_maybe: 
             # Use JSONResponse to ensure the body is "{}"
             JSONResponse(content={}, status_code=200)
        )\
        .catch(api_handle_error)


@router.get("/service-info",
            responses={200: {"description": "Ok"}},
            description=descriptions["service-info"],
            response_model=TesServiceInfo)
async def get_service_info() -> TesServiceInfo: # FastAPI directly handles Pydantic model return
    return TesServiceInfo(
        id=service_id(),
        name="TESP",
        type=TesServiceType(
            group="org.ga4gh",
            artifact="tes",
            version="1.0.0"
        ),
        description="GA4GH TES Server implementation for Pulsar",
        organization=TesServiceOrganization(
            name="Faculty of Informatics, Masaryk University",
            url="https://www.fi.muni.cz/"
        ),
        contactUrl="https://www.fi.muni.cz/",
        documentationUrl="https://www.fi.muni.cz/",
        createdAt="2021-10-26T00:00:00Z",
        updatedAt="2021-10-26T00:00:00Z",
        environment="dev",
        version=Commons.get_service_version(),
        storage=["https://www.fi.muni.cz/"]
    )
