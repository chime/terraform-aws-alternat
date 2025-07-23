package test

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"

	"github.com/aws/aws-sdk-go-v2/service/ec2"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"

	terraws "github.com/gruntwork-io/terratest/modules/aws"
	"github.com/gruntwork-io/terratest/modules/logger"
	"github.com/gruntwork-io/terratest/modules/random"
	"github.com/gruntwork-io/terratest/modules/retry"
	"github.com/gruntwork-io/terratest/modules/ssh"
	"github.com/gruntwork-io/terratest/modules/terraform"
	test_structure "github.com/gruntwork-io/terratest/modules/test-structure"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// Maintainer's note: This test will currently cause name collisions if multiple tests run in parallel
// in the same account. This is because the test uses a fixed name prefix for resources. This could be fixed
// by using GetRandomStableRegion and updating some resources (such as IAM role and CloudWatch event name)
// to use a random suffix.

func TestAlternat(t *testing.T) {
	// Uncomment any of the following lines to skip that part of the test.
	// This is useful for iterating during test development.
	// See https://terratest.gruntwork.io/docs/testing-best-practices/iterating-locally-using-test-stages/
	// os.Setenv("SKIP_setup", "true")
	// os.Setenv("SKIP_apply_vpc", "true")
	// os.Setenv("SKIP_apply_alternat_basic", "true")
	// os.Setenv("SKIP_validate_alternat_basic", "true")
	// os.Setenv("SKIP_validate_alternat_setup", "true")
	// os.Setenv("SKIP_validate_alternat_replace_route", "true")
	// os.Setenv("SKIP_validate_alternat_return_to_nat_instance", "true")
	// os.Setenv("SKIP_cleanup", "true")

	exampleFolder := test_structure.CopyTerraformFolderToTemp(t, "..", "examples/")

	// logger := logger.Logger{}

	defer test_structure.RunTestStage(t, "cleanup", func() {
		terraformOptions := test_structure.LoadTerraformOptions(t, exampleFolder)
		awsKeyPair := test_structure.LoadEc2KeyPair(t, exampleFolder)
		terraws.DeleteEC2KeyPair(t, awsKeyPair)
		terraform.Destroy(t, terraformOptions)
	})

	test_structure.RunTestStage(t, "setup", func() {
		//Use a random region if the SCP allows, otherwise hardcode.
		//awsRegion := terraws.GetRandomStableRegion(t, nil, nil)
		awsRegion := "us-east-1"

		uniqueID := random.UniqueId()
		keyPair := ssh.GenerateRSAKeyPair(t, 2048)
		awsKeyPair := terraws.ImportEC2KeyPair(t, awsRegion, uniqueID, keyPair)

		terraformOptions := terraform.WithDefaultRetryableErrors(t, &terraform.Options{
			TerraformDir: exampleFolder,
			Vars: map[string]interface{}{
				"aws_region":            awsRegion,
				"nat_instance_key_name": awsKeyPair.Name,
			},
		})

		test_structure.SaveString(t, exampleFolder, "awsRegion", awsRegion)
		test_structure.SaveEc2KeyPair(t, exampleFolder, awsKeyPair)
		test_structure.SaveTerraformOptions(t, exampleFolder, terraformOptions)
	})

	test_structure.RunTestStage(t, "apply_vpc", func() {
		terraformOptions := test_structure.LoadTerraformOptions(t, exampleFolder)
		terraformOptionsVpcOnly, err := terraformOptions.Clone()
		if err != nil {
			t.Fatal(err)
		}
		terraformOptionsVpcOnly.Targets = []string{"module.vpc"}
		terraform.InitAndApply(t, terraformOptionsVpcOnly)

		vpcID := terraform.Output(t, terraformOptions, "vpc_id")
		test_structure.SaveString(t, exampleFolder, "vpcID", vpcID)
	})

	test_structure.RunTestStage(t, "apply_alternat_basic", func() {
		terraformOptions := test_structure.LoadTerraformOptions(t, exampleFolder)
		terraform.InitAndApply(t, terraformOptions)
		assert.Equal(t, 0, terraform.InitAndPlanWithExitCode(t, terraformOptions))
		sgId := terraform.Output(t, terraformOptions, "nat_instance_security_group_id")
		test_structure.SaveString(t, exampleFolder, "sgId", sgId)
	})

	test_structure.RunTestStage(t, "validate_alternat_basic", func() {
		vpcID := test_structure.LoadString(t, exampleFolder, "vpcID")
		awsRegion := test_structure.LoadString(t, exampleFolder, "awsRegion")
		ec2Client := getEc2Client(t, awsRegion)
		routeTables, err := getRouteTables(t, ec2Client, vpcID)
		require.NoError(t, err)

		// Validate that private route tables have routes to the Internet via ENI
		for _, rt := range routeTables {
			for _, r := range rt.Routes {
				// If the route has a gateway ID, it must be a public route table.
				// Otherwise, it must be a private route table, and it must route to the Internet via ENI.
				if aws.ToString(r.DestinationCidrBlock) == "0.0.0.0/0" && r.GatewayId == nil && r.NetworkInterfaceId == nil {
					t.Fatalf("Private route table %v does not have a default route via ENI", rt.RouteTableId)
				}
			}
		}
	})

	test_structure.RunTestStage(t, "validate_alternat_setup", func() {
		sgId := aws.String(test_structure.LoadString(t, exampleFolder, "sgId"))
		awsRegion := test_structure.LoadString(t, exampleFolder, "awsRegion")
		ec2Client := getEc2Client(t, awsRegion)
		awsKeyPair := test_structure.LoadEc2KeyPair(t, exampleFolder)

		authorizeSshIngress(t, ec2Client, sgId)
		ip, err := getNatInstancePublicIp(t, ec2Client)
		require.NoError(t, err)

		natInstance := ssh.Host{
			Hostname:    ip,
			SshUserName: "ec2-user",
			SshKeyPair:  awsKeyPair.KeyPair,
		}

		maxRetries := 6
		waitTime := 10 * time.Second
		retry.DoWithRetry(t, fmt.Sprintf("Check SSH connection to %s", ip), maxRetries, waitTime, func() (string, error) {
			return "", ssh.CheckSshConnectionE(t, natInstance)
		})

		command := "sudo /usr/sbin/nft list ruleset"

		expectedText := `table ip nat {
	chain postrouting {
		type nat hook postrouting priority srcnat; policy accept;
		ip saddr 10.10.0.0/16 oif "ens5" masquerade
		ip saddr 10.20.0.0/16 oif "ens5" masquerade
	}
}
`

		maxRetries = 5
		waitTime = 10 * time.Second
		retry.DoWithRetry(t, fmt.Sprintf("SSH to NAT instance at IP %s", ip), maxRetries, waitTime, func() (string, error) {
			actualText, err := ssh.CheckSshCommandE(t, natInstance, command)
			assert.NoError(t, err)
			if actualText != expectedText {
				return "", fmt.Errorf("Expected SSH command to return '%s' but got '%s'", expectedText, actualText)
			}
			return "", nil
		})

		userdataLogFile := "/var/log/user-data.log"
		output := retry.DoWithRetry(t, fmt.Sprintf("Check contents of file %s", userdataLogFile), maxRetries, waitTime, func() (string, error) {
			return ssh.FetchContentsOfFileE(t, natInstance, false, userdataLogFile)
		})
		assert.Contains(t, output, "Configuration completed successfully!", "Success string not found in user-data log: %s", output)
	})

	// Delete the egress rules that allow access to the Internet from the instance, then
	// validate that Alternat has updated the route to use the NAT Gateway.
	test_structure.RunTestStage(t, "validate_alternat_replace_route", func() {
		sgId := aws.String(test_structure.LoadString(t, exampleFolder, "sgId"))
		vpcID := test_structure.LoadString(t, exampleFolder, "vpcID")
		awsRegion := test_structure.LoadString(t, exampleFolder, "awsRegion")
		ec2Client := getEc2Client(t, awsRegion)

		updateEgress(t, ec2Client, sgId, true)

		// Get the NAT Gateway IDs to validate routes point to any of the correct targets
		expectedNatGwIds, err := getNatGatewayIds(t, ec2Client, vpcID)
		require.NoError(t, err)
		require.Greater(t, len(expectedNatGwIds), 0, "No NAT Gateway IDs found")

		// Validate that private route tables have routes to the Internet via any of the NAT Gateways
		maxRetries := 12
		waitTime := 10 * time.Second
		output := retry.DoWithRetry(t, "Validating route through NAT Gateway", maxRetries, waitTime, func() (string, error) {
			routeTables, err := getRouteTables(t, ec2Client, vpcID)
			require.NoError(t, err)

			for _, rt := range routeTables {
				foundCorrectRoute := false
				for _, r := range rt.Routes {
					if aws.ToString(r.DestinationCidrBlock) == "0.0.0.0/0" {
						// Check that this default route points to one of our expected NAT Gateways
						currentNatGwId := aws.ToString(r.NatGatewayId)
						for _, expectedNatGwId := range expectedNatGwIds {
							if currentNatGwId == expectedNatGwId {
								foundCorrectRoute = true
								break
							}
						}
						if foundCorrectRoute {
							break
						} else if currentNatGwId != "" {
							// Route exists but points to wrong NAT Gateway
							return "", fmt.Errorf("Private route table %v has 0.0.0.0/0 route pointing to NAT Gateway %v, which is not one of the expected NAT Gateways %v",
								*rt.RouteTableId, currentNatGwId, expectedNatGwIds)
						}
					}
				}
				if !foundCorrectRoute {
					return "", fmt.Errorf("Private route table %v does not have a 0.0.0.0/0 route pointing to any NAT Gateway %v",
						*rt.RouteTableId, expectedNatGwIds)
				}
			}
			return "All private route tables route through NAT Gateway", nil
		})
		logger := logger.Logger{}
		logger.Logf(t, output)
	})

	// Validate that Alternat returns to the NAT instance when the egress rules are restored
	test_structure.RunTestStage(t, "validate_alternat_return_to_nat_instance", func() {
		sgId := aws.String(test_structure.LoadString(t, exampleFolder, "sgId"))
		vpcID := test_structure.LoadString(t, exampleFolder, "vpcID")
		awsRegion := test_structure.LoadString(t, exampleFolder, "awsRegion")
		ec2Client := getEc2Client(t, awsRegion)

		// Restore the egress rules that allow access to the Internet from the instance
		updateEgress(t, ec2Client, sgId, false)

		// Get the NAT instance ENI IDs to validate routes point to any of the correct targets
		expectedEniIds, err := getNatInstanceEniIds(t, ec2Client)
		require.NoError(t, err)
		require.Greater(t, len(expectedEniIds), 0, "No NAT instance ENI IDs found")

		// Validate that private route tables have routes to the Internet via any of the NAT instance ENIs
		maxRetries := 12
		waitTime := 10 * time.Second
		output := retry.DoWithRetry(t, "Validating route returns to the NAT instance", maxRetries, waitTime, func() (string, error) {
			routeTables, err := getRouteTables(t, ec2Client, vpcID)
			require.NoError(t, err)

			for _, rt := range routeTables {
				foundCorrectRoute := false
				for _, r := range rt.Routes {
					if aws.ToString(r.DestinationCidrBlock) == "0.0.0.0/0" {
						// Check that this default route points to one of our expected NAT instance ENIs
						currentEniId := aws.ToString(r.NetworkInterfaceId)
						for _, expectedEniId := range expectedEniIds {
							if currentEniId == expectedEniId {
								foundCorrectRoute = true
								break
							}
						}
						if foundCorrectRoute {
							break
						} else if currentEniId != "" {
							// Route exists but points to wrong ENI
							return "", fmt.Errorf("Private route table %v has 0.0.0.0/0 route pointing to ENI %v, which is not one of the expected NAT instance ENIs %v",
								*rt.RouteTableId, currentEniId, expectedEniIds)
						}
					}
				}
				if !foundCorrectRoute {
					return "", fmt.Errorf("Private route table %v does not have a 0.0.0.0/0 route pointing to any NAT instance ENI %v",
						*rt.RouteTableId, expectedEniIds)
				}
			}
			return "All private route tables route through NAT instance", nil
		})
		logger := logger.Logger{}
		logger.Logf(t, output)
	})
}

