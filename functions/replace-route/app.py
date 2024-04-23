import os
import json
import logging
import time
import urllib
import socket

import botocore
import boto3


logger = logging.getLogger()
logger.setLevel(logging.INFO)
logging.getLogger('boto3').setLevel(logging.CRITICAL)
logging.getLogger('botocore').setLevel(logging.CRITICAL)


ec2_client = boto3.client("ec2")

LIFECYCLE_KEY = "LifecycleHookName"
ASG_KEY = "AutoScalingGroupName"
EC2_KEY = "EC2InstanceId"

# Checks every CONNECTIVITY_CHECK_INTERVAL seconds, exits after 1 minute
DEFAULT_CONNECTIVITY_CHECK_INTERVAL = "5"

# Which URLs to check for connectivity
DEFAULT_CHECK_URLS = ["https://www.example.com", "https://www.google.com"]

# The timeout for the connectivity checks.
REQUEST_TIMEOUT = 5

# Whether or not use IPv6.
DEFAULT_HAS_IPV6 = True


# Overrides socket.getaddrinfo to perform IPv4 lookups
# See https://github.com/1debit/alternat/issues/87
def disable_ipv6():
    prv_getaddrinfo = socket.getaddrinfo
    def getaddrinfo_ipv4(*args):
        modified_args = (args[0], args[1], socket.AF_INET) + args[3:]
        res = prv_getaddrinfo(*modified_args)
        return res
    socket.getaddrinfo = getaddrinfo_ipv4


def get_az_and_vpc_zone_identifier(auto_scaling_group):
    autoscaling = boto3.client("autoscaling")

    try:
        asg_objects = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[auto_scaling_group])
    except botocore.exceptions.ClientError as error:
        logger.error("Unable to describe autoscaling groups")
        raise error

    if asg_objects["AutoScalingGroups"] and len(asg_objects["AutoScalingGroups"]) > 0:
        asg = asg_objects["AutoScalingGroups"][0]
        logger.debug("Auto Scaling Group: %s", asg)

        availability_zone = asg["AvailabilityZones"][0]
        logger.debug("Availability Zone: %s", availability_zone)

        vpc_zone_identifier = asg["VPCZoneIdentifier"]
        logger.debug("VPC zone identifier: %s", vpc_zone_identifier)

        return availability_zone, vpc_zone_identifier

    raise MissingVPCZoneIdentifierError(asg_objects)


def get_vpc_id(route_table):
    try:
        route_tables = ec2_client.describe_route_tables(RouteTableIds=[route_table])
    except botocore.exceptions.ClientError as error:
        logger.error("Unable to get vpc id")
        raise error
    if "RouteTables" in route_tables and len(route_tables["RouteTables"]) == 1:
        vpc_id = route_tables["RouteTables"][0]["VpcId"]
        logger.debug("VPC ID: %s", vpc_id)
    return vpc_id


def get_nat_gateway_id(vpc_id, subnet_id):
    nat_gateway_id = os.getenv("NAT_GATEWAY_ID")
    if nat_gateway_id:
        logger.info("Using NAT_GATEWAY_ID env. variable (%s)", nat_gateway_id)
        return nat_gateway_id

    try:
        nat_gateways = ec2_client.describe_nat_gateways(
            Filters=[
                {
                    "Name": "vpc-id",
                    "Values": [vpc_id]
                },
                {
                    "Name": "subnet-id",
                    "Values": [subnet_id]
                },
            ]
        )
    except botocore.exceptions.ClientError as error:
        logger.error("Unable to describe nat gateway")
        raise error

    logger.debug("NAT Gateways: %s", nat_gateways)
    if len(nat_gateways.get("NatGateways")) < 1:
        raise MissingNatGatewayError(nat_gateways)

    nat_gateway_id = nat_gateways['NatGateways'][0]["NatGatewayId"]
    logger.debug("NAT Gateway ID: %s", nat_gateway_id)
    return nat_gateway_id


def replace_route(route_table_id, nat_gateway_id):
    new_route_table = {
        "DestinationCidrBlock": "0.0.0.0/0",
        "NatGatewayId": nat_gateway_id,
        "RouteTableId": route_table_id
    }
    try:
        logger.info("Replacing existing route %s for route table %s", route_table_id, new_route_table)
        ec2_client.replace_route(**new_route_table)
    except botocore.exceptions.ClientError as error:
        logger.error("Unable to replace route")
        raise error


