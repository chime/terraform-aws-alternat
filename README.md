# HA NAT Instances

NAT Gateways are dead. Long live NAT instances!

![build](https://github.com/1debit/alternat/actions/workflows/main.yaml/badge.svg)

## Background

On AWS, [NAT devices](https://docs.aws.amazon.com/vpc/latest/userguide/vpc-nat.html) are required for accessing the Internet from private VPC subnets. Usually, the best option is a NAT gateway, a fully managed NAT service. The [pricing structure of NAT gateway](https://aws.amazon.com/vpc/pricing/) includes charges of $.045 per hour per NAT Gateway, plus **$.045 per GB** processed. The former charge is reasonable at about $32.40 per month. However, the latter charge can be *extremely* expensive for larger traffic volumes.

In addition to the direct NAT Gateway charges, there are also Data Transfer charges for outbound traffic leaving AWS (known as egress traffic). The cost varies depending on destination and volume, ranging from $0.09/GB to $0.01 per GB (after a free tier of 100GB). That’s right: traffic traversing the NAT Gateway is first charged for processing, then charged again for egress to the Internet.

Consider, for instance, the cost of sending 1PB to and from the Internet through a NAT Gateway - not an unusual amount for some use cases - is $75,604. Many customers may be dealing with far less than 1PB, but the cost can be high even at relatively lower traffic volumes. This drawback of NAT gateway is [widely](https://www.lastweekinaws.com/blog/the-aws-managed-nat-gateway-is-unpleasant-and-not-recommended/) [lamented](https://www.cloudforecast.io/blog/aws-nat-gateway-pricing-and-cost/) [among](https://www.vantage.sh/blog/nat-gateway-vpc-endpoint-savings) [AWS users](https://www.stephengrier.com/reducing-the-cost-of-aws-nat-gateways/).

Plug in the numbers to the [AWS Pricing Calculator](https://calculator.aws/#/estimate?id=25774f7303040fde173fe274a8dd6ef268a16087) and you may well be flabbergasted. Rather than 1PB, which may be less relatable for some users, let’s choose a nice, relatively low round number as an example. Say, 10TB. The cost of sending 10TB over the Internet (5GB ingress, 5TB egress) through NAT Gateway works out to $954 per month, or $11,448 per year.

Unlike NAT Gateways, NAT instances do not suffer from data processing charges. With NAT instances, you pay for:

1. The cost of the EC2 instances
1. [Data transfer](https://aws.amazon.com/ec2/pricing/on-demand/#Data_Transfer) out of AWS (the same as NAT Gateway)
1. The operational expense of maintaining EC2 instances

Of these, at scale, outbound data transfer (egress from your AWS resources to the Internet) is the most significant. Outbound data transfer is priced on a sliding scale based on the amount of traffic. Inbound data transfer is free. It is this asymmetry that this project leverages to save on the punishing data processing charges of NAT Gateway.

Consider the cost of transferring that same 5TB inbound and 5TB outbound through a NAT instance. Using the EC2 Data Transfer sliding scale for egress traffic and a `c6gn.large` NAT instance (optimized for networking), the cost comes to about $526. This is a $428 per month savings (~45%) compared to the NAT Gateway. The more data processed - especially on the ingress side - the higher the savings.

NAT instances aren't for everyone. You might benefit from this project if NAT Gateway data processing costs are a significant item on your AWS bill. If the hourly cost of the NAT instances and/or the NAT Gateways are a material line item on your bill, this project is probably not for you. As a rule of thumb, assuming a roughly equal volume of ingress/egress traffic, and considering the slight overhead of operating NAT instances, you might save money using this solution if you are processing more than 10TB per month with NAT Gateway.

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

Both are deployed by the Terraform module located in [`modules/terraform-aws-alternat`](modules/terraform-aws-alternat).

### NAT instance Auto Scaling Group and standby NAT Gateway

The solution deploys an Auto Scaling Group (ASG) for each provided public subnet. Each ASG contains a single instance. When the instance boots, the [user data](modules/terraform-aws-alternat/alternat.sh.tftpl) initializes the instance to do the NAT stuff.

By default, the ASGs are configured with a [maximum instance lifetime](https://docs.aws.amazon.com/autoscaling/ec2/userguide/asg-max-instance-lifetime.html). This is to facilitate periodic replacement of the instance to automate patching. When the maximum instance lifetime is reached (14 days by default), the following occurs:

1. The instance is terminated by the Auto Scaling service.
1. A [`Terminating:Wait` lifecycle hook](https://docs.aws.amazon.com/autoscaling/ec2/userguide/lifecycle-hooks.html) fires to an SNS topic.
1. The replace-route function updates the route table of the corresponding private subnet to instead route through a standby NAT Gateway.
1. When the new instance boots, its user data automatically reclaims the Elastic IP address and updates the route table to route through itself.

The standby NAT Gateway is a safety measure. It is only used if the NAT instance is actively being replaced, either due to the maximum instance lifetime or due to some other failure scenario.

### replace-route Lambda Function

The purpose of [the replace-route Lambda Function](functions/replace-route) is to update the route table of the private subnets to route through the standby NAT gateway. It does this in response to two events:

1. By the lifecycle hook (via SNS topic) when the ASG terminates a NAT instance (such as when the max instance lifetime is reached), and
1. by a CloudWatch Event rule, once per minute for every private subnet.

When a NAT instance in any of the zonal ASGs is terminated, the lifecycle hook publishes an event to an SNS topic to which the Lambda function is subscribed. The Lambda then performs the necessary steps to identify which zone is affected and updates the respective private route table to point at its standby NAT gateway.

The replace-route function also acts as a health check. Every minute, in the private subnet of each availability zone, the function checks that connectivity to the Internet works by requesting https://www.example.com and, if that fails, https://www.google.com. If the request succeeds, the function exits. If both requests fail, the NAT instance is presumably borked, and the function updates the route to point at the standby NAT gateway.

In the event that a NAT instance is unavailable, the function would have no route to the AWS EC2 and Lambda APIs to perform the necessary steps to update the route table. This is mitigated by the use of [interface VPC endpoints](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/interface-vpc-endpoints.html) to EC2 and Lambda.

## Drawbacks

No solution is without its downsides. To understand the primary drawback of this design, a brief discussion about how NAT works is warranted.

NAT stands for Network Address Translation. NAT devices act as proxies, allowing hosts in private networks to communicate over the Internet without public, Internet-routable addresses. They have a network presence in both the private network and on the Internet. NAT devices accept connections from hosts on the private network, mark the connection in a translation table, then open a corresponding connection to the destination using their public-facing Internet connection.

![NAT Translation Table](/assets/nat-table.png)

The table, typically stored in memory on the NAT device, tracks the state of open connections. If the state is lost or changes abruptly, the connections will be unexpectedly closed. Processes on clients in the private network with open connections to the Internet will need to reopen the connection.

In the design described above, we intentionally terminate the NAT instance for automated patching. The connection fails over to the NAT Gateway, then back to the newly launched, freshly patched NAT instance. Any connections that are open during either change - from NAT instance to Gateway, and back again - are closed.

Notably, connectivity to the Internet is never lost. A route to the Internet is available at all times.

For our use case, and for many others, the compromise is acceptable. Many clients will open new connections. Other clients may use primarily short-lived connections that retry after a failure. For some use cases - for example, file transfers, or other operations that are unable to recover from failures - this drawback may be unacceptable.

The Internet is unreliable by design, so failure modes such as connection loss should be a consideration in any system designed for high availability.

## Usage and Considerations

There are two high level steps to using this project:

1. Build and push the container image using the [`Dockerfile`](Dockerfile).
1. Use the Terraform module to deploy all the things.

Use this project directly, as provided, or draw inspiration from it and use only the parts you need. We cut [releases](https://github.com/1debit/alternat/releases) following the [Semantic Versioning](https://semver.org/) method. We recommend pinning to our tagged releases or using the short commit SHA if you decide to use this repo directly.

### Building and pushing the container image

We do not provide a public image, so you'll need to build an image and push it to the registry and repo of your choice. [Amazon ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/what-is-ecr.html) is the obvious choice.

```
docker build . -t <your_registry_url>/<your_repo:<release tag or short git commit sha>
docker push <your_registry_url>/<your_repo:<release tag or short git commit sha>
```

### Use the Terraform module

Start by reviewing the available [input variables](modules/terraform-aws-alternat/variables.tf). Example usage:

```
module "alternat_instances" {
  source = "1debit/alternat//modules/terraform-aws-alternat?ref=v0.1.0"

  alternat_image_uri = "0123456789012.dkr.ecr.us-east-1.amazonaws.com/alternat-functions-lambda"
  alternat_image_tag = "v0.1.0"

  ingress_security_group_ids = var.ingress_security_group_ids

  subnet_suffix = var.nat_subnet_suffix

  private_route_table_ids = module.vpc.private_route_table_ids

  tags = var.tags

  vpc_id                 = module.vpc.vpc_id
  vpc_private_subnet_ids = module.vpc.private_subnets
  vpc_public_subnet_ids  = module.vpc.public_subnets
}
```

Feel free to submit a pull request or create an issue if you need an input or output that isn't available.

### Other considerations

- We recommend using a network optimized instance type, such as the `c5gn.8xlarge` which offers 50Gbps guaranteed bandwidth. It's wise to start by overprovisioning, observing patterns, and resizing if necessary. Don't be surprised by the network I/O credit mechanism explained in [the AWS EC2 docs](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-network-bandwidth.html) thusly:

> Typically, instances with 16 vCPUs or fewer (size 4xlarge and smaller) are documented as having "up to" a specified bandwidth; for example, "up to 10 Gbps". These instances have a baseline bandwidth. To meet additional demand, they can use a network I/O credit mechanism to burst beyond their baseline bandwidth. Instances can use burst bandwidth for a limited time, typically from 5 to 60 minutes, depending on the instance size.

- The code is currently constrained to a 1:1 relationship of public subnets to private subnets. Each provided public subnet should correspond to a single private subnet in the same zone.

- The `subnet_suffix` is used to match the name of the private subnet and subsequently update the corresponding route table. The suffix of the private subnet names must match `<subnet suffix>-<availability zone>`. For example, `my-foo-vpc-private-us-east-1a`. This is the default used by the [terraform-aws-vpc module](https://github.com/terraform-aws-modules/terraform-aws-vpc/blob/6a3a9bde634e2147205273337b1c22e4d94ad6ff/main.tf#L402).

- [SSM Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html) is enabled by default. To view NAT connections on an instance, use sessions manager to connect, then run `sudo cat /proc/net/nf_conntrack`. Disable SSM by setting `enable_ssm=false`.

- We intentionally use `most_recent=true` for the Amazon Linux 2 AMI. This helps to ensure that the latest AMI is used in the ASG launch template. If a new AMI is available when you run `terraform apply`, the launch template will be updated with the latest AMI. The new AMI will be launched automatically when the maximum instance lifetime is reached.

- Most of the time, except when the instance is actively being replaces, NAT traffic should be routed through the NAT instance and NOT through the NAT Gateway. You should monitor your logs for the text "Failed connectivity tests! Replacing route" and alert when this occurs as you may need to manually intervene to resolve a problem with the NAT instances.

- There are four Elastic IP addresses for the NAT instances and four for the NAT Gateways. Be sure to add all eight addresses to any external allow lists if necessary.


## Contributing

[Issues](https://github.com/issues) and [pull requests](https://github.com/1debit/alternat/pulls) are most welcome!

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
