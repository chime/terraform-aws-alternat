package test

import (
	"context"
	"fmt"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"

	"github.com/gruntwork-io/terratest/modules/retry"
	"github.com/gruntwork-io/terratest/modules/terraform"
	test_structure "github.com/gruntwork-io/terratest/modules/test-structure"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	// "github.com/davecgh/go-spew/spew"
)

func TestAlternat(t *testing.T) {
	// os.Setenv("SKIP_setup", "true")	
	// os.Setenv("SKIP_apply_vpc", "true")	
	// os.Setenv("SKIP_apply_alternat_basic", "true")	
	// os.Setenv("SKIP_validate_alternat_basic", "true")	
	// os.Setenv("SKIP_validate_alternat_replace_route", "true")	
	// os.Setenv("SKIP_destroy", "true")	

	// logger := logger.Logger{}
	
	cfg, err := config.LoadDefaultConfig(context.TODO())
	if err != nil {
			t.Fatalf("Unable to load SDK config, %v", err)
	}
	ec2Client := ec2.NewFromConfig(cfg)

	defer test_structure.RunTestStage(t, "destroy", func() {
		terraformOptions := test_structure.LoadTerraformOptions(t, ".")
		terraform.Destroy(t, terraformOptions)
	})
	
	test_structure.RunTestStage(t, "setup", func() {
		//awsRegion := terratestaws.GetRandomStableRegion(t, nil, nil)
		awsRegion := "us-east-1"
		terraformOptions := terraform.WithDefaultRetryableErrors(t, &terraform.Options{
			TerraformDir: "../example",
			Vars: map[string]interface{}{
				"aws_region": awsRegion,
			},
		})
		test_structure.SaveString(t, ".", "awsRegion", awsRegion)
		test_structure.SaveTerraformOptions(t, ".", terraformOptions)
	})

	test_structure.RunTestStage(t, "apply_vpc", func() {
		terraformOptions := test_structure.LoadTerraformOptions(t, ".")
		terraformOptionsVpcOnly, err := terraformOptions.Clone()
		if err != nil {
			t.Fatal(err)
		}
		terraformOptionsVpcOnly.Targets = []string{"module.vpc"}
		terraform.InitAndApply(t, terraformOptionsVpcOnly)
		
		vpcID := terraform.Output(t, terraformOptions, "vpc_id")
		test_structure.SaveString(t, ".", "vpcID", vpcID)
	})

	test_structure.RunTestStage(t, "apply_alternat_basic", func() {
		terraformOptions := test_structure.LoadTerraformOptions(t, ".")
		terraform.InitAndApply(t, terraformOptions)
		assert.Equal(t, 0, terraform.InitAndPlanWithExitCode(t, terraformOptions))
	})

	test_structure.RunTestStage(t, "validate_alternat_basic", func() {
		vpcID := test_structure.LoadString(t, ".", "vpcID")
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

	// Delete the egress rules that allow access to the Internet from the instance, then 
	// validate that Alternat has updated the route to use the NAT Gateway
	test_structure.RunTestStage(t, "validate_alternat_replace_route", func() {
		terraformOptions := test_structure.LoadTerraformOptions(t, ".")
		vpcID := test_structure.LoadString(t, ".", "vpcID")
		
		revokeInternetEgress(ec2Client, t, terraformOptions)

		// Validate that private route tables have routes to the Internet via Nat Gateway
		maxRetries := 6
		waitTime := 10 * time.Second
		retry.DoWithRetry(t, "Validating route through NAT Gateway", maxRetries, waitTime, func() (string, error) {
			routeTables, err := getRouteTables(t, ec2Client, vpcID)
			require.NoError(t, err)
			for _, rt := range routeTables {
				for _, r := range rt.Routes {
					if aws.ToString(r.DestinationCidrBlock) == "0.0.0.0/0" && r.GatewayId == nil && r.NatGatewayId == nil {
						return "", fmt.Errorf("Private route table %v does not have a route via NAT Gateway", *rt.RouteTableId)
					}
				}
			}	
			return "All private route tables route through NAT Gateway", nil
		})
	})
}

func revokeInternetEgress(ec2Client *ec2.Client, t *testing.T, terraformOptions *terraform.Options) {
	sgId := aws.String(terraform.Output(t, terraformOptions, "nat_instance_security_group_id"))
	_, err := ec2Client.RevokeSecurityGroupEgress(context.TODO(), &ec2.RevokeSecurityGroupEgressInput{
		GroupId: sgId,
		IpPermissions: []ec2types.IpPermission{
			{
				FromPort:   aws.Int32(0),
				ToPort:     aws.Int32(0),
				IpProtocol: aws.String("-1"),
				IpRanges: []ec2types.IpRange{
					{
						CidrIp: aws.String("0.0.0.0/0"),
					},
				},
			},
		},
	})
	require.NoError(t, err)
	
	_, err = ec2Client.RevokeSecurityGroupEgress(context.TODO(), &ec2.RevokeSecurityGroupEgressInput{
		GroupId: sgId,
		IpPermissions: []ec2types.IpPermission{
			{
				FromPort:   aws.Int32(0),
				ToPort:     aws.Int32(0),
				IpProtocol: aws.String("-1"),
				Ipv6Ranges: []ec2types.Ipv6Range{
					{
						CidrIpv6: aws.String("::/0"),
					},
				},
			},
		},
	})
	require.NoError(t, err)
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
