"""
Run like this : `AWS_DEFAULT_REGION='us-east-1' pytest`
"""

import os
import json
import sys
import zipfile
import io
import logging
import mock
import socket

import boto3
import sure
from moto import mock_aws

sys.path.append('..')

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
logging.getLogger('boto3').setLevel(logging.CRITICAL)
logging.getLogger('botocore').setLevel(logging.CRITICAL)

EXAMPLE_AMI_ID = "ami-12c6146b"


@mock_aws
def setup_networking():
    az = f"{os.environ['AWS_DEFAULT_REGION']}a"
    ec2 = boto3.resource("ec2")

    vpc = ec2.create_vpc(CidrBlock="10.1.0.0/16")

    public_subnet = ec2.create_subnet(
        VpcId=vpc.id,
        CidrBlock="10.1.1.0/24",
        AvailabilityZone=f"{az}"
    )

    private_subnet = ec2.create_subnet(
        VpcId=vpc.id,
        CidrBlock="10.1.2.0/24",
        AvailabilityZone=f"{az}",
    )

    private_subnet_two = ec2.create_subnet(
        VpcId=vpc.id,
        CidrBlock="10.1.3.0/24",
        AvailabilityZone=f"{az}",
    )


    route_table = ec2.create_route_table(VpcId=vpc.id)
    route_table_two = ec2.create_route_table(VpcId=vpc.id)
    sg = ec2.create_security_group(GroupName="test-sg", Description="test-sg")

    ec2_client = boto3.client("ec2")
    allocation_id = ec2_client.allocate_address(Domain="vpc")["AllocationId"]
    nat_gw_id = ec2_client.create_nat_gateway(
        SubnetId=public_subnet.id,
        AllocationId=allocation_id
    )["NatGateway"]["NatGatewayId"]

    eni = ec2_client.create_network_interface(
        SubnetId=public_subnet.id, PrivateIpAddress="10.1.1.5"
    )
    ec2_client.associate_route_table(
        RouteTableId=route_table.id,
        SubnetId=private_subnet.id
    )
    ec2_client.create_route(
        DestinationCidrBlock="0.0.0.0/0",
        NetworkInterfaceId=eni["NetworkInterface"]["NetworkInterfaceId"],
        RouteTableId=route_table.id
    )
    ec2_client.associate_route_table(
        RouteTableId=route_table.id,
        SubnetId=private_subnet_two.id
    )
    ec2_client.create_route(
        DestinationCidrBlock="0.0.0.0/0",
        NetworkInterfaceId=eni["NetworkInterface"]["NetworkInterfaceId"],
        RouteTableId=route_table_two.id
    )

    return {
        "vpc": vpc.id,
        "public_subnet": public_subnet.id,
        "private_subnet": private_subnet.id,
        "private_subnet_two": private_subnet_two.id,
        "nat_gw": nat_gw_id,
        "route_table": route_table.id,
        "route_table_two": route_table_two.id,
        "sg": sg.id,
    }


def verify_nat_gateway_route(mocked_networking):
    ec2_client = boto3.client("ec2")

    filters = [{"Name": "route-table-id", "Values": [mocked_networking["route_table"],mocked_networking["route_table_two"]]}]
    route_tables = ec2_client.describe_route_tables(Filters=filters)["RouteTables"]

    route_tables.should.have.length_of(2)
    route_tables[0]["Routes"].should.have.length_of(2)
    route_tables[1]["Routes"].should.have.length_of(2)

    for rt in route_tables:
        for route in rt["Routes"]:
            if route["DestinationCidrBlock"] == "0.0.0.0/0":
                zero_route = route
        zero_route.should.have.key("NatGatewayId").equals(mocked_networking["nat_gw"])


@mock_aws
def test_handler():
    mocked_networking = setup_networking()
    ec2_client = boto3.client("ec2")
    template = ec2_client.create_launch_template(
        LaunchTemplateName="test_launch_template",
        LaunchTemplateData={"ImageId": EXAMPLE_AMI_ID, "InstanceType": "t2.micro"},
    )["LaunchTemplate"]

    autoscaling_client = boto3.client("autoscaling")
    autoscaling_client.create_auto_scaling_group(
        AutoScalingGroupName="alternat-asg",
        VPCZoneIdentifier=mocked_networking["public_subnet"],
        MinSize=1,
        MaxSize=1,
        LaunchTemplate={
            "LaunchTemplateId": template["LaunchTemplateId"],
            "Version": str(template["LatestVersionNumber"]),
        },
    )

    from app import handler

    script_dir = os.path.dirname(__file__)
    with open(os.path.join(script_dir, "../sns-event.json"), "r") as file:
        asg_termination_event = file.read()

    az = f"{os.environ['AWS_DEFAULT_REGION']}a".upper().replace("-", "_")
    os.environ[az] = ",".join([mocked_networking["route_table"],mocked_networking["route_table_two"]])

    handler(json.loads(asg_termination_event), {})

    verify_nat_gateway_route(mocked_networking)


@mock_aws
def get_role():
    iam = boto3.client("iam")
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


@mock_aws
@mock.patch('urllib.request.urlopen')
def test_connectivity_test_handler(mock_urlopen):
    from app import connectivity_test_handler
    mocked_networking = setup_networking()

    lambda_client = boto3.client("lambda")
    lambda_function_name = "alternat-connectivity-test"
    lambda_client.create_function(
        FunctionName=lambda_function_name,
        Role=get_role(),
        Code={"ZipFile": get_test_zip_file1()},
    )

    script_dir = os.path.dirname(__file__)
    with open(os.path.join(script_dir, "../cloudwatch-event.json"), "r") as file:
        cloudwatch_event = file.read()

    class Context:
        function_name = lambda_function_name

    mock_urlopen.side_effect = socket.timeout()
    os.environ["ROUTE_TABLE_IDS_CSV"] = ",".join([mocked_networking["route_table"], mocked_networking["route_table_two"]])
    os.environ["PUBLIC_SUBNET_ID"] = mocked_networking["public_subnet"]

    connectivity_test_handler(event=json.loads(cloudwatch_event), context=Context())

    verify_nat_gateway_route(mocked_networking)


def test_disable_ipv6():
    with mock.patch('socket.getaddrinfo') as mock_getaddrinfo:
        from app import disable_ipv6
        disable_ipv6()
        socket.getaddrinfo('example.com', 80)
        mock_getaddrinfo.assert_called()
        call_args = mock_getaddrinfo.call_args.args
        assert len(call_args) == 3, f"With IPv6 disabled, expected 3 arguments to getaddrinfo, found {len(call_args)}"
        assert call_args[2] == socket.AF_INET, "Did not find AF_INET family in args to getaddrinfo"
