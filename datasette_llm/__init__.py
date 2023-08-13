from datasette import hookimpl, Response
from datasette.database import Database
import llm
from llm.cli import cli as llm_cli
import pathlib


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
    return [(r"^/-/llm$", llm_index)]


async def llm_index(request, datasette):
    return Response.html(await datasette.render_template("llm.html", {
        "models": [{
            "model_id": ma.model.model_id,
            "name": str(ma.model)
        } for ma in llm.get_models_with_aliases()]
    }))
