from unittest.mock import MagicMock, patch

import pytest

import main
from agent.service import AgentError
from data_pipeline.config import DB_PATH

# ── helpers ───────────────────────────────────────────────────────────────────


def _set_env(monkeypatch, api_key="sk-test", model="gpt-4o"):
    if api_key is not None:
        monkeypatch.setenv("OPENAI_API_KEY", api_key)
    else:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    if model is not None:
        monkeypatch.setenv("OPENAI_MODEL", model)
    else:
        monkeypatch.delenv("OPENAI_MODEL", raising=False)


def _run_main_mocked(monkeypatch, *, analytics_error=None, analytics_counts=None):
    """Run main() with all external dependencies mocked. Returns call_order list."""
    call_order = []
    _set_env(monkeypatch)

    async def fake_pipeline(db_path):
        call_order.append(("pipeline", db_path))

    mock_analytics_inst = MagicMock()
    if analytics_error:
        mock_analytics_inst.run_all.side_effect = analytics_error
    else:

        def fake_run_all():
            call_order.append("analytics")
            return analytics_counts or {"analytics_expansion_scores": 5}

        mock_analytics_inst.run_all.side_effect = fake_run_all

    def fake_run_chat(_svc):
        call_order.append("chat")

    ctx = (
        patch("main.run_pipeline", side_effect=fake_pipeline),
        patch("main.SQLiteHandler"),
        patch("main.AviationDAL"),
        patch("main.AnalyticsService", return_value=mock_analytics_inst),
        patch("main.AgentTools"),
        patch("main.AgentService"),
        patch("main.run_chat", side_effect=fake_run_chat),
        patch("main.configure_logging"),
        patch("main.check_pipeline_warnings"),
    )

    with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7], ctx[8]:
        if analytics_error:
            with pytest.raises(SystemExit):
                main.main()
        else:
            main.main()

    return call_order


# ── validate_environment ──────────────────────────────────────────────────────


class TestValidateEnvironment:
    def test_missing_api_key_stops(self, monkeypatch):
        _set_env(monkeypatch, api_key=None)
        with pytest.raises(SystemExit):
            main.validate_environment()

    def test_missing_model_stops(self, monkeypatch):
        _set_env(monkeypatch, model=None)
        with pytest.raises(SystemExit):
            main.validate_environment()

    def test_empty_api_key_rejected(self, monkeypatch):
        _set_env(monkeypatch, api_key="")
        with pytest.raises(SystemExit):
            main.validate_environment()

    def test_empty_model_rejected(self, monkeypatch):
        _set_env(monkeypatch, model="")
        with pytest.raises(SystemExit):
            main.validate_environment()

    def test_whitespace_api_key_rejected(self, monkeypatch):
        _set_env(monkeypatch, api_key="   ")
        with pytest.raises(SystemExit):
            main.validate_environment()

    def test_whitespace_model_rejected(self, monkeypatch):
        _set_env(monkeypatch, model="   ")
        with pytest.raises(SystemExit):
            main.validate_environment()

    def test_valid_env_passes(self, monkeypatch):
        _set_env(monkeypatch)
        main.validate_environment()  # must not raise


# ── orchestration order ───────────────────────────────────────────────────────


class TestOrchestration:
    def test_pipeline_runs_before_analytics(self, monkeypatch):
        order = _run_main_mocked(monkeypatch)
        pipeline_idx = next(
            i for i, x in enumerate(order) if isinstance(x, tuple) and x[0] == "pipeline"
        )
        analytics_idx = order.index("analytics")
        assert pipeline_idx < analytics_idx

    def test_analytics_runs_before_chat(self, monkeypatch):
        order = _run_main_mocked(monkeypatch)
        assert order.index("analytics") < order.index("chat")

    def test_pipeline_receives_db_path(self, monkeypatch):
        order = _run_main_mocked(monkeypatch)
        pipeline_call = next(x for x in order if isinstance(x, tuple) and x[0] == "pipeline")
        assert pipeline_call[1] == DB_PATH

    def test_analytics_failure_prevents_chat(self, monkeypatch):
        order = _run_main_mocked(monkeypatch, analytics_error=RuntimeError("db fail"))
        assert "chat" not in order

    def test_missing_api_key_stops_before_ingestion(self, monkeypatch):
        _set_env(monkeypatch, api_key=None)
        call_order = []

        async def fake_pipeline(db_path):
            call_order.append("pipeline")

        with patch("main.run_pipeline", side_effect=fake_pipeline), patch("main.configure_logging"):
            with pytest.raises(SystemExit):
                main.main()

        assert "pipeline" not in call_order

    def test_missing_model_stops_before_ingestion(self, monkeypatch):
        _set_env(monkeypatch, model=None)
        call_order = []

        async def fake_pipeline(db_path):
            call_order.append("pipeline")

        with patch("main.run_pipeline", side_effect=fake_pipeline), patch("main.configure_logging"):
            with pytest.raises(SystemExit):
                main.main()

        assert "pipeline" not in call_order


# ── partial pipeline failures ─────────────────────────────────────────────────