def check_connection(check_urls):
    """
    Checks connectivity to check_urls. If any of them succeed, return success.
    If all fail, replaces the route table to point at a standby NAT Gateway and
    return failure.
    """
    for url in check_urls:
        try:
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'alternat/1.0')
            urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
            logger.debug("Successfully connected to %s", url)
            return True
        except urllib.error.HTTPError as error:
            logger.warning("Response error from %s: %s, treating as success", url, error)
            return True
        except urllib.error.URLError as error:
            logger.error("error connecting to %s: %s", url, error)
        except socket.timeout as error:
            logger.error("timeout error connecting to %s: %s", url, error)

    logger.warning("Failed connectivity tests! Replacing route")

    public_subnet_id = os.getenv("PUBLIC_SUBNET_ID")
    if not public_subnet_id:
        raise MissingEnvironmentVariableError("PUBLIC_SUBNET_ID")

    route_tables = "ROUTE_TABLE_IDS_CSV" in os.environ and os.getenv("ROUTE_TABLE_IDS_CSV").split(",")
    if not route_tables:
        raise MissingEnvironmentVariableError("ROUTE_TABLE_IDS_CSV")
    vpc_id = get_vpc_id(route_tables[0])

    nat_gateway_id = get_nat_gateway_id(vpc_id, public_subnet_id)

    for rtb in route_tables:
        replace_route(rtb, nat_gateway_id)
        logger.info("Route replacement succeeded")
    return False


def connectivity_test_handler(event, context):
    if not isinstance(event, dict):
        logger.error(f"Unknown event: {event}")
        return

    if event.get("source") != "aws.events":
        logger.error(f"Unable to handle unknown event type: {json.dumps(event)}")
        raise UnknownEventTypeError

    logger.debug("Starting NAT instance connectivity test")

    check_interval = int(os.getenv("CONNECTIVITY_CHECK_INTERVAL", DEFAULT_CONNECTIVITY_CHECK_INTERVAL))
    check_urls = "CHECK_URLS" in os.environ and os.getenv("CHECK_URLS").split(",") or DEFAULT_CHECK_URLS

    has_ipv6 = get_env_bool("HAS_IPV6", DEFAULT_HAS_IPV6)
    if not has_ipv6:
        disable_ipv6()

    # Run connectivity checks for approximately 1 minute
    run = 0
    num_runs = 60 / check_interval
    while run < num_runs:
        if check_connection(check_urls):
            time.sleep(check_interval)
            run += 1
        else:
            break


def get_env_bool(var_name, default_value=False):
    value = os.getenv(var_name, default_value)
    true_values = ["t", "true", "y", "yes", "1"]
    return str(value).lower() in true_values


def handler(event, _):
    try:
        for record in event["Records"]:
            message = json.loads(record["Sns"]["Message"])
            if LIFECYCLE_KEY in message and ASG_KEY in message:
                asg = message[ASG_KEY]
            else:
                logger.error("Failed to find lifecycle message to parse")
                raise LifecycleMessageError
    except Exception as error:
        logger.error("Error: %s", error)
        raise error

    availability_zone, vpc_zone_identifier = get_az_and_vpc_zone_identifier(asg)
    public_subnet_id = vpc_zone_identifier.split(",")[0]
    az = availability_zone.upper().replace("-", "_")
    route_tables = az in os.environ and os.getenv(az).split(",")
    if not route_tables:
        raise MissingEnvironmentVariableError
    vpc_id = get_vpc_id(route_tables[0])

    nat_gateway_id = get_nat_gateway_id(vpc_id, public_subnet_id)

    for rtb in route_tables:
        replace_route(rtb, nat_gateway_id)
        logger.info("Route replacement succeeded")


class UnknownEventTypeError(Exception): pass


class MissingVpcConfigError(Exception): pass


class MissingFunctionSubnetError(Exception): pass


class MissingAZSubnetError(Exception): pass


class MissingVPCZoneIdentifierError(Exception): pass


class MissingVPCandSubnetError(Exception): pass


class MissingNatGatewayError(Exception): pass


class MissingRouteTableError(Exception): pass


class LifecycleMessageError(Exception): pass


class MissingEnvironmentVariableError(Exception): pass
