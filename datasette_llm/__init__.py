from datasette import hookimpl, Response
from datasette.database import Database
import datetime
import llm
from llm.cli import cli as llm_cli
import pathlib
from sqlite_utils import Database as SqliteUtilsDatabase


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


@hookimpl
def register_routes():
    return [
        (r"^/-/llm$", llm_index),
        # Capture conversation_id
        (r"^/-/llm/start$", llm_start),
        (r"^/-/llm/(?P<conversation_id>[0-9a-z]+)$", llm_conversation),
    ]


async def llm_conversation(request, datasette):
    conversation_id = request.url_vars["conversation_id"]
    return Response.text("Conversation {}".format(conversation_id))


async def llm_index(request, datasette):
    return Response.html(
        await datasette.render_template(
            "llm.html",
            {
                "models": [
                    {"model_id": ma.model.model_id, "name": str(ma.model)}
                    for ma in llm.get_models_with_aliases()
                ],
                "start_path": datasette.urls.path("/-/llm/start"),
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
                "model_id": model_id,
                "prompt": prompt,
                "system": system,
                "actor_id": request.actor.get("id") if request.actor else None,
                "datetime_utc": datetime.datetime.utcnow().isoformat(),
            },
            pk="id",
        )

    await datasette.get_database("llm").execute_write_fn(store)
    return Response.redirect(datasette.urls.path("/-/llm/{}".format(conversation.id)))