func updateEgress(t *testing.T, ec2Client *ec2.Client, sgId *string, revoke bool) {
	basePermission := ec2types.IpPermission{
		FromPort:   aws.Int32(0),
		ToPort:     aws.Int32(0),
		IpProtocol: aws.String("-1"),
	}
	ipv4Permission := basePermission
	ipv4Permission.IpRanges = []ec2types.IpRange{
		{
			CidrIp: aws.String("0.0.0.0/0"),
		},
	}
	ipv6Permission := basePermission
	ipv6Permission.Ipv6Ranges = []ec2types.Ipv6Range{
		{
			CidrIpv6: aws.String("::/0"),
		},
	}
	allPermissions := []ec2types.IpPermission{ipv4Permission, ipv6Permission}

	var err error
	if revoke {
		_, err = ec2Client.RevokeSecurityGroupEgress(context.TODO(), &ec2.RevokeSecurityGroupEgressInput{
			GroupId:       sgId,
			IpPermissions: allPermissions,
		},
		)
		require.NoError(t, err)
	} else {
		_, err = ec2Client.AuthorizeSecurityGroupEgress(context.TODO(), &ec2.AuthorizeSecurityGroupEgressInput{
			GroupId:       sgId,
			IpPermissions: allPermissions,
		},
		)
		require.NoError(t, err)
	}
}

