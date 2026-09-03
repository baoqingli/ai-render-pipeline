# tests/unit/conftest.py
"""Shared stubs for unit tests.

`tests/unit/test_comfy_client.py` (kept verbatim from the task brief) calls
`ComfyClient.upload_image("local.png")`, and the client opens that path
relative to the CWD. The repo intentionally has no such file, so this
fixture provides a stub for the duration of the unit-test session and
removes it afterwards, keeping the working tree pristine.
"""
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _local_upload_stub():
    stub = Path("local.png")
    stub.write_bytes(b"stub-image")
    try:
        yield
    finally:
        stub.unlink(missing_ok=True)


@pytest.fixture(scope="session", autouse=True)
def _direct_api_output_cleanup():
    """Remove the DirectAPIEngine output written to the CWD.

    `tests/unit/test_direct_api.py` (kept verbatim from the task brief) uses a
    RenderTask with `control_maps={}`, so the engine falls back to an out dir
    of "." and writes v1_var0_nano-banana-2_1.png into the repo root. Removed
    afterwards to keep the working tree pristine.
    """
    yield
    Path("v1_var0_nano-banana-2_1.png").unlink(missing_ok=True)
