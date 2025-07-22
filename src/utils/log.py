import logging
from loguru import logger


logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


logger.disable("httpx")
logger.disable("httpcore")