class TestPartialPipelineFailures:
    def test_partial_failure_shows_warning(self, capsys):
        from data_pipeline.config import REFRESH_HOURS

        mock_dal = MagicMock()
        failing_dataset = next(iter(REFRESH_HOURS))
        mock_dal.get_sync_state.side_effect = lambda name: (
            {"status": "error"} if name == failing_dataset else {"status": "ok"}
        )
        main.check_pipeline_warnings(mock_dal)
        out = capsys.readouterr().out
        assert failing_dataset in out

    def test_partial_failure_does_not_prevent_analytics(self, monkeypatch):
        call_order = []
        _set_env(monkeypatch)

        mock_dal_inst = MagicMock()
        mock_dal_inst.get_sync_state.return_value = {"status": "error"}

        mock_analytics_inst = MagicMock()

        def fake_run_all():
            call_order.append("analytics")
            return {"t": 1}

        mock_analytics_inst.run_all.side_effect = fake_run_all

        async def fake_pipeline(db_path):
            pass

        with (
            patch("main.run_pipeline", side_effect=fake_pipeline),
            patch("main.SQLiteHandler"),
            patch("main.AviationDAL", return_value=mock_dal_inst),
            patch("main.AnalyticsService", return_value=mock_analytics_inst),
            patch("main.AgentTools"),
            patch("main.AgentService"),
            patch("main.run_chat"),
            patch("main.configure_logging"),
        ):
            main.main()

        assert "analytics" in call_order


# ── run_chat ──────────────────────────────────────────────────────────────────


class TestRunChat:
    def _make_service(self, response="Answer."):
        svc = MagicMock()
        svc.respond.return_value = response
        return svc

    def test_exit_stops_loop(self):
        svc = self._make_service()
        with patch("builtins.input", side_effect=["exit"]):
            main.run_chat(svc)
        svc.respond.assert_not_called()

    def test_quit_stops_loop(self):
        svc = self._make_service()
        with patch("builtins.input", side_effect=["quit"]):
            main.run_chat(svc)
        svc.respond.assert_not_called()

    def test_exit_case_insensitive(self):
        svc = self._make_service()
        with patch("builtins.input", side_effect=["EXIT"]):
            main.run_chat(svc)
        svc.respond.assert_not_called()

    def test_quit_case_insensitive(self):
        svc = self._make_service()
        with patch("builtins.input", side_effect=["QUIT"]):
            main.run_chat(svc)
        svc.respond.assert_not_called()

    def test_empty_input_skips_service(self):
        svc = self._make_service()
        with patch("builtins.input", side_effect=["", "  ", "exit"]):
            main.run_chat(svc)
        svc.respond.assert_not_called()

    def test_valid_question_calls_respond(self):
        svc = self._make_service()
        with patch("builtins.input", side_effect=["Which airports?", "exit"]):
            main.run_chat(svc)
        svc.respond.assert_called_once()
        assert svc.respond.call_args[0][0] == "Which airports?"

    def test_successful_exchange_added_to_history(self):
        svc = MagicMock()
        svc.respond.return_value = "Answer."
        with patch("builtins.input", side_effect=["Q1", "Q2", "exit"]):
            main.run_chat(svc)
        second_history = svc.respond.call_args_list[1][0][1]
        assert {"role": "user", "content": "Q1"} in second_history
        assert {"role": "assistant", "content": "Answer."} in second_history

    def test_followup_receives_previous_history(self):
        history_sizes: list[int] = []

        def capture(msg, hist):
            history_sizes.append(len(hist))
            return "Answer."

        svc = MagicMock()
        svc.respond.side_effect = capture
        with patch("builtins.input", side_effect=["Q1", "Q2", "exit"]):
            main.run_chat(svc)
        assert history_sizes[0] == 0  # first call had empty history
        assert history_sizes[1] == 2  # second call received Q1 + answer

    def test_agent_error_displayed_safely(self, capsys):
        svc = MagicMock()
        svc.respond.side_effect = [AgentError("rate limit reached"), "Fine."]
        with patch("builtins.input", side_effect=["Bad Q", "Good Q", "exit"]):
            main.run_chat(svc)
        out = capsys.readouterr().out
        assert "rate limit reached" in out

    def test_agent_error_loop_continues(self):
        svc = MagicMock()
        svc.respond.side_effect = [AgentError("fail"), "OK answer"]
        with patch("builtins.input", side_effect=["Q1", "Q2", "exit"]):
            main.run_chat(svc)
        assert svc.respond.call_count == 2

    def test_failed_exchange_not_added_to_history(self):
        history_sizes: list[tuple[str, int]] = []

        def capture(msg, hist):
            history_sizes.append((msg, len(hist)))
            if msg == "Q1":
                raise AgentError("fail")
            return "OK"

        svc = MagicMock()
        svc.respond.side_effect = capture
        with patch("builtins.input", side_effect=["Q1", "Q2", "exit"]):
            main.run_chat(svc)
        q2_history_size = next(size for msg, size in history_sizes if msg == "Q2")
        assert q2_history_size == 0  # Q1 failed, so history was not updated

    def test_eof_exits_cleanly(self, capsys):
        svc = self._make_service()
        with patch("builtins.input", side_effect=EOFError):
            main.run_chat(svc)
        out = capsys.readouterr().out
        assert "Goodbye" in out

    def test_keyboard_interrupt_exits_cleanly(self, capsys):
        svc = self._make_service()
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            main.run_chat(svc)
        out = capsys.readouterr().out
        assert "Goodbye" in out

    def test_goodbye_printed_on_exit(self, capsys):
        svc = self._make_service()
        with patch("builtins.input", side_effect=["exit"]):
            main.run_chat(svc)
        assert "Goodbye" in capsys.readouterr().out
