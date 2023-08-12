from datasette.cli import cli
from click.testing import CliRunner


def test_llm_commands():
    runner = CliRunner()
    result = runner.invoke(cli, ["llm", "logs", "path"])
    assert result.exit_code == 0
    assert result.output.endswith("/logs.db\n")
