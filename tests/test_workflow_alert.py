"""Exercise the actual dependency-free failure notification, without sending it."""

from io import BytesIO
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock
from urllib.error import URLError
from urllib.parse import parse_qs

import pytest

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"


def alert_script():
    workflow = (WORKFLOWS / "weekly.yml").read_text()
    script = workflow.split("python3 - <<'PY'\n", 1)[1].rsplit("          PY", 1)[0]
    return compile(dedent(script), "weekly-failure-alert", "exec")


@pytest.fixture
def delivery(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-secret")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("RUN_URL", "https://github.com/slowdata/peel/actions/runs/1")
    send = MagicMock(return_value=BytesIO(b'{"ok":true}'))
    monkeypatch.setattr("urllib.request.urlopen", send)
    return send


def test_alert_is_one_plain_text_message(delivery, capsys):
    exec(alert_script(), {})
    delivery.assert_called_once()
    request = delivery.call_args.args[0]
    assert request.get_method() == "POST"
    assert delivery.call_args.kwargs == {"timeout": 20}
    assert parse_qs(request.data.decode()) == {
        "chat_id": ["123"],
        "text": [
            "Peel: a execução semanal falhou.\nhttps://github.com/slowdata/peel/actions/runs/1"
        ],
    }
    assert "Alerta de falha enviado." in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["network", "rejected", "invalid-json"])
def test_alert_never_retries_or_prints_credentials(delivery, capsys, failure):
    if failure == "network":
        delivery.side_effect = URLError("https://api.telegram.org/bottest-secret/sendMessage")
    else:
        delivery.return_value = BytesIO(b'{"ok":false}' if failure == "rejected" else b"bad")
    with pytest.raises(SystemExit, match="envio não repetido") as exc:
        exec(alert_script(), {})
    delivery.assert_called_once()
    output = capsys.readouterr()
    assert "test-secret" not in str(exc.value) + output.out + output.err
    assert "Alerta de falha enviado." not in output.out


def test_alert_missing_secret_does_not_send(delivery, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    with pytest.raises(SystemExit, match="configuração Telegram em falta"):
        exec(alert_script(), {})
    delivery.assert_not_called()


def test_ci_and_weekly_share_the_same_test_gate():
    for name in ("ci.yml", "weekly.yml"):
        assert "uses: ./.github/workflows/tests.yml" in (WORKFLOWS / name).read_text()
    weekly = (WORKFLOWS / "weekly.yml").read_text()
    assert "    needs: tests\n" in weekly
    assert "needs: [tests, run]" in weekly
    assert (
        "always() && (needs.tests.result == 'failure' || needs.run.result == 'failure')" in weekly
    )
