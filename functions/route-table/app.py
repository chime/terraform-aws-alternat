import json
import logging
import time

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

LIFECYCLE_KEY = "LifecycleHookName"
ASG_KEY = "AutoScalingGroupName"
EC2_KEY = "EC2InstanceId"

def handler(event, context):
    try:
        logger.info(json.dumps(event))
        for record in event["Records"]:
            message = json.loads(record["Sns"]["Message"])
            if LIFECYCLE_KEY in message and ASG_KEY in message:
                life_cycle_hook = message[LIFECYCLE_KEY]
                auto_scaling_group = message[ASG_KEY]
                instance_id = message[EC2_KEY]
                logger.info("LIFECYLE_HOOK: %s\nAUTO_SCALING_GROUP: %s\nINSANCE_ID: %s", life_cycle_hook, auto_scaling_group, instance_id)

    except Exception as e:
        logging.error("Error: %s", str(e))
