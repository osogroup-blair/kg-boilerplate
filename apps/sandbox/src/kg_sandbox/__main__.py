import logging

import uvicorn

from kg_sandbox.app import create_app

logging.basicConfig(level=logging.INFO)
uvicorn.run(create_app(), host="0.0.0.0", port=8020, proxy_headers=True, forwarded_allow_ips="*", log_level="info")  # noqa: S104
