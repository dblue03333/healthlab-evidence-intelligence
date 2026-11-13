import pytest

# gold-editorial-17 is the last version with an AI review step. Tests of that review path
# pin the runtime to it; archived v17 runs keep exactly these rules.
REVIEW_RUNTIME = "gold-editorial-17"


@pytest.fixture
def review_runtime(monkeypatch):
    from healthlab.editorial import runs

    monkeypatch.setattr(runs, "VERSION", REVIEW_RUNTIME)
    return REVIEW_RUNTIME
