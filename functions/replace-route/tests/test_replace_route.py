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
import sure

import boto3
import botocore

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

    launch_template = ec2_client.create_launch_template(
        LaunchTemplateName="test_launch_template",
        LaunchTemplateData={"ImageId": EXAMPLE_AMI_ID, "InstanceType": "t2.micro"},
    )["LaunchTemplate"]

    autoscaling_client = boto3.client("autoscaling")
    autoscaling_client.create_auto_scaling_group(
        AutoScalingGroupName="alternat-asg",
        VPCZoneIdentifier=public_subnet.id,
        MinSize=1,
        MaxSize=1,
        LaunchTemplate={
            "LaunchTemplateId": launch_template["LaunchTemplateId"],
            "Version": str(launch_template["LatestVersionNumber"]),
        },
    )

    reservations = ec2_client.describe_instances()["Reservations"]
    instance_id = reservations[0]["Instances"][0]["InstanceId"]

    ec2_client.associate_route_table(
        RouteTableId=route_table.id,
        SubnetId=private_subnet.id
    )
    ec2_client.create_route(
        DestinationCidrBlock="0.0.0.0/0",
        InstanceId=instance_id,
        RouteTableId=route_table.id
    )
    ec2_client.associate_route_table(
        RouteTableId=route_table.id,
        SubnetId=private_subnet_two.id
    )
    ec2_client.create_route(
        DestinationCidrBlock="0.0.0.0/0",
        InstanceId=instance_id,
        RouteTableId=route_table_two.id
    )

    return {
        "public_subnet": public_subnet.id,
        "nat_gw": nat_gw_id,
        "route_table": route_table.id,
        "route_table_two": route_table_two.id,
        "instance": instance_id,
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


def verify_nat_instance_route(mocked_networking, instance_id):
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
        zero_route.should.have.key("InstanceId").equals(instance_id)


@mock_aws
def test_handler(monkeypatch):
    mocked_networking = setup_networking()

    from app import handler

    script_dir = os.path.dirname(__file__)
    with open(os.path.join(script_dir, "../sns-event.json"), "r") as file:
        asg_termination_event = json.loads(file.read())

    az = f"{os.environ['AWS_DEFAULT_REGION']}a".upper().replace("-", "_")
    monkeypatch.setenv(az, ",".join([mocked_networking["route_table"],mocked_networking["route_table_two"]]))

    # CompleteLifecycleAction is not implemented by Moto
    orig_make_api_call = botocore.client.BaseClient._make_api_call
    mock_complete_lifecycle_action = mock.Mock()
    def mock_make_api_call(self, operation_name, kwarg):
        if operation_name == "CompleteLifecycleAction":
            return mock_complete_lifecycle_action(self, operation_name, kwarg)
        return orig_make_api_call(self, operation_name, kwarg)

    with mock.patch("botocore.client.BaseClient._make_api_call", new=mock_make_api_call):
        handler(asg_termination_event, {})
        mock_complete_lifecycle_action.assert_called_once()
    verify_nat_instance_route(mocked_networking, mocked_networking["instance"])

    sns_message = json.loads(asg_termination_event["Records"][0]["Sns"]["Message"])
    sns_message["EC2InstanceId"] = mocked_networking["instance"]
    asg_termination_event["Records"][0]["Sns"]["Message"] = json.dumps(sns_message)

    mock_complete_lifecycle_action.reset_mock()
    with mock.patch("botocore.client.BaseClient._make_api_call", new=mock_make_api_call):
        handler(asg_termination_event, {})
        mock_complete_lifecycle_action.assert_called_once()
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
@mock.patch('time.sleep')
@mock.patch('urllib.request.urlopen')
def test_connectivity_test_handler(mock_urlopen, mock_sleep, monkeypatch):
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
    monkeypatch.setenv("ROUTE_TABLE_IDS_CSV", ",".join([mocked_networking["route_table"], mocked_networking["route_table_two"]]))
    monkeypatch.setenv("PUBLIC_SUBNET_ID", mocked_networking["public_subnet"])
    monkeypatch.setenv("ENABLE_NAT_RESTORE", "false")  # Disable NAT restore for this test

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


@mock_aws
def test_is_source_dest_check_enabled():
    mocked_networking = setup_networking()
    ec2_client = boto3.client("ec2")

    # Create a test instance
    instance = ec2_client.run_instances(
        ImageId=EXAMPLE_AMI_ID,
        MinCount=1,
        MaxCount=1,
        InstanceType="t2.micro",
        SubnetId=mocked_networking["public_subnet"]
    )["Instances"][0]
    instance_id = instance["InstanceId"]

    from app import is_source_dest_check_enabled

    # Default is True
    assert is_source_dest_check_enabled(instance_id) == True

    # Disable source/dest check
    ec2_client.modify_instance_attribute(
        InstanceId=instance_id,
        SourceDestCheck={"Value": False}
    )

    # Should return False now
    assert is_source_dest_check_enabled(instance_id) == False

    # Test error handling (invalid instance ID)
    assert is_source_dest_check_enabled("i-invalid") is None


@mock_aws
def test_are_any_routes_pointing_to_nat_gateway():
    mocked_networking = setup_networking()
    ec2_client = boto3.client("ec2")

    # Setup: Modify routes to use NAT Gateway for testing
    for rtb_id in [mocked_networking["route_table"], mocked_networking["route_table_two"]]:
        # First delete any existing default routes
        ec2_client.delete_route(
            RouteTableId=rtb_id,
            DestinationCidrBlock="0.0.0.0/0"
        )
        # Create new route using NAT Gateway
        ec2_client.create_route(
            RouteTableId=rtb_id,
            DestinationCidrBlock="0.0.0.0/0",
            NatGatewayId=mocked_networking["nat_gw"]
        )

    from app import are_any_routes_pointing_to_nat_gateway

    # Now our setup has routes with NAT gateway, should return True
    route_tables = [mocked_networking["route_table"], mocked_networking["route_table_two"]]
    assert are_any_routes_pointing_to_nat_gateway(route_tables) == True

    # Test with invalid route table
    assert are_any_routes_pointing_to_nat_gateway(["rtb-invalid"]) == False


@mock_aws
@mock.patch('time.sleep')
def test_run_nat_instance_diagnostics(mock_sleep):
    from app import run_nat_instance_diagnostics

    # Common text fragments for building diagnostic outputs
    nft_table_start = "nft_nat_table=table ip nat {\nchain postrouting {\ntype nat hook postrouting priority 100; policy accept;"
    masquerade_rule = "\nip saddr 10.0.0.0/8 oifname \"eth0\" counter packets 0 bytes 0 masquerade"
    nft_table_end = "\n}\n}\n"

    # Build diagnostic outputs
    good_nft_output = nft_table_start + masquerade_rule + nft_table_end
    missing_masquerade_output = nft_table_start + nft_table_end

    # Test scenarios: (description, ssm_output, source_dest_check, expected_result)
    test_cases = [
        ("successful diagnostics", f"ip_forward=1\n{good_nft_output}", False, True),
        ("ip_forward=0 failure", f"ip_forward=0\n{good_nft_output}", False, False),
        ("missing masquerade rule failure", f"ip_forward=1\n{missing_masquerade_output}", False, False),
        ("source/dest check enabled failure", f"ip_forward=1\n{good_nft_output}", True, False),
        ("error in source/dest check", f"ip_forward=1\n{good_nft_output}", None, False),
    ]

    with mock.patch('boto3.client') as mock_boto_client:
        mock_ssm = mock.MagicMock()
        mock_boto_client.return_value = mock_ssm
        mock_ssm.send_command.return_value = {'Command': {'CommandId': 'test-command-id'}}

        for description, ssm_output, source_dest_check, expected_result in test_cases:
            mock_ssm.get_command_invocation.return_value = {
                'Status': 'Success',
                'StandardOutputContent': ssm_output,
                'StandardErrorContent': ''
            }

            with mock.patch('app.is_source_dest_check_enabled', return_value=source_dest_check):
                result = run_nat_instance_diagnostics('i-12345678')
                assert result == expected_result, f"Failed test case: {description}"

        # Test SSM command failure separately
        mock_ssm.send_command.side_effect = botocore.exceptions.ClientError(
            {'Error': {'Code': 'InvalidInstanceId', 'Message': 'Test error'}},
            'SendCommand'
        )
        result = run_nat_instance_diagnostics('i-12345678')
        assert result == False


@mock_aws
@mock.patch('time.sleep')
def test_attempt_nat_instance_restore(mock_sleep, monkeypatch):
    from app import attempt_nat_instance_restore

    # Need to mock boto3.client to avoid calling AWS API
    with mock.patch('boto3.client') as mock_boto_client:
        # Mock AWS clients
        mock_ssm = mock.MagicMock()
        mock_ec2 = mock.MagicMock()

        def get_boto_client(service):
            if service == 'ssm':
                return mock_ssm
            elif service == 'ec2':
                return mock_ec2
            return mock.MagicMock()

        mock_boto_client.side_effect = get_boto_client

    # Setup environment
    route_tables = ['rtb-12345', 'rtb-67890']
    monkeypatch.setenv("ROUTE_TABLE_IDS_CSV", ",".join(route_tables))
    monkeypatch.setenv("NAT_ASG_NAME", "test-nat-asg")

    # Mock successful test and diagnostic
    with mock.patch('app.get_current_nat_instance_id', return_value='i-test123'):
        with mock.patch('app.run_nat_instance_diagnostics', return_value=True):
            # Mock successful connectivity test
            mock_ssm.send_command.return_value = {
                'Command': {'CommandId': 'test-command-id'}
            }
            mock_ssm.get_command_invocation.return_value = {
                'Status': 'Success',
                'StandardOutputContent': '200\n200',
                'StandardErrorContent': ''
            }

            # Mock replace_route
            with mock.patch('app.replace_route') as mock_replace_route:
                # Test successful restore
                attempt_nat_instance_restore()

                # Verify replace_route called for both route tables
                assert mock_replace_route.call_count == 2
                mock_replace_route.assert_any_call(route_tables[0], 'i-test123')
                mock_replace_route.assert_any_call(route_tables[1], 'i-test123')

    # Test when NAT instance has no internet
    with mock.patch('app.get_current_nat_instance_id', return_value='i-test123'):
        mock_ssm.get_command_invocation.return_value = {
            'Status': 'Success',
            'StandardOutputContent': '404\n500',
            'StandardErrorContent': ''
        }
        with mock.patch('app.replace_route') as mock_replace_route:
            attempt_nat_instance_restore()
            # Should not call replace_route
            assert mock_replace_route.call_count == 0

    # Test when diagnostics fail
    with mock.patch('app.get_current_nat_instance_id', return_value='i-test123'):
        with mock.patch('app.run_nat_instance_diagnostics', return_value=False):
            mock_ssm.get_command_invocation.return_value = {
                'Status': 'Success',
                'StandardOutputContent': '200\n200',
                'StandardErrorContent': ''
            }
            with mock.patch('app.replace_route') as mock_replace_route:
                attempt_nat_instance_restore()
                # Should not call replace_route
                assert mock_replace_route.call_count == 0


@mock_aws
@mock.patch('time.sleep')
def test_nat_restore_option(mock_sleep, monkeypatch):
    from app import connectivity_test_handler
    mocked_networking = setup_networking()

    with mock.patch('app.get_current_nat_instance_id') as mock_get_instance:
        # Just mock a NAT instance ID directly instead of creating instances
        mock_get_instance.return_value = 'i-test123'

    # Setup environment variables
    script_dir = os.path.dirname(__file__)
    with open(os.path.join(script_dir, "../cloudwatch-event.json"), "r") as file:
        cloudwatch_event = file.read()

    class Context:
        function_name = "alternat-connectivity-test"

    monkeypatch.setenv("ROUTE_TABLE_IDS_CSV", ",".join([mocked_networking["route_table"], mocked_networking["route_table_two"]]))
    monkeypatch.setenv("PUBLIC_SUBNET_ID", mocked_networking["public_subnet"])
    monkeypatch.setenv("NAT_ASG_NAME", "alternat-nat-asg")
    monkeypatch.setenv("CONNECTIVITY_CHECK_INTERVAL", "60")

    # Use a with block for urllib mocking
    with mock.patch('urllib.request.urlopen') as mock_urlopen:
        # Test with NAT restore disabled (default)
        mock_urlopen.side_effect = None  # Connection succeeds

        # Run test with restore DISABLED (default behavior)
        with mock.patch('app.attempt_nat_instance_restore') as mock_restore:
            connectivity_test_handler(event=json.loads(cloudwatch_event), context=Context())
            mock_restore.assert_not_called()  # Should not try to restore

        # Test with NAT restore enabled
        monkeypatch.setenv("ENABLE_NAT_RESTORE", "true")

        # Mock that we're using NAT Gateway
        with mock.patch('app.are_any_routes_pointing_to_nat_gateway', return_value=True):
            # Mock the attempt_nat_instance_restore function
            with mock.patch('app.attempt_nat_instance_restore') as mock_restore:
                connectivity_test_handler(event=json.loads(cloudwatch_event), context=Context())
                mock_restore.assert_called_once()  # Should try to restore