func getRouteTables(t *testing.T, client *ec2.Client, vpcID string) ([]ec2types.RouteTable, error) {
	input := &ec2.DescribeRouteTablesInput{
		Filters: []ec2types.Filter{
			{
				Name:   aws.String("vpc-id"),
				Values: []string{vpcID},
			},
		},
	}

	result, err := client.DescribeRouteTables(context.TODO(), input)
	if err != nil {
		return nil, err
	}
	require.Greaterf(t, len(result.RouteTables), 0, "Could not find a route table for vpc %s", vpcID)

	return result.RouteTables, nil
}

func getNatGatewayIds(t *testing.T, ec2Client *ec2.Client, vpcID string) ([]string, error) {
	input := &ec2.DescribeNatGatewaysInput{
		Filter: []ec2types.Filter{
			{
				Name:   aws.String("vpc-id"),
				Values: []string{vpcID},
			},
			{
				Name:   aws.String("state"),
				Values: []string{"available"},
			},
		},
	}

	maxRetries := 6
	waitTime := 10 * time.Second
	var finalNatGwIds []string

	retry.DoWithRetry(t, "Get NAT Gateway IDs", maxRetries, waitTime, func() (string, error) {
		result, err := ec2Client.DescribeNatGateways(context.TODO(), input)
		if err != nil {
			return "", err
		}

		if len(result.NatGateways) == 0 {
			return "", fmt.Errorf("No NAT Gateways found in VPC %v", vpcID)
		}

		var natGwIds []string
		for _, natGw := range result.NatGateways {
			natGwId := aws.ToString(natGw.NatGatewayId)
			if natGwId != "" {
				natGwIds = append(natGwIds, natGwId)
			}
		}

		if len(natGwIds) == 0 {
			return "", fmt.Errorf("No valid NAT Gateway IDs found in VPC %v", vpcID)
		}

		finalNatGwIds = natGwIds
		return "success", nil
	})

	return finalNatGwIds, nil
}

