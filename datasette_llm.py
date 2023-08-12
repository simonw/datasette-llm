from datasette import hookimpl
from llm.cli import cli as llm_cli


@hookimpl
def register_commands(cli):
    cli.add_command(llm_cli, name="llm")
