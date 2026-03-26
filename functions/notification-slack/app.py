import os
import json
import urllib.request
import logging

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)
logging.getLogger('boto3').setLevel(logging.CRITICAL)
logging.getLogger('botocore').setLevel(logging.CRITICAL)


_cached_webhook_url = None


def _get_slack_webhook_url():
    # Speed up the function by caching the webhook URL for warm starts
    global _cached_webhook_url
    if _cached_webhook_url:
        return _cached_webhook_url

    secret_name = os.getenv("SLACK_WEBHOOK_URL_SECRET_NAME")
    if not secret_name:
        raise RuntimeError("SLACK_WEBHOOK_URL_SECRET_NAME environment variable is not set.")

    secrets_client = boto3.client("secretsmanager")
    response = secrets_client.get_secret_value(SecretId = secret_name)
    _cached_webhook_url = response["SecretString"]
    return _cached_webhook_url


def lambda_handler(event, context):
    """
    Lambda function to receive SNS notifications and forward them to Slack via webhook.
    The Slack webhook URL is retrieved from AWS Secrets Manager.
    """
    slack_webhook_url = _get_slack_webhook_url()
    
    # Loop through SNS records in case of multiple notifications
    for record in event.get("Records", []):
        sns_msg = record.get("Sns", {})
        subject = sns_msg.get("Subject", "Notification")
        message = sns_msg.get("Message", "")
        
        slack_message = {
            "text": f"*{subject if subject else 'Notification'}*\n{message}"
        }
        logger.info(f"Slack message: {slack_message}")
        
        data = json.dumps(slack_message).encode("utf-8")
        req = urllib.request.Request(
            slack_webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        try:
            logger.info("Sending request to Slack...")
            with urllib.request.urlopen(req) as resp:
                if resp.status != 200:
                    logger.error(f"Request to Slack returned error {resp.status}: {resp.reason}")
                    raise RuntimeError(f"Request to Slack returned error {resp.status}: {resp.reason}")
                logger.info(f"Request to Slack successful")
        except Exception as e:
            logger.error(f"Failed to send message to Slack: {e}")
            raise e
    logger.info(f"All messages sent to Slack")
    return {"status": "success"}
