"""Voice: voice pair debugger for AWS.

Run: uv run bot.py
Then open http://localhost:7860/client in your browser.
"""

import sys
import logging

from loguru import logger

# Suppress all pipecat debug/warning noise; only show errors
logger.remove()
logger.add(sys.stderr, level="ERROR")

# Silence uvicorn access logs
logging.getLogger("uvicorn.access").setLevel(logging.ERROR)
logging.getLogger("uvicorn.error").setLevel(logging.ERROR)

from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.transports.base_transport import TransportParams

from voice.pipeline import run_bot

transport_params = {
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
    ),
}


async def bot(runner_args: RunnerArguments):
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
