"""Browser routes.

This file is kept as a thin re-export layer so other parts of the codebase
(e.g. backend/server.py lifespan cleanup) can keep importing:

  from routes.browser import router, session_manager

All implementation is in routes/browser_core.py.
"""

from .browser_core import router, session_manager, cleanup_sessions
