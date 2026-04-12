# -*- coding: utf-8 -*-
"""camper-mcp pyRevit extension startup script.

Executed automatically by pyRevit when the extension loads.  Registers all
API routes defined in lib/mcp_routes.py with the pyRevit Routes server.
"""

from pyrevit.coreutils.logger import get_logger

logger = get_logger(__name__)

try:
    # Importing the routes module triggers @api.route decorator registration
    import mcp_routes  # noqa: F401

    logger.info("[camper-mcp] All routes registered successfully. "
                "API version: %s", mcp_routes.API_VERSION)
except Exception as exc:
    import traceback
    msg = "[camper-mcp] FAILED to register routes: {}\n{}".format(
        exc, traceback.format_exc()
    )
    logger.error(msg)
    print(msg)