func getNatInstanceEniIds(t *testing.T, ec2Client *ec2.Client) ([]string, error) {
	namePrefix := "alternat-"
	input := &ec2.DescribeInstancesInput{
		Filters: []ec2types.Filter{
			{
				Name:   aws.String("tag:Name"),
				Values: []string{namePrefix + "*"},
			},
			{
				Name:   aws.String("instance-state-name"),
				Values: []string{"running"},
			},
		},
	}

	maxRetries := 6
	waitTime := 10 * time.Second
	var finalEniIds []string

	retry.DoWithRetry(t, "Get NAT Instance ENI IDs", maxRetries, waitTime, func() (string, error) {
		result, err := ec2Client.DescribeInstances(context.TODO(), input)
		if err != nil {
			return "", err
		}

		if len(result.Reservations) == 0 {
			return "", fmt.Errorf("No NAT instances found")
		}

		var eniIds []string
		for _, reservation := range result.Reservations {
			for _, instance := range reservation.Instances {
				if len(instance.NetworkInterfaces) == 0 {
					continue // Skip instances without network interfaces
				}
				// Get the primary network interface (index 0)
				eniId := aws.ToString(instance.NetworkInterfaces[0].NetworkInterfaceId)
				if eniId != "" {
					eniIds = append(eniIds, eniId)
				}
			}
		}

		if len(eniIds) == 0 {
			return "", fmt.Errorf("No valid ENI IDs found for NAT instances")
		}

		finalEniIds = eniIds
		return "success", nil
	})

	return finalEniIds, nil
}

