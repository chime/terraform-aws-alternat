import json
import logging
import time

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

def handler(event, context):
    try:
        logger.info(json.dumps(event))
    except Exception as e:
        logging.error("Error: %s", str(e))