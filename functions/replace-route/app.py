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

# Waiting time for SSM to start commands.
SSM_TIMEOUT_SECONDS = 30

# Whether or not use IPv6.
DEFAULT_HAS_IPV6 = True


# Overrides socket.getaddrinfo to perform IPv4 lookups
# See https://github.com/chime/terraform-aws-alternat/issues/87
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


def replace_route(route_table_id, new_route_table):
    try:
        logger.info("Replacing existing route %s for route table %s", route_table_id, new_route_table)
        ec2_client.replace_route(**new_route_table)
    except botocore.exceptions.ClientError as error:
        logger.error("Unable to replace route")
        raise error

def run_nat_instance_diagnostics(instance_id):
    """
    Runs a basic diagnostic script via SSM on the NAT instance.
    It checks if IP forwarding is enabled and lists the nftables NAT configuration.
    Returns True if configuration is healthy, False otherwise.
    """
    ssm_client = boto3.client("ssm")

    diagnostic_script = [
        "#!/bin/bash",
        "set -e",
        "echo 'ip_forward='$(cat /proc/sys/net/ipv4/ip_forward)",
        "echo 'nft_nat_table='$(nft list table ip nat 2>/dev/null || echo 'nftables nat table not found')"
    ]

    try:
        response = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": diagnostic_script},
            Comment="Run NAT instance diagnostics",
            TimeoutSeconds=SSM_TIMEOUT_SECONDS
        )
        command_id = response['Command']['CommandId']
        time.sleep(5)  # Allow some time for the command to execute

        invocation = ssm_client.get_command_invocation(
            CommandId=command_id,
            InstanceId=instance_id,
        )

        output = invocation.get('StandardOutputContent', '')

        if invocation.get('StandardErrorContent'):
            logger.warning("NAT instance diagnostic errors:\n%s", invocation['StandardErrorContent'])

        # Check conditions
        if "ip_forward=0" in output:
            logger.warning("NAT instance has ip_forward=0 — IP forwarding is disabled.")
            return False

        if "masquerade" not in output:
            logger.warning("NAT instance nftables missing 'masquerade' rule — SNAT may be broken.")
            return False
        
        if is_source_dest_check_enabled(instance_id) is True:
            logger.warning("Source/destination check is ENABLED — this will break NAT functionality.")
            return False
        if is_source_dest_check_enabled(instance_id) is None:
            logger.warning("Skipping NAT restore due to error checking source/dest.")
            return False

        return True

    except botocore.exceptions.ClientError as e:
        logger.error("SSM diagnostic command failed: %s", str(e))
        return False

def is_source_dest_check_enabled(instance_id):
    ec2 = boto3.client('ec2')
    try:
        response = ec2.describe_instances(InstanceIds=[instance_id])
        attr = response['Reservations'][0]['Instances'][0].get('SourceDestCheck', True)
        return attr
    except Exception as e:
        logger.error(f"Error checking source/dest check: {e}")
        return None

def are_any_routes_pointing_to_nat_gateway(route_table_ids):
    ec2 = boto3.client('ec2')
    try:
        response = ec2.describe_route_tables(RouteTableIds=route_table_ids)
        for rtb in response.get('RouteTables', []):
            for route in rtb.get('Routes', []):
                if route.get('DestinationCidrBlock') == "0.0.0.0/0" and 'NatGatewayId' in route and route.get('State') == 'active':
                    return True
        return False
    except Exception as e:
        logger.error(f"Error checking NAT Gateway routes: {e}")
        return False

