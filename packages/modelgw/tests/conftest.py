import uuid

import pytest
from kg_modelgw.gateway import Correlation

from tests.support.router_env import Env


@pytest.fixture()
def env() -> Env:
    return Env()


@pytest.fixture()
def corr() -> Correlation:
    return Correlation(str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()), "kg-worker")
