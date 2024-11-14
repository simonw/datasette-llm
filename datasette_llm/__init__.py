import asyncio
from concurrent.futures import ThreadPoolExecutor
from datasette import hookimpl, Request, Response, NotFound
from datasette.database import Database
from datasette.utils import sqlite3
import datetime
import json
import llm
from llm.cli import cli as llm_cli
import pathlib
from sqlite_utils import Database as SqliteUtilsDatabase
from typing import Optional, Tuple


@hookimpl
def register_commands(cli):
    cli.add_command(llm_cli, name="llm")


@hookimpl
def startup(datasette):
    has_llm_db = False
    try:
        datasette.get_database("llm")
        has_llm_db = True
    except KeyError:
        pass

    config = datasette.plugin_config("llm") or {}
    db_path = config.get("db_path") or (llm.user_dir() / "logs.db")
    db_path = pathlib.Path(db_path)
    if db_path.exists() and not has_llm_db:
        datasette.add_database(Database(datasette, path=str(db_path)), name="llm")


async def validate(
    request: Request,
) -> Tuple[Optional[llm.AsyncModel], str, Optional[str], Optional[int]]:
    # Returns model_id, prompt, optional error string, optional error status code
    if request.method != "POST":
        return (None, "", "Must be a POST request", 405)
    try:
        body = await request.post_body()
        data = json.loads(body)
    except json.JSONDecodeError:
        return (None, "", "Invalid JSON", 400)
    model_id = data.get("model") or "gpt-4o-mini"
    if not model_id or not isinstance(model_id, str):
        return (None, "", "Model not provided", 400)
    try:
        model = llm.get_async_model(model_id)
    except llm.UnknownModelError:
        return (None, "", "Unknown model", 400)
    prompt = data.get("prompt")
    if not prompt or not isinstance(prompt, str):
        return (None, "", "Prompt not provided", 400)
    return (model, prompt, None, None)


def _error(msg, status=400):
    return Response.json({"error": msg}, status=status)


async def llm_chat(request):
    model, prompt, error, status = await validate(request)
    if error:
        return _error(error, status)
    try:
        response = await model.prompt(prompt)
    except Exception as ex:
        return _error(str(ex), 500)
    return Response.json(
        {
            "prompt": prompt,
            "response": await response.text(),
            "details": response.response_json,
        }
    )


async def llm_stream(request, send):
    model, prompt, error, status = await validate(request)
    if error:
        return _error(error, status)
    response = await model.prompt(prompt)
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                [b"Content-Type", b"text/event-stream"],
            ],
        }
    )
    async for chunk in response:
        await send(
            {
                "type": "http.response.body",
                "body": b"data: "
                + json.dumps({"text": chunk}).encode("utf-8")
                + b"\r\n\r\n",
                "more_body": True,
            }
        )
    await send(
        {
            "type": "http.response.body",
            "body": b"data: " + json.dumps({"done": True}).encode("utf-8"),
            "more_body": False,
        }
    )


@hookimpl
def register_routes():
    return [
        (r"^/-/llm$", llm_index),
        # API proxies
        (r"^/-/llm/chat$", llm_chat),
        (r"^/-/llm/stream$", llm_stream),
        # Capture conversation_id
        (r"^/-/llm/start$", llm_start),
        (r"^/-/llm/ws/(?P<conversation_id>[0-9a-z]+)$", llm_conversation_ws),
        (r"^/-/llm/(?P<conversation_id>[0-9a-z]+)$", llm_conversation),
    ]


async def llm_conversation_ws(request, scope, receive, send, datasette):
    if scope["type"] != "websocket":
        return Response.text("ws only", status=400)

    conversation_id = request.url_vars["conversation_id"]
    # Must have been initiated already
    initiated = (
        await datasette.get_database("llm").execute(
            "select * from initiated where id = :id", {"id": conversation_id}
        )
    ).first()
    if not initiated:
        return Response.text("Conversation not yet initiated", status=404)

    model_id = initiated["model"]
    db = datasette.get_database("llm")

    try:
        model = llm.get_model(model_id)
    except llm.UnknownModelError:
        return Response.text("Unknown model", status=400)

    if model.needs_key:
        model.key = llm.get_key(None, model.needs_key, model.key_env_var)

    first_prompt = None
    # If this conversation has not yet been started, start it
    has_messages = (
        await db.execute(
            "select 1 from responses where conversation_id = :id",
            {"id": conversation_id},
        )
    ).rows
    if not has_messages:
        first_prompt = initiated["prompt"]

    def run_in_thread(prompt, system=None):
        response = model.prompt(prompt, system=system)
        for chunk in response:
            yield chunk
        yield {"end": response}

    async def execute_prompt(prompt, send):
        async for item in async_wrap(run_in_thread, prompt)():
            if "error" in item:
                await send({"type": "websocket.send", "text": item["error"]})
            else:
                # It might be the 'end'
                if isinstance(item["item"], dict) and "end" in item["item"]:
                    # Log to the DB
                    response = item["item"]["end"]
                    await db.execute_write_fn(
                        lambda conn: response.log_to_db(SqliteUtilsDatabase(conn)),
                        block=False,
                    )
                else:
                    # Send the message to the client
                    await send({"type": "websocket.send", "text": item["item"]})
        await send({"type": "websocket.send", "text": "\n\n"})

    while True:
        event = await receive()
        if event["type"] == "websocket.connect":
            await send({"type": "websocket.accept"})
            if first_prompt:
                await execute_prompt(first_prompt, send)
            first_prompt = None
        elif first_prompt or (event["type"] == "websocket.receive"):
            if event["type"] == "websocket.receive":
                message = event["text"]
                decoded = json.loads(message)
                prompt = decoded["prompt"]
            else:
                prompt = first_prompt
                first_prompt = None
            await execute_prompt(prompt, send)

        elif event["type"] == "websocket.disconnect":
            break


