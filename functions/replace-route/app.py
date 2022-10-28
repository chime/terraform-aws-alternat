import os
import json
import logging

import botocore
import boto3
import requests


logger = logging.getLogger()
logger.setLevel(logging.INFO)
logging.getLogger('boto3').setLevel(logging.CRITICAL)
logging.getLogger('botocore').setLevel(logging.CRITICAL)

ec2_client = boto3.client("ec2")

LIFECYCLE_KEY = "LifecycleHookName"
ASG_KEY = "AutoScalingGroupName"
EC2_KEY = "EC2InstanceId"
DEFAULT_SUBNET_SUFFIX = "private"


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


def get_vpc_and_subnet_id(subnet_suffix, asg_az, vpc_zone_identifier):
    try:
        subnets = ec2_client.describe_subnets(SubnetIds=[vpc_zone_identifier])

    except botocore.exceptions.ClientError as error:
        logger.error("Unable to get vpc and subnet id")
        raise error

    logger.debug("Subnets: %s", subnets)

    if subnets["Subnets"] and len(subnets["Subnets"]) > 0:
        logger.debug("Number of subnets: %s", len(subnets["Subnets"]))

        subnet = subnets["Subnets"][0]
        public_subnet_id = subnet["SubnetId"]
        logger.debug("Public subnet ID: %s", public_subnet_id)

        vpc_id = subnet["VpcId"]
        logger.debug("VPC ID: %s", vpc_id)
    else:
        raise MissingVPCandSubnetError(subnets)

    try:
        az_subnets = ec2_client.describe_subnets(
            Filters = [
                {
                    "Name": "availability-zone",
                    "Values": [
                        asg_az
                    ]
                },
                {
                    "Name": "vpc-id",
                    "Values": [
                        vpc_id
                    ]
                },
            ]
        )
    except botocore.exceptions.ClientError as error:
        logger.error("Unable to describe subnets")
        raise error

    if len(az_subnets.get("Subnets")) < 1:
        logger.error("Unable to find subnets associated with AZ! Cannot replace route.")
        raise MissingAZSubnetError(az_subnets)

    private_subnet_id = ""
    for subnet in az_subnets.get("Subnets"):
        tags = subnet.get("Tags")
        for tag in tags:
            if tag.get("Key") == "Name":
                subnet_name = tag.get("Value")
                if f"{subnet_suffix}-{asg_az}" in subnet_name:
                    private_subnet_id = subnet.get("SubnetId")
                    break

    if private_subnet_id == "":
        logger.error("Unable to find the private subnet ID for %s! Cannot replace route.", asg_az)
        raise MissingAZSubnetError(az_subnets)

    logger.debug("Private subnet ID: %s", private_subnet_id)
    return vpc_id, private_subnet_id, public_subnet_id


def get_vpc_and_subnets_from_lambda(function_name):
    """
    This function operates as follows:
    - Get the Lambda function currently being executed
    - Read the VPC config of the function
    - Find the VPC and subnet IDs of the function
    - Use the VPC and subnet ID to deduce which AZ the function runs in
    - Use the VPC ID and AZ to deduce which corresponding public subnet the NAT Gateway is in
    - Return the VPC ID, private subnet ID of the Lambda, and public subnet ID of the NAT
    Gateway for use in replacing the route.
    """
    boto_lambda = boto3.client("lambda")
    try:
        func = boto_lambda.get_function(FunctionName=function_name)
    except botocore.exceptions.ClientError as error:
        logger.error("Unable to get Lambda function")
        raise error

    logger.info(func)
    vpc_config = func.get("Configuration").get("VpcConfig")
    if vpc_config == "":
        logger.error("Unable to read VpcConfig from function")
        raise MissingVpcConfigError(func.get("Configuration"))

    subnet_ids = vpc_config.get("SubnetIds")
    if len(subnet_ids) != 1:
        logger.error("Unable to find single subnet ID for this function! Cannot replace route.")
        raise MissingFunctionSubnetError(vpc_config)
    subnet_id = subnet_ids[0]

    try:
        # Due to a limitation in how moto returns Lambda Vpc configuration, we cannot
        # get vpc_id from the get_function() response above.
        # See https://github.com/spulec/moto/blob/59910c812e3008506a5b8d7841d88e8bf4e4e153/moto/awslambda/models.py#L484
        # Instead, make a second call to describe_subnets and filter on the subnet-id which is reliable.
        vpc_id = ec2_client.describe_subnets(Filters=[
            {
                "Name": "subnet-id",
                "Values": [
                    subnet_id
                ]
            }
        ]).get("Subnets")[0]["VpcId"]
        if vpc_id == "":
            logger.error("Could not discover VpcId from Lambda subnet")
            raise MissingVpcConfigError(vpc_config)

        lambda_subnet = ec2_client.describe_subnets(
            Filters=[
                {
                    "Name": "subnet-id",
                    "Values": [
                        subnet_id
                    ]
                },
                {
                    "Name": "vpc-id",
                    "Values": [
                        vpc_id
                    ]
                },
            ]
        )
    except botocore.exceptions.ClientError as error:
        logger.error("Unable to describe subnets")
        raise error

    lambda_subnets = lambda_subnet.get("Subnets")
    if len(lambda_subnets) != 1:
        logger.error("Unable to describe Lambda subnet ID! Cannot replace route.")
        raise MissingAZSubnetError(lambda_subnet)
    if "AvailabilityZone" not in lambda_subnets[0]:
        logger.error("Unable to find AZ of lambda function subnet! Cannot replace route.")
        raise MissingAZSubnetError(lambda_subnets)
    availability_zone = lambda_subnets[0]["AvailabilityZone"]
    lambda_subnet_id = lambda_subnets[0].get("SubnetId")

    try:
        az_subnets = ec2_client.describe_subnets(
            Filters=[
                {
                    "Name": "availability-zone",
                    "Values": [
                        availability_zone
                    ]
                },
                {
                    "Name": "vpc-id",
                    "Values": [
                        vpc_id
                    ]
                },
            ]
        )
    except botocore.exceptions.ClientError as error:
        logger.error("Unable to describe subnets")
        raise error

    if len(az_subnets.get("Subnets")) < 1:
        logger.error("Unable to find subnets associated with AZ! Cannot replace route.")
        raise MissingAZSubnetError(az_subnets)

    public_subnet_id = ""
    for subnet in az_subnets.get("Subnets"):
        tags = subnet.get("Tags")
        for tag in tags:
            if tag.get("Key") == "Name":
                subnet_name = tag.get("Value")
                if f"public-{availability_zone}" in subnet_name:
                    public_subnet_id = subnet.get("SubnetId")
                    break

    if public_subnet_id == "":
        logger.error("Unable to find the public subnet ID for %s! Cannot replace route.", availability_zone)
        raise MissingAZSubnetError(az_subnets)

    logger.debug("Found subnet %s in VPC %s", public_subnet_id, vpc_id)
    return vpc_id, public_subnet_id, lambda_subnet_id


