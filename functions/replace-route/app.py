import json
import logging
import time

import boto3
import sys
import requests


logger = logging.getLogger()
logger.setLevel(logging.INFO)
logging.getLogger('boto3').setLevel(logging.CRITICAL)
logging.getLogger('botocore').setLevel(logging.CRITICAL)


AUTOSCALING_FUNC_NAME = "ha-nat-autoscaling-hook"
SCHEDULED_FUNC_NAME = "ha-nat-connectivity-tester"
LIFECYCLE_KEY = "LifecycleHookName"
ASG_KEY = "AutoScalingGroupName"
EC2_KEY = "EC2InstanceId"
autoscaling = boto3.client("autoscaling")
ec2 = boto3.client("ec2")
boto_lambda = boto3.client("lambda")

def get_vpc_zone_identifier(auto_scaling_group):
    asg_objects = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[auto_scaling_group])
    if asg_objects["AutoScalingGroups"] and len(asg_objects["AutoScalingGroups"]) > 0:
        asg = asg_objects["AutoScalingGroups"][0]
        logger.info("ASG: %s", asg)
        vpc_zone_identifier = asg["VPCZoneIdentifier"]
        logger.info("VPC_ZONE_IDENTIFIER: %s", vpc_zone_identifier)
        return vpc_zone_identifier, None
    else:
        return None, "Failed to describe autoscaling group data"

def get_vpc_and_subnet_id(vpc_zone_identifier):
    subnets = ec2.describe_subnets(SubnetIds=[vpc_zone_identifier])
    logger.info("SUBNETS: %s", subnets)
    if subnets["Subnets"] and len(subnets["Subnets"]) > 0:
        subnet = subnets["Subnets"][0]
        subnet_id = subnet["SubnetId"]
        logger.info("SUBNET_ID: %s", subnet_id)
        vpc_id = subnet["VpcId"]
        logger.info("VPC_ID: %s", vpc_id)
        return vpc_id, subnet_id, None
    else:
        return None, None, "Failed to describe subnet data"

