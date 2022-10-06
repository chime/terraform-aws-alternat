import os
import json
import sys
import zipfile
import io

import boto3
import sure
import responses
from requests import ConnectTimeout
from moto import mock_autoscaling, mock_ec2, mock_iam, mock_lambda

sys.path.append('..')

EXAMPLE_AMI_ID = "ami-12c6146b"
AWS_REGION = "us-east-1"


@mock_ec2
def setup_networking():
    az = f"{AWS_REGION}a"
    ec2 = boto3.resource("ec2", region_name=AWS_REGION)

    vpc = ec2.create_vpc(CidrBlock="10.1.0.0/16")

    subnet1 = ec2.create_subnet(
        VpcId=vpc.id,
        CidrBlock="10.1.1.0/24",
        AvailabilityZone=f"{az}"
    )
    subnet1.create_tags(Tags=[{
        "Key": "Name",
        "Value": f"public-{az}-mock"
    }])

    subnet2 = ec2.create_subnet(
        VpcId=vpc.id,
        CidrBlock="10.1.2.0/24",
        AvailabilityZone=f"{az}",
    )
    subnet2.create_tags(Tags=[{
        "Key": "Name",
        "Value": f"private-{az}-mock"
    }])

    route_table = ec2.create_route_table(VpcId=vpc.id)
    sg = ec2.create_security_group(GroupName="test-sg", Description="test-sg")


    ec2_client = boto3.client("ec2", AWS_REGION)
    allocation_id = ec2_client.allocate_address(Domain="vpc")["AllocationId"]
    nat_gw_id = ec2_client.create_nat_gateway(
        SubnetId=subnet1.id,
        AllocationId=allocation_id
    )["NatGateway"]["NatGatewayId"]

    eni = ec2_client.create_network_interface(
        SubnetId=subnet1.id, PrivateIpAddress="10.1.1.5"
    )
    ec2_client.associate_route_table(
        RouteTableId=route_table.id,
        SubnetId=subnet2.id
    )
    ec2_client.create_route(
        DestinationCidrBlock="0.0.0.0/0",
        NetworkInterfaceId=eni["NetworkInterface"]["NetworkInterfaceId"],
        RouteTableId=route_table.id
    )

    return {
        "vpc": vpc.id,
        "subnet1": subnet1.id,
        "subnet2": subnet2.id,
        "nat_gw": nat_gw_id,
        "route_table": route_table.id,
        "sg": sg.id,
    }


def verify_nat_gateway_route(mocked_networking):
    ec2_client = boto3.client("ec2", AWS_REGION)

    filters = [{"Name": "route-table-id", "Values": [mocked_networking["route_table"]]}]
    route_tables = ec2_client.describe_route_tables(Filters=filters)["RouteTables"]

    route_tables.should.have.length_of(1)
    route_tables[0]["Routes"].should.have.length_of(2)

    for route in route_tables[0]["Routes"]:
        if route["DestinationCidrBlock"] == "0.0.0.0/0":
            zero_route = route
    zero_route.should.have.key("NatGatewayId").equals(mocked_networking["nat_gw"])


@mock_autoscaling
@mock_ec2
def test_handler():
    mocked_networking = setup_networking()
    ec2_client = boto3.client("ec2", region_name=AWS_REGION)
    template = ec2_client.create_launch_template(
        LaunchTemplateName="test_launch_template",
        LaunchTemplateData={"ImageId": EXAMPLE_AMI_ID, "InstanceType": "t2.micro"},
    )["LaunchTemplate"]

    autoscaling_client = boto3.client("autoscaling", AWS_REGION)
    autoscaling_client.create_auto_scaling_group(
        AutoScalingGroupName="ha-nat-asg",
        VPCZoneIdentifier=mocked_networking["subnet1"],
        MinSize=1,
        MaxSize=1,
        LaunchTemplate={
            "LaunchTemplateId": template["LaunchTemplateId"],
            "Version": str(template["LatestVersionNumber"]),
        },
    )

    ec2 = boto3.resource("ec2", region_name=AWS_REGION)

    from app import handler

    script_dir = os.path.dirname(__file__)
    with open(os.path.join(script_dir, "../sns-event.json"), "r") as file:
        asg_termination_event = file.read()

    handler(event=json.loads(asg_termination_event), context={})

    verify_nat_gateway_route(mocked_networking)


@mock_iam
def get_role():
    iam = boto3.client("iam", region_name=AWS_REGION)
    return iam.create_role(
        RoleName="my-role",
        AssumeRolePolicyDocument="some policy",
        Path="/my-path/",
    )["Role"]["Arn"]


def get_test_zip_file1():
    pfunc = """
    def lambda_handler(event, context):
        print("custom log event")
        return event
    """
    return _process_lambda(pfunc)


def _process_lambda(func_str):
    zip_output = io.BytesIO()
    zip_file = zipfile.ZipFile(zip_output, "w", zipfile.ZIP_DEFLATED)
    zip_file.writestr("lambda_function.py", func_str)
    zip_file.close()
    zip_output.seek(0)
    return zip_output.read()


@mock_lambda
@mock_ec2
@responses.activate
def test_connectivity_test_handler():
    ec2 = boto3.resource("ec2", region_name=AWS_REGION)
    ec2_client = boto3.client("ec2", region_name=AWS_REGION)
    lambda_function_name = "ha-nat-connectivity-test"
    mocked_networking = setup_networking()

    lambda_client = boto3.client("lambda", AWS_REGION)
    lambda_client.create_function(
        FunctionName=lambda_function_name,
        Runtime="python3.7",
        Role=get_role(),
        Handler="lambda_function.lambda_handler",
        Code={"ZipFile": get_test_zip_file1()},
        VpcConfig={"SecurityGroupIds": [mocked_networking["sg"]], "SubnetIds": [mocked_networking["subnet2"]]},
    )

    from app import connectivity_test_handler

    script_dir = os.path.dirname(__file__)
    with open(os.path.join(script_dir, "../cloudwatch-event.json"), "r") as file:
        cloudwatch_event = file.read()

    class Context:
        function_name=lambda_function_name

    responses.add(responses.GET, 'https://www.example.com', body=ConnectTimeout())
    responses.add(responses.GET, 'https://www.google.com', body=ConnectTimeout())
    connectivity_test_handler(event=json.loads(cloudwatch_event), context=Context())

    verify_nat_gateway_route(mocked_networking)