def get_nat_gateway_id(vpc_id, subnet_id):
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


def describe_and_replace_route(subnet_id, nat_gateway_id):
    try:
        route_tables = ec2_client.describe_route_tables(
            Filters=[{
                "Name": "association.subnet-id",
                "Values": [subnet_id]
            }]
        )
    except botocore.exceptions.ClientError as error:
        logger.error("Unable to describe route tables")
        raise error

    if len(route_tables.get("RouteTables")) < 1:
        raise MissingRouteTableError(route_tables)

    route_table = route_tables['RouteTables'][0]

    new_route_table = {"DestinationCidrBlock": "0.0.0.0/0",
                       "NatGatewayId": nat_gateway_id,
                       "RouteTableId": route_table["RouteTableId"]}
    try:
        logger.info("Replacing existing route %s for route table %s", route_table, new_route_table)
        ec2_client.replace_route(**new_route_table)
    except botocore.exceptions.ClientError as error:
        logger.error("Unable to replace route")
        raise error


def handler(event, _):
    subnet_suffix = os.getenv("PRIVATE_SUBNET_SUFFIX", DEFAULT_SUBNET_SUFFIX)

    try:
        for record in event["Records"]:
            message = json.loads(record["Sns"]["Message"])
            if LIFECYCLE_KEY in message and ASG_KEY in message:
                life_cycle_hook = message[LIFECYCLE_KEY]
                auto_scaling_group = message[ASG_KEY]
                instance_id = message[EC2_KEY]
                logger.info("Handling Auto Scaling Group termination event for instance %s", instance_id)
                logger.debug("Lifecycle Hook: %s", life_cycle_hook)
                logger.debug("Auto Scaling Group: %s", auto_scaling_group)

                availability_zone, vpc_zone_identifier = get_az_and_vpc_zone_identifier(auto_scaling_group)
                vpc_id, private_subnet_id, public_subnet_id = get_vpc_and_subnet_id(subnet_suffix, availability_zone, vpc_zone_identifier)
                nat_gateway_id = get_nat_gateway_id(vpc_id, public_subnet_id)
                describe_and_replace_route(private_subnet_id, nat_gateway_id)

                logger.info("Route replacement succeeded")
                return

        logger.error("Failed to find lifecyle message to parse")
        raise LifecycleMessageError
    except Exception as error:
        logger.error("Error: %s", error)
        raise error


def connectivity_test_handler(event, context):
    if event.get("source") != "aws.events":
        logger.error("Unable to handle unknown event type: %s", json.dumps(event))
        raise UnknownEventTypeError

    logger.info("Starting NAT instance connectivity test")

    try:
        requests.get("https://www.example.com", timeout=5)
        logger.info("Successfully connected to www.example.com")
        return
    except requests.exceptions.RequestException as error:
        logger.error("alternat-connectivity-test error connecting to example.com: %s", error)

    try:
        requests.get("https://www.google.com", timeout=5)
        logger.info("Successfully connected to www.google.com")
        return
    except requests.exceptions.RequestException as error:
        logger.error("alternat-connectivity-test error connecting to google.com: %s", error)

    logger.warning("Failed connectivity tests! Replacing route")

    vpc_id, public_subnet_id, lambda_subnet_id = get_vpc_and_subnets_from_lambda(context.function_name)
    nat_gateway_id = get_nat_gateway_id(vpc_id, public_subnet_id)
    describe_and_replace_route(lambda_subnet_id, nat_gateway_id)


class UnknownEventTypeError(Exception): pass


class MissingVpcConfigError(Exception): pass


class MissingFunctionSubnetError(Exception): pass


class MissingAZSubnetError(Exception): pass


class MissingVPCZoneIdentifierError(Exception): pass


class MissingVPCandSubnetError(Exception): pass


class MissingNatGatewayError(Exception): pass


class MissingRouteTableError(Exception): pass


class LifecycleMessageError(Exception): pass
