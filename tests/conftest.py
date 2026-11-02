"""Test-session setup.

The server logs to ``~/.zbbx-mcp`` by default. A test run is not a use of the
server, and its thousands of synthetic starts and calls made the real
analytics useless for answering "which tools are slow" (ADR 160), so every
test — including the subprocess server tests, which inherit the environment —
logs to a throwaway directory instead.
"""

import atexit
import os
import shutil
import tempfile

_LOG_DIR = tempfile.mkdtemp(prefix="zbbx-test-logs-")
os.environ["ZABBIX_LOG_FILE"] = os.path.join(_LOG_DIR, "zbbx-mcp.log")
os.environ["ZABBIX_ANALYTICS_FILE"] = os.path.join(_LOG_DIR, "analytics.log")
atexit.register(shutil.rmtree, _LOG_DIR, ignore_errors=True)
