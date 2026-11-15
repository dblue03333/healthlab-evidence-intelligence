import logging

import pytest

from healthlab.common import configure_logging


@pytest.fixture
def healthlab_logger():
    logger = logging.getLogger("healthlab")
    handlers, level = list(logger.handlers), logger.level
    yield logger
    logger.handlers[:] = handlers
    logger.setLevel(level)


def test_diagnostics_go_to_stderr_and_stdout_stays_for_results(
    healthlab_logger, monkeypatch, capsys
):
    monkeypatch.delenv("HEALTHLAB_LOG_LEVEL", raising=False)
    configure_logging()
    logging.getLogger("healthlab.data.pilot").info("Collection %s is running", "abc")
    out, err = capsys.readouterr()
    assert out == ""
    assert "INFO healthlab.data.pilot: Collection abc is running" in err


def test_configure_logging_is_idempotent_and_reads_the_level(healthlab_logger, monkeypatch):
    monkeypatch.setenv("HEALTHLAB_LOG_LEVEL", "warning")
    configure_logging()
    configure_logging()
    assert healthlab_logger.level == logging.WARNING
    assert len(healthlab_logger.handlers) == 1


def test_unknown_log_level_fails_loudly(healthlab_logger, monkeypatch):
    monkeypatch.setenv("HEALTHLAB_LOG_LEVEL", "chatty")
    with pytest.raises(ValueError, match="CHATTY"):
        configure_logging()
