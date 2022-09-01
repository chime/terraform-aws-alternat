import json
import logging
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

LIFECYCLE_KEY = "LifecycleHookName"
ASG_KEY = "AutoScalingGroupName"
EC2_KEY = "EC2InstanceId"
autoscaling = boto3.client("autoscaling")
ec2 = boto3.client("ec2")

def get_vpc_zone_identifier_and_azs(auto_scaling_group):
    asg_objects = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[auto_scaling_group])
    asg = asg_objects["AutoScalingGroups"][0]
    logger.info("ASG: %s", asg)
    availability_zones = asg["AvailabilityZones"]
    vpc_zone_identifier = asg["VPCZoneIdentifier"]
    logger.info("AVAILABILITY_ZONES: %s", availability_zones)
    logger.info("VPC_ZONE_IDENTIFIER: %s", vpc_zone_identifier)
    return availability_zones, vpc_zone_identifier

def get_vpc_and_subnet_id(vpc_zone_identifier):
    subnets = ec2.describe_subnets(SubnetIds=[vpc_zone_identifier])
    subnet = subnets["Subnets"][0]
    subnet_id = subnet["SubnetId"]
    logger.info("SUBNET_ID: %s", subnet_id)
    vpc_id = subnet["VpcId"]
    logger.info("VPC_ID: %s", vpc_id)
    return vpc_id, subnet_id

def get_nat_gatway_id(vpc_id, subnet_id):
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
    nat_gateway_id = nat_gateways['NatGateways'][0]["NatGatewayId"]
    logger.info("NAT_GATEWAY_ID %s", nat_gateway_id)
    return nat_gateway_id

def describe_and_replace_route(subnet_id, nat_gateway_id):
    route_tables = ec2.describe_route_tables(
        Filters=[{ "Name": "association.subnet-id",
                    "Values": [subnet_id]
                    }]
    )
    route_table = route_tables['RouteTables'][0]
    logger.info("ROUTE_TABLE: %s", route_table)
    response = ec2.replace_route(
        DryRun=True,
        DestinationCidrBlock="0.0.0.0/0",
        NatGatewayId=nat_gateway_id,
        RouteTableId=route_table["RouteTableId"],
    )
    logger.info("RESPONSE: %s", response)

def handler(event, context):
    try:
        #logger.info(json.dumps(event))
        for record in event["Records"]:
            message = json.loads(record["Sns"]["Message"])
            if LIFECYCLE_KEY in message and ASG_KEY in message:
                life_cycle_hook = message[LIFECYCLE_KEY]
                auto_scaling_group = message[ASG_KEY]
                instance_id = message[EC2_KEY]
                logger.info("LIFECYLE_HOOK: %s", life_cycle_hook)
                logger.info("AUTO_SCALING_GROUP: %s", auto_scaling_group)
                logger.info("INSANCE_ID: %s", instance_id)
                availability_zones, vpc_zone_identifier = get_vpc_zone_identifier_and_azs(auto_scaling_group)
                vpc_id, subnet_id = get_vpc_and_subnet_id(vpc_zone_identifier)
                nat_gateway_id = get_nat_gatway_id(vpc_id, subnet_id)
                describe_and_replace_route(subnet_id, nat_gateway_id)


    except Exception as e:
        logging.error("Error: %s", str(e))