def attempt_nat_instance_restore():
    ssm_client = boto3.client('ssm')
    nat_instance_id = get_current_nat_instance_id(os.getenv("NAT_ASG_NAME"))
    route_tables = os.getenv("ROUTE_TABLE_IDS_CSV", "").split(",")

    if not nat_instance_id or not route_tables:
        logger.warning("NAT_INSTANCE_ID or ROUTE_TABLE_IDS_CSV not set. Skipping NAT restore.")
        return

    logger.info("Attempting to restore route to NAT Instance: %s", nat_instance_id)

    try:
        check_urls = os.getenv("CHECK_URLS", ",".join(DEFAULT_CHECK_URLS)).split(",")
        commands = []
        for url in check_urls:
            command = f"curl -s -o /dev/null -w '%{{http_code}}\\n' --max-time 5 {url.strip()}"
            commands.append(command)
        # Send SSM command to test connectivity
        response = ssm_client.send_command(
            InstanceIds=[nat_instance_id],
            DocumentName="AWS-RunShellScript",
            TimeoutSeconds=SSM_TIMEOUT_SECONDS,
            Parameters={
                "commands": commands
            },
            Comment="Check Internet access from NAT instance via Lambda",
        )
        command_id = response['Command']['CommandId']
        time.sleep(5)  # Wait briefly before checking command result

        # Poll command result
        invocation = ssm_client.get_command_invocation(
            CommandId=command_id,
            InstanceId=nat_instance_id,
        )
        if invocation['Status'] == "Success":
            output = invocation['StandardOutputContent'].strip()
            http_codes = output.splitlines()
            if all(code == "200" for code in http_codes):
                logger.info("NAT instance has Internet access, we can diagnose the NAT configuration.")
                try:
                    if not run_nat_instance_diagnostics(nat_instance_id):
                        logger.warning("Skipping route restore due to failed NAT diagnostics.")
                        return
                except Exception as diag_error:
                    logger.error("Unexpected error during NAT diagnostics: %s", str(diag_error))
                    return
                for rtb in route_tables:
                    replace_route(rtb, { "DestinationCidrBlock": "0.0.0.0/0", "InstanceId": nat_instance_id, "RouteTableId": rtb })
                    logger.info("Route table %s now points to NAT instance %s", rtb, nat_instance_id)
                return
            else:
                logger.warning("Invocation output: %s", invocation['StandardOutputContent'])
        else:
            logger.warning("NAT instance connectivity test failed or did not return expected result.")
            

    except botocore.exceptions.ClientError as e:
        logger.error("SSM command failed: %s", str(e))
    except Exception as ex:
        logger.error("Unexpected error during NAT restore: %s", str(ex))

def check_connection(check_urls):
    """
    Checks connectivity to check_urls. If any of them succeed, return success.
    If all fail, replaces the route table to point at a standby NAT Gateway and
    return failure.
    """
    success = False
    for url in check_urls:
        try:
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'alternat/1.0')
            urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
            logger.debug("Successfully connected to %s", url)
            success = True
        except urllib.error.HTTPError as error:
            logger.warning("Response error from %s: %s, treating as success", url, error)
            success = True
        except urllib.error.URLError as error:
            logger.error("error connecting to %s: %s", url, error)
        except socket.timeout as error:
            logger.error("timeout error connecting to %s: %s", url, error)

    route_tables = "ROUTE_TABLE_IDS_CSV" in os.environ and os.getenv("ROUTE_TABLE_IDS_CSV").split(",")
    if not route_tables:
        raise MissingEnvironmentVariableError("ROUTE_TABLE_IDS_CSV")
    
    if success:
        if are_any_routes_pointing_to_nat_gateway(route_tables):
            attempt_nat_instance_restore()  # Try to restore NAT instance only if we have NAT gateway
        else:
            logger.info("Connectivity OK and already using NAT instance — no action needed.")
        return True

    logger.warning("Failed connectivity tests! Replacing route")

    public_subnet_id = os.getenv("PUBLIC_SUBNET_ID")
    if not public_subnet_id:
        raise MissingEnvironmentVariableError("PUBLIC_SUBNET_ID")

    vpc_id = get_vpc_id(route_tables[0])

    nat_gateway_id = get_nat_gateway_id(vpc_id, public_subnet_id)

    for rtb in route_tables:
        replace_route(rtb, { "DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": nat_gateway_id, "RouteTableId": rtb })
        logger.info("Route replacement succeeded")
    return False

def get_current_nat_instance_id(asg_name):
    autoscaling = boto3.client("autoscaling")

    try:
        response = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
        instances = response['AutoScalingGroups'][0]['Instances']
        for instance in instances:
            if instance['LifecycleState'] == 'InService':
                return instance['InstanceId']
    except Exception as e:
        logger.error(f"Failed to retrieve NAT instance ID from ASG {asg_name}: {e}")
        return None

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
        replace_route(rtb, { "DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": nat_gateway_id, "RouteTableId": rtb })
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