# This function operates as follows:
# - Get the Lambda function currently being executed
# - Read the VPC config of the function
# - Find the VPC and subnet IDs of the function
# - Use the VPC and subnet ID to deduce which AZ the function runs in
# - Use the VPC ID and AZ to deduce which corresponding public subnet the NAT Gateway is in
# - Return the VPC ID, private subnet ID of the Lambda, and public subnet ID of the NAT
#   Gateway for use in replacing the route.
def get_vpc_and_subnets_from_lambda(function_name):
    try:
        func = boto_lambda.get_function(FunctionName=function_name)
    except botocore.exceptions.ClientError as error:
        logger.error("Unable to get Lambda function")
        raise error

    vpc_config = func.get("Configuration").get("VpcConfig")
    if vpc_config == "":
        logger.error("Unable to read VpcConfig from function")
        raise MissingVpcConfigError(func.get("Configuration"))

    vpc_id = vpc_config.get("VpcId")
    if vpc_id == "":
        logger.error("Could not get VpcId from Lambda VpcConfig")
        raise MissingVpcConfigError(vpc_config)

    subnet_ids = vpc_config.get("SubnetIds")
    if len(subnet_ids) != 1:
        logger.error("Unable to find single subnet ID for this function! Cannot replace route.")
        raise MissingFunctionSubnetError(vpc_config)
    subnet_id = subnet_ids[0]

    try:
        lambda_subnet = ec2.describe_subnets(
            Filters = [
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
        raise
    if "AvailabilityZone" not in lambda_subnets[0]:
        logger.error("Unable to find AZ of lambda function subnet! Cannot replace route.")
        raise MissingAZSubnetError(lambda_subnets)
    az = lambda_subnets[0]["AvailabilityZone"]
    lambda_subnet_id = lambda_subnets[0].get("SubnetId")

    try:
        az_subnets = ec2.describe_subnets(
            Filters = [
                {
                    "Name": "availability-zone",
                    "Values": [
                        az
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
                if f"public-{az}" in subnet_name:
                    public_subnet_id = subnet.get("SubnetId")
                    break

    if public_subnet_id == "":
        logger.error(f"Unable to find the public subnet ID for {az}! Cannot replace route.")
        raise MissingAZSubnetError(az_subnets)

    logger.info(f"Found subnet {public_subnet_id} in VPC {vpc_id}")
    return vpc_id, public_subnet_id, lambda_subnet_id

def get_nat_gateway_id(vpc_id, subnet_id):
    nat_gateways = ec2.describe_nat_gateways(
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
    logger.info("SUBNET ID: %s", subnet_id)
    logger.info("NAT GATEWAY: %s", nat_gateways)
    if nat_gateways['NatGateways'] and len(nat_gateways['NatGateways']) > 0:
        nat_gateway_id = nat_gateways['NatGateways'][0]["NatGatewayId"]
        logger.info("NAT_GATEWAY_ID %s", nat_gateway_id)
        return nat_gateway_id, None
    else:
        return None, "Failed To describe nat gateways"

def describe_and_replace_route(subnet_id, nat_gateway_id):
    route_tables = ec2.describe_route_tables(
        Filters=[{ "Name": "association.subnet-id",
                    "Values": [subnet_id]
                    }]
    )
    if route_tables['RouteTables'] and len(route_tables['RouteTables']) > 0:
        route_table = route_tables['RouteTables'][0]
        logger.info("ROUTE_TABLE: %s", route_table)
    else:
        return None, "Failed to describe route tables"

    response = ec2.replace_route(
        DestinationCidrBlock="0.0.0.0/0",
        NatGatewayId=nat_gateway_id,
        RouteTableId=route_table["RouteTableId"],
    )
    logger.info("RESPONSE: %s", response)
    if response["ResponseMetadata"] and response["ResponseMetadata"]['HTTPStatusCode'] == 200:
        logger.info("Successfully replaced route!")
        return response, None
    else:
        return None, "Failed to replace route"

def handle_autoscaling_hook(event):
    try:
        for record in event["Records"]:
            message = json.loads(record["Sns"]["Message"])
            if LIFECYCLE_KEY in message and ASG_KEY in message:
                life_cycle_hook = message[LIFECYCLE_KEY]
                auto_scaling_group = message[ASG_KEY]
                instance_id = message[EC2_KEY]
                logger.info("LIFECYLE_HOOK: %s", life_cycle_hook)
                logger.info("AUTO_SCALING_GROUP: %s", auto_scaling_group)
                logger.info("INSANCE_ID: %s", instance_id)
                vpc_zone_identifier, err = get_vpc_zone_identifier(auto_scaling_group)
                if err is not None:
                    return {
                        'statusCode': 400,
                        'body': json.dumps(err)
                    }
                vpc_id, subnet_id, err = get_vpc_and_subnet_id(vpc_zone_identifier)
                if err is not None:
                    return {
                        'statusCode': 400,
                        'body': json.dumps(err)
                    }
                nat_gateway_id, err = get_nat_gateway_id(vpc_id, subnet_id)
                if err is not None:
                    return {
                        'statusCode': 400,
                        'body': json.dumps(err)
                    }
                response, err = describe_and_replace_route(subnet_id, nat_gateway_id)
                if err is not None:
                    return {
                        'statusCode': 400,
                        'body': json.dumps(err)
                    }
                else:
                    return {
                        'statusCode': 200,
                        'body': json.dumps("Route replace succeeded")
                    }

    except Exception as e:
        logging.error("Error: %s", str(e))
        return {
            'statusCode': 400,
            'body': json.dumps(str(e))
        }

# handle_connection_test() tests connectivity by first trying an
# http GET on example.com. If that fails, try again on
# google.com. If that fails, replace the route to use NAT gateway.
# If either call succeeds, connectivity is fine so just exit early.
def handle_connection_test(event, context):
    if event.get("source") != "aws.events":
        logger.error("Unable to handle unknown event type: ", json.dumps(event))
        sys.exit(1)

    try:
        requests.get("https://www.example.com", timeout=5)
        return
    except requests.exceptions.RequestException as error:
        logger.error("ha-nat-connectivity-test error connecting to example.com, trying google.com")

    try:
        requests.get("https://www.google.com", timeout=5)
        return
    except requests.exceptions.RequestException as error:
        logger.error("ha-nat-connectivity-test error connecting to google.com, replacing route!")

    vpc_id, public_subnet_id, lambda_subnet_id = get_vpc_and_subnets_from_lambda(context.function_name)
    nat_gateway_id, _ = get_nat_gateway_id(vpc_id, public_subnet_id)
    describe_and_replace_route(lambda_subnet_id, nat_gateway_id)

def handler(event, context):
    if context.function_name.startswith(AUTOSCALING_FUNC_NAME):
        handle_autoscaling_hook(event)
    elif context.function_name.startswith(SCHEDULED_FUNC_NAME):
        handle_connection_test(event, context)
    else:
        logger.error("Unknown function invocation: %s", context.function_name)
        raise UnknownFunctionInvocation(context.function_name)


class UnknownFunctionInvocation(Exception): pass


class MissingVpcConfigError(Exception): pass


class MissingFunctionSubnetError(Exception): pass


class MissingAZSubnetError(Exception): pass
