# HA NAT Instances

NAT Gateways are dead. Long live NAT instances!

![build](https://github.com/1debit/ha-nat/actions/workflows/main.yaml/badge.svg)

## Background

On AWS, [NAT devices](https://docs.aws.amazon.com/vpc/latest/userguide/vpc-nat.html) are required for accessing the Internet from private VPC subnets. Usually, the best option is a NAT gateway, a fully managed NAT service. The [pricing structure of NAT gateway](https://aws.amazon.com/vpc/pricing/) includes charges of $.045 per hour per NAT Gateway, plus **$.045 per GB** processed. The former charge is reasonable at about $32.40 per month. However, the latter charge can be *extremely* expensive for larger traffic volumes. For example, the cost of processing 1PB through a NAT Gateway - not an unusual amount for some use cases - is $45,000. This drawback of NAT gateway is [widely](https://www.lastweekinaws.com/blog/the-aws-managed-nat-gateway-is-unpleasant-and-not-recommended/) [lamented](https://www.cloudforecast.io/blog/aws-nat-gateway-pricing-and-cost/) [among](https://www.vantage.sh/blog/nat-gateway-vpc-endpoint-savings) [AWS users](https://www.stephengrier.com/reducing-the-cost-of-aws-nat-gateways/).

This project is a high availability implementation of [NAT instances](https://docs.aws.amazon.com/vpc/latest/userguide/VPC_NAT_Instance.html), a self-managed alternative to NAT gateways. Unlike NAT Gateways, NAT instances do not suffer from data processing charges. With NAT instances, you pay for:

1. The cost of the EC2 instances
1. [Data transfer](https://aws.amazon.com/ec2/pricing/on-demand/#Data_Transfer) out of AWS
1. The operational expense of maintaining EC2 instances

Of these, at scale, outbound data transfer (egress from your AWS resources to the Internet) is the most significant. Outbound data transfer is priced on a sliding scale based on the amount of traffic. Inbound data transfer is free. It is this asymmetry that this project leverages to save on the punishing data processing charges of NAT Gateway.

Consider the cost of transferring 500TB inbound and 500TB outbound through a NAT instance. Using the EC2 Data Transfer sliding scale for egress traffic and a `c6gn.2xlarge` NAT instance (optimized for networking), the cost comes to about than $32,800. This is a $12,200 per month savings compared to the NAT Gateway. The higher the volume, the greater the savings, especially so if the traffic is heavily inbound or if you have a Data Transfer Private Pricing agreement with AWS.

NAT instances aren't for everyone. You might benefit from this project if:

* NAT Gateway data processing costs are a significant item on your AWS bill, and
* you process significant *ingress* traffic through NAT Gateways, or
* you are a large enterprise with a Data Transfer Private Pricing agreement with AWS

If the hourly cost of the NAT instances and/or the NAT Gateways are a material line item on your AWS bill, this project is probably not for you. As a rule of thumb, assuming a roughly equal volume of ingress/egress traffic, you might save money using this solution if you are processing more than 150TB per month with NAT Gateway.

Features:

* Self-provisioned NAT instances in Auto Scaling Groups
* Standby NAT Gateways with health checks and automated failover, facilitated by a Lambda function
* Vanilla Amazon Linux 2 AMI (no AMI management requirement)
* Optional use of SSM for connecting to the NAT instances
* Max instance lifetimes (no long-lived instances!) with automated failover
* A Terraform module to set everything up
* Compatibility with the default naming convention used by the open source [terraform-aws-vpc Terraform module](https://github.com/terraform-aws-modules/terraform-aws-vpc/blob/master/variables.tf)

Read on to learn more about the project.

## Architecture overview

![Architecture diagram](/assets/architecture.png)

The two main elements of the NAT instance solution are:

1. The NAT instance Auto Scaling Groups, one per zone, with a corresponding standby NAT Gateway
1. The replace-route Lambda function

Both are deployed by the Terraform module located in [`modules/terraform-aws-ha-nat`](modules/terraform-aws-ha-nat).

### NAT instance Auto Scaling Group and standby NAT Gateway

The solution deploys an Auto Scaling Group (ASG) for each provided public subnet. Each ASG contains a single instance. When the instance boots, the [user data](modules/terraform-aws-ha-nat/ha-nat.sh.tftpl) initializes the instance to do the NAT stuff.

By default, the ASGs are configured with a [maximum instance lifetime](https://docs.aws.amazon.com/autoscaling/ec2/userguide/asg-max-instance-lifetime.html). This is to facilitate periodic replacement of the instance to automate patching. When the maximum instance lifetime is reached (14 days by default), the following occurs:

1. The instance is terminated by the Auto Scaling service.
1. A [`Terminating:Wait` lifecycle hook](https://docs.aws.amazon.com/autoscaling/ec2/userguide/lifecycle-hooks.html) fires to an SNS topic.
1. The replace-route function updates the route table of the corresponding private subnet to instead route through a standby NAT Gateway.
1. When the new instance boots, its user data automatically reclaims the Elastic IP address and updates the route table to route through itself.

The standby NAT Gateway is a safety measure. It is only used if the NAT instance is actively being replaced, either due to the maximum instance lifetime or due to some other failure scenario.

### replace-route Lambda Function

The purpose of [the replace-route Lambda Function](`functions/replace-route/app.py`) is to update the route table of the private subnets to route through the standby NAT gateway. It does this in response to two events:

1. By the lifecycle hook (via SNS topic) when the ASG terminates a NAT instance (such as when the max instance lifetime is reached), and
1. by a CloudWatch Event rule, once per minute for every private subnet.

When a NAT instance in any of the zonal ASGs is terminated, the lifecycle hook publishes an event to an SNS topic to which the Lambda function is subscribed. The Lambda then performs the necessary steps to identify which zone is affected and updates the respective private route table to point at its standby NAT gateway.

The replace-route function also acts as a health check. In the private subnet of each availability zone, every minute, the function checks that connectivity to the Internet works by requesting https://www.example.com (and, if that fails, https://www.google.com). If the request succeeds, the function exits. If both requests fail, the NAT instance is presumably borked, and the function updates the route to point at the standby NAT gateway.

In the event that a NAT instance is unavailable, the function would have no route to the AWS EC2 and Lambda APIs to perform the necessary steps to update the route table. This is mitigated by the use of [interface VPC endpoints](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/interface-vpc-endpoints.html) to EC2 and Lambda.

> **_NOTE:_** When a route replacement occurs, all active NAT connections will be disconnected and will need to be reestablished. For example, when the NAT instance max lifetime is reached, the connections will be terminated and reestablished through the NAT Gateway after replace-route has fired. When the new instances comes online and reclaims the route, the connections will again be closed. Clients will need to reopen connections.

## Usage and Considerations

There are two high level steps to using this project:

1. Build and push the container image using the [`Dockerfile`](Dockerfile).
1. Use the Terraform module to deploy all the things.

Use this project directly, as provided, or draw inspiration from it and use only the parts you need. We cut [releases](https://github.com/1debit/ha-nat/releases) following the [Semantic Versioning](https://semver.org/) method. We recommend pinning to our tagged releases or using the short commit SHA if you decide to use this repo directly.

### Building and pushing the container image

We do not provide a public image, so you'll need to build an image and push it to the registry and repo of your choice. [Amazon ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/what-is-ecr.html) is the obvious choice.

```
docker build . -t <your_registry_url>/<your_repo:<release tag or short git commit sha>
docker push <your_registry_url>/<your_repo:<release tag or short git commit sha>
```

### Use the Terraform module

Start by reviewing the available [input variables](modules/terraform-aws-ha-nat/variables.tf). Feel free to submit a pull request or create an issue if you need an input or output that isn't available.

A few comments on the inputs:

- We recommend using a network optimized instance type, such as the `c5gn.8xlarge` which offers 50Gbps guaranteed bandwidth. It's wise to start by overprovisioning, observing patterns, and resizing if necessary. Don't be surprised by the network I/O credit mechanism explained in [the AWS EC2 docs](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-network-bandwidth.html) thusly:

> Typically, instances with 16 vCPUs or fewer (size 4xlarge and smaller) are documented as having "up to" a specified bandwidth; for example, "up to 10 Gbps". These instances have a baseline bandwidth. To meet additional demand, they can use a network I/O credit mechanism to burst beyond their baseline bandwidth. Instances can use burst bandwidth for a limited time, typically from 5 to 60 minutes, depending on the instance size.

- The code is currently constrained to a 1:1 relationship of public subnets to private subnets. Each provided public subnet should correspond to a single private subnet in the same zone.

- The `subnet_suffix` is used to match the name of the private subnet and subsequently update the corresponding route table. The suffix of the private subnet names must match `<subnet suffix>-<availability zone>`. For example, `my-foo-vpc-private-us-east-1a`. This is the default used by the [terraform-aws-vpc module](https://github.com/terraform-aws-modules/terraform-aws-vpc/blob/6a3a9bde634e2147205273337b1c22e4d94ad6ff/main.tf#L402).

- [SSM Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html) is enabled by default. To view NAT connections on an instance, use sessions manager to connect, then run `sudo cat /proc/net/nf_conntrack`. Disable SSM by setting `enable_ssm=false`.

- We intentionally use `most_recent=true` for the Amazon Linux 2 AMI. This helps to ensure that the latest AMI is used in the ASG launch template. If a new AMI is available when you run `terraform apply`, the launch template will be updated with the latest AMI. The new AMI will be launched automatically when the maximum instance lifetime is reached.


## Contributing

[Issues](https://github.com/issues) and [pull requests](https://github.com/1debit/ha-nat/pulls) are most welcome!

This project is intended to be a safe, welcoming space for collaboration. Contributors are expected to adhere to the [Contributor Covenant code of conduct](CODE_OF_CONDUCT.md).


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
