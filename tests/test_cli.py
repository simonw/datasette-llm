from datasette.cli import cli
from click.testing import CliRunner
import json
import pytest
import sqlite3


def test_llm_commands():
    runner = CliRunner()
    result = runner.invoke(cli, ["llm", "logs", "path"])
    assert result.exit_code == 0
    assert result.output.endswith("/logs.db\n")


@pytest.mark.parametrize("logs_db_exists", (False, True))
def test_startup_loads_llm_db_if_available(monkeypatch, tmpdir, logs_db_exists):
    monkeypatch.setenv("LLM_USER_PATH", str(tmpdir))
    if logs_db_exists:
        path = str(tmpdir / "logs.db")
        sqlite3.connect(path).execute("create table logs (id integer primary key)")
    runner = CliRunner()
    result = runner.invoke(cli, ["--get", "/.json"])
    info = json.loads(result.output)
    if logs_db_exists:
        assert "llm" in info
    else:
        assert "llm" not in info