func getNatInstancePublicIp(t *testing.T, ec2Client *ec2.Client) (string, error) {
	namePrefix := "alternat-"
	input := &ec2.DescribeInstancesInput{
		Filters: []ec2types.Filter{
			{
				Name:   aws.String("tag:Name"),
				Values: []string{namePrefix + "*"},
			},
			{
				Name:   aws.String("instance-state-name"),
				Values: []string{"running"},
			},
		},
	}
	maxRetries := 6
	waitTime := 10 * time.Second
	ip := retry.DoWithRetry(t, "Get NAT Instance public IP", maxRetries, waitTime, func() (string, error) {
		result, err := ec2Client.DescribeInstances(context.TODO(), input)
		if err != nil {
			return "", err
		}

		publicIp := aws.ToString(result.Reservations[0].Instances[0].PublicIpAddress)
		if publicIp == "" {
			return "", fmt.Errorf("Public IP not found")
		}
		return publicIp, nil
	})

	return ip, nil
}

func getThisPublicIp() (string, error) {
	url := "https://api.ipify.org"
	resp, err := http.Get(url)
	if err != nil {
		return "", fmt.Errorf("Error fetching IP: %v\n", err)
	}
	defer resp.Body.Close()

	ip, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", fmt.Errorf("Error reading response: %v", err)
	}

	return string(ip), nil
}

func authorizeSshIngress(t *testing.T, ec2Client *ec2.Client, sgId *string) {
	ip, err := getThisPublicIp()
	require.NoError(t, err)

	ipPermission := []ec2types.IpPermission{
		{
			FromPort:   aws.Int32(22),
			ToPort:     aws.Int32(22),
			IpProtocol: aws.String("tcp"),
			IpRanges: []ec2types.IpRange{
				{
					CidrIp: aws.String(ip + "/32"),
				},
			},
		},
	}

	_, err = ec2Client.AuthorizeSecurityGroupIngress(context.TODO(), &ec2.AuthorizeSecurityGroupIngressInput{
		GroupId:       sgId,
		IpPermissions: ipPermission,
	},
	)
	require.NoError(t, err)
}

func getEc2Client(t *testing.T, awsRegion string) *ec2.Client {
	cfg, err := config.LoadDefaultConfig(context.TODO(), config.WithRegion(awsRegion))
	if err != nil {
		t.Fatalf("Unable to load SDK config, %v", err)
	}
	return ec2.NewFromConfig(cfg)
}
