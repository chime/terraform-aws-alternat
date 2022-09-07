import json
import logging
import time

import boto3
import sys

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

LIFECYCLE_KEY = "LifecycleHookName"
ASG_KEY = "AutoScalingGroupName"
EC2_KEY = "EC2InstanceId"
autoscaling = boto3.client("autoscaling")
ec2 = boto3.client("ec2")

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
    if subnets["Subnets"] and len(subnets["Subnets"]) > 0:
        subnet = subnets["Subnets"][0]
        subnet_id = subnet["SubnetId"]
        logger.info("SUBNET_ID: %s", subnet_id)
        vpc_id = subnet["VpcId"]
        logger.info("VPC_ID: %s", vpc_id)
        return vpc_id, subnet_id, None
    else:
        return None, None, "Failed to describe subnet data"

def get_nat_gateway_id(vpc_id, subnet_id):
    nat_gateways = ec2.describe_nat_gateways(
        Filters=[{ "Name": "vpc-id",
                    "Values": [vpc_id]
                    },
                    { "Name": "subnet-id",
                    "Values": [subnet_id]
                    },
                    ])
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
        return response, None
    else:
        return None, "Failed to replace route"

def handler(event, context):
    logger.info(json.dumps(event))
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
