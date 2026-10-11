import logging
import os

import uvicorn

from .web.app import create_app

logging.basicConfig(level=os.environ.get("WS_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("WS_PORT", "8080")), proxy_headers=True)
