"""Fixtures shared by the test modules."""

from __future__ import annotations

import sys

import pytest

from win_harness import Place, _params, _sandbox_unavailable


@pytest.fixture(scope="session", params=_params())
def place(request):
    if sys.platform != "win32":
        pytest.skip("Windows only")
    if request.param == "sandbox":
        why = _sandbox_unavailable()
        if why:
            pytest.skip(why)
    p = Place(request.param)
    yield p
    p.close()
