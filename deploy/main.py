# GridQ App Lab entry point: starts gridq-io (FastAPI + SSE on :8000), which spawns gridq-brain.
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

from gridq.io.app import main  # noqa: E402

main()
