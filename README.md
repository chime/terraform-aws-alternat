# HA NAT Instances

NAT Gateways are dead. Long live NAT instances!

![build](https://github.com/1debit/ha-nat/actions/workflows/main.yaml/badge.svg)

## Background

On AWS, [NAT devices](https://docs.aws.amazon.com/vpc/latest/userguide/vpc-nat.html) are required for accessing the Internet from private VPC subnets. Usually, the best option is a NAT gateway, a fully managed NAT service. The [pricing structure of NAT gateway](https://aws.amazon.com/vpc/pricing/) includes charges of $.045 per hour per NAT Gateway, plus **$.045 per GB** processed. The former charge is reasonable at about $32.04 per month. However, the latter charge can be *extremely* expensive for larger traffic volumes. For example, the cost of processing 1PB through a NAT Gateway - not an unusual amount for some use cases - is $45,000. This drawback of NAT gateway is [widely](https://www.lastweekinaws.com/blog/the-aws-managed-nat-gateway-is-unpleasant-and-not-recommended/) [lamented](https://www.cloudforecast.io/blog/aws-nat-gateway-pricing-and-cost/) [among](https://www.vantage.sh/blog/nat-gateway-vpc-endpoint-savings) [AWS users](https://www.stephengrier.com/reducing-the-cost-of-aws-nat-gateways/).

This project is a high availability implementation of [NAT instances](https://docs.aws.amazon.com/vpc/latest/userguide/VPC_NAT_Instance.html), a self-managed alternative to NAT gateways. Unlike NAT Gateways, NAT instances do not suffer from data processing charges. With NAT instances, you pay for:

1. The cost of the instances (the exact amount depends on the type of instance)
1. [Data transfer](https://aws.amazon.com/ec2/pricing/on-demand/#Data_Transfer) out of AWS
1. The operational cost of maintaining EC2 instances

Outbound data transfer (egress from your AWS resources to the Internet) is priced on a sliding scale based on the amount of traffic. Inbound data transfer is free. It is this asymmetry that this project leverages to save on the punishing data processing charges of NAT Gateway.

Consider the cost of transferring 500GB inbound and 500GB outbound through a NAT instance. Using the EC2 Data Transfer sliding scale for egress traffic and a `c6g.xlarge` NAT instance, the cost comes to less than $30,000. This is about a $15,000 per month savings compared to the NAT Gateway.

NAT instances aren't for everyone. You might benefit from this project if:

* NAT Gateway data processing costs are a signifiant item on your AWS bill, and
* you process significant *ingress* traffic through NAT Gateways, or
* you are a large enterprise with a Data Transfer Private Pricing agreement with AWS

If the hourly cost of the NAT instances and/or the NAT Gateways are a material line item on your AWS bill, this project will not reduce your costs. As a rule of thumb, assuming a roughly equal volume of ingress/egress traffic, you might save money using this solution if you are processing more than 150TB per month with NAT Gateway. The higher the volume, especially the volume of ingress traffic, the greater the savings.

Features:

* Self-provisioned NAT instances in Auto Scaling Groups
* Standby NAT Gateways with health checks and automated failover
* Vanilla Amazon Linux 2 AMI (no AMI management requirement)
* Optional use of SSM for connecting to the NAT instances
* Max instance lifetimes (no long-lived instances!) with automated failover
* A Terraform module to set everything up
* Compatibility with the default naming convention used by the open source [terraform-aws-vpc Terraform module](https://github.com/terraform-aws-modules/terraform-aws-vpc/blob/master/variables.tf)

![Architecture diagram](/assets/architecture.png)

## Local Testing

To test locally, install the AWS SAM CLI client:

```
brew tap aws/tap
brew install aws-sam-cli
```

Build sam and invoke the functions:

```
sam build
sam local invoke <FUNCTION NAME> -e <event_filename>.json
```

Example:

```
cd functions/replace-route
sam local invoke AutoScalingTerminationFunction -e sns-event.json
sam local invoke ConnectivityTestFunction -e cloudwatch-event.json
```


## Making actual calls to AWS for testing

In the first terminal

```
cd functions/replace-route
sam build && sam local start-lambda # This will start up a docker container running locally
```

In a second terminal, invoke the function back in terminal one:

```
cd functions/replace-route
aws lambda invoke --function-name "AutoScalingTerminationFunction" --endpoint-url "http://127.0.0.1:3001" --region us-east-1 --cli-binary-format raw-in-base64-out --payload file://./sns-event.json --no-verify-ssl out.txt
aws lambda invoke --function-name "ConnectivityTestFunction" --endpoint-url "http://127.0.0.1:3001" --region us-east-1 --cli-binary-format raw-in-base64-out --payload file://./cloudwatch-event.json --no-verify-ssl out.txt
```