async def llm_conversation(request, datasette):
    conversation_id = request.url_vars["conversation_id"]
    # It may have just been initiated but not yet started:
    try:
        initiated = (
            await datasette.get_database("llm").execute(
                "select * from initiated where id = :id", {"id": conversation_id}
            )
        ).first()
    except sqlite3.OperationalError:
        # Table has not been created yet
        initiated = None
    # Or there may be a conversation with responses:
    conversation = (
        await datasette.get_database("llm").execute(
            "select * from conversations where id = :id", {"id": conversation_id}
        )
    ).first()
    responses = (
        await datasette.get_database("llm").execute(
            "select * from responses where conversation_id = :id",
            {"id": conversation_id},
        )
    ).rows
    if not initiated and not conversation and not responses:
        raise NotFound("Conversation not found")

    model_id = conversation["model"] if conversation else initiated["model"]

    return Response.html(
        await datasette.render_template(
            "llm_conversation.html",
            {
                "model_id": model_id,
                "conversation_id": conversation_id,
                "conversation_title": (conversation["name"] if conversation else None)
                or "Untitled conversation",
                "responses": responses,
                "start_datetime_utc": (
                    responses[0]["datetime_utc"] if responses else None
                ),
                "ws_path": datasette.urls.path("/-/llm/ws/{}".format(conversation_id)),
                # If we expect WebSocket to start streaming in results straight away:
                "show_empty_response": bool(initiated),
                "first_prompt": initiated["prompt"] if initiated else None,
                "first_system_prompt": initiated["system"] if initiated else None,
            },
            request=request,
        )
    )


async def llm_index(request, datasette):
    db = datasette.get_database("llm")
    previous_conversations = await db.execute(
        """
        select
            conversations.id,
            conversations.name,
            conversations.model,
            min(responses.datetime_utc) as start_datetime_utc,
            count(responses.id) as num_responses
        from conversations
        left join responses on conversations.id = responses.conversation_id
        group by conversations.id, conversations.name, conversations.model
        order by conversations.id desc limit 200;
        """
    )
    return Response.html(
        await datasette.render_template(
            "llm.html",
            {
                "models": [
                    {"model_id": ma.model.model_id, "name": str(ma.model)}
                    for ma in llm.get_models_with_aliases()
                ],
                "start_path": datasette.urls.path("/-/llm/start"),
                "previous_conversations": previous_conversations.rows,
            },
            request=request,
        )
    )


async def llm_start(request, datasette):
    if request.method != "POST":
        return Response.redirect(datasette.urls.path("/-/llm"))
    form_data = await request.post_vars()
    try:
        model_id = form_data["model_id"]
        prompt = form_data["prompt"]
        system = form_data.get("system")
    except KeyError:
        datasette.add_message(
            request, "Invalid start to the conversation", type=datasette.ERROR
        )
        return Response.redirect(datasette.urls.path("/-/llm"))
    try:
        model = llm.get_model(model_id)
    except llm.UnknownModelError:
        datasette.add_message(request, "Unknown model", type=datasette.ERROR)
        return Response.redirect(datasette.urls.path("/-/llm"))
    # Create the conversation and redirect the user
    conversation = model.conversation()

    def store(conn):
        db = SqliteUtilsDatabase(conn)
        db["initiated"].insert(
            {
                "id": conversation.id,
                "model": model_id,
                "prompt": prompt,
                "system": system,
                "actor_id": request.actor.get("id") if request.actor else None,
                "datetime_utc": datetime.datetime.utcnow().isoformat(),
            },
            pk="id",
        )

    await datasette.get_database("llm").execute_write_fn(store)
    return Response.redirect(datasette.urls.path("/-/llm/{}".format(conversation.id)))


END_SIGNAL = object()


def async_wrap(generator_func, *args, **kwargs):
    def next_item(gen):
        try:
            return next(gen)
        except StopIteration:
            return END_SIGNAL

    async def async_generator():
        loop = asyncio.get_running_loop()
        generator = iter(generator_func(*args, **kwargs))
        with ThreadPoolExecutor() as executor:
            while True:
                try:
                    item = await loop.run_in_executor(executor, next_item, generator)
                    if item is END_SIGNAL:
                        break
                    yield {"item": item}
                except Exception as ex:
                    yield {"error": str(ex)}
                    break

    return async_generator
