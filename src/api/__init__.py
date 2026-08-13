"""Track B: HTTP transport, application wiring, and server bootstrap.

This package holds the framework-agnostic HTTP contracts (:mod:`api.http`), the
request-dispatch application (:mod:`api.app`), and a stdlib-only server
(:mod:`api.server`). It depends on :mod:`routes`, :mod:`controllers`,
:mod:`middleware`, and :mod:`services`, but never the other way around.
"""

from __future__ import annotations

from api.app import Application, build_app
from api.http import Request, Response

__all__ = ["Application", "Request", "Response", "build_app"]
