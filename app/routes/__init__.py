"""HTTP routes. Each handler validates input, calls one service module and returns a response."""

from typing import Annotated

from fastapi import Depends, Request

from app.auth import require_csrf, require_session, require_writable
from app.fsutil import DataDir


def _data_dir(request: Request) -> DataDir:
    return request.app.state.data_dir


DataDirDep = Annotated[DataDir, Depends(_data_dir)]

# Every /api route except login: a valid session, and the CSRF header on mutations.
PROTECTED = [Depends(require_session), Depends(require_csrf)]
# Added to each route that changes data (all mutations except login and logout).
WRITABLE = [Depends(require_writable)]
