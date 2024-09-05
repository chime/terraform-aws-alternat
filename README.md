# alterNAT

NAT Gateways are dead. Long live NAT instances!

Built and released with 💚 by <a href="https://chime.com"><img src="/assets/Chime_company_logo.png" alt="Chime Engineering" width="60"/></a>


[![Test](https://github.com/chime/terraform-aws-alternat/actions/workflows/test.yaml/badge.svg)](https://github.com/chime/terraform-aws-alternat/actions/workflows/test.yaml)


## Background

On AWS, [NAT devices](https://docs.aws.amazon.com/vpc/latest/userguide/vpc-nat.html) are required for accessing the Internet from private VPC subnets. Usually, the best option is a NAT gateway, a fully managed NAT service. The [pricing structure of NAT gateway](https://aws.amazon.com/vpc/pricing/) includes charges of $0.045 per hour per NAT Gateway, plus **$0.045 per GB** processed. The former charge is reasonable at about $32.40 per month. However, the latter charge can be *extremely* expensive for larger traffic volumes.

In addition to the direct NAT Gateway charges, there are also Data Transfer charges for outbound traffic leaving AWS (known as egress traffic). The cost varies depending on destination and volume, ranging from $0.09/GB to $0.01 per GB (after a free tier of 100GB). That’s right: traffic traversing the NAT Gateway is first charged for processing, then charged again for egress to the Internet.

Consider, for instance, the cost of sending 1PB to and from the Internet through a NAT Gateway - not an unusual amount for some use cases - is $75,604. Many customers may be dealing with far less than 1PB, but the cost can be high even at relatively lower traffic volumes. This drawback of NAT gateway is [widely](https://www.lastweekinaws.com/blog/the-aws-managed-nat-gateway-is-unpleasant-and-not-recommended/) [lamented](https://www.cloudforecast.io/blog/aws-nat-gateway-pricing-and-cost/) [among](https://www.vantage.sh/blog/nat-gateway-vpc-endpoint-savings) [AWS users](https://www.stephengrier.com/reducing-the-cost-of-aws-nat-gateways/).

Plug in the numbers to the [AWS Pricing Calculator](https://calculator.aws/#/estimate?id=25774f7303040fde173fe274a8dd6ef268a16087) and you may well be flabbergasted. Rather than 1PB, which may be less relatable for some users, let’s choose a nice, relatively low round number as an example. Say, 10TB. The cost of sending 10TB over the Internet (5TB ingress, 5TB egress) through NAT Gateway works out to $954 per month, or $11,448 per year.

Unlike NAT Gateways, NAT instances do not suffer from data processing charges. With NAT instances, you pay for:

1. The cost of the EC2 instances
1. [Data transfer](https://aws.amazon.com/ec2/pricing/on-demand/#Data_Transfer) out of AWS (the same as NAT Gateway)
1. The operational expense of maintaining EC2 instances

Of these, at scale, data transfer is the most significant. NAT instances are subject to the same data transfer sliding scale as NAT Gateways. Inbound data transfer is free, and most importantly, there is no $0.045 per GB data processing charge.

Consider the cost of transferring that same 5TB inbound and 5TB outbound through a NAT instance. Using the EC2 Data Transfer sliding scale for egress traffic and a `c6gn.large` NAT instance (optimized for networking), the cost comes to about $526. This is a $428 per month savings (~45%) compared to the NAT Gateway. The more data processed - especially on the ingress side - the higher the savings.

NAT instances aren't for everyone. You might benefit from alterNAT if NAT Gateway data processing costs are a significant item on your AWS bill. If the hourly cost of the NAT instances and/or the NAT Gateways are a material line item on your bill, alterNAT is probably not for you. As a rule of thumb, assuming a roughly equal volume of ingress/egress traffic, and considering the slight overhead of operating NAT instances, you might save money using this solution if you are processing more than 10TB per month with NAT Gateway.

Features:

* Self-provisioned NAT instances in Auto Scaling Groups
* Standby NAT Gateways with health checks and automated failover, facilitated by a Lambda function
* Vanilla Amazon Linux 2 AMI (no AMI management requirement)
* Optional use of SSM for connecting to the NAT instances
* Max instance lifetimes (no long-lived instances!) with automated failover
* A Terraform module to set everything up
* Compatibility with the default naming convention used by the open source [terraform-aws-vpc Terraform module](https://github.com/terraform-aws-modules/terraform-aws-vpc/blob/master/variables.tf)

Read on to learn more about alterNAT.

## Architecture Overview

![Architecture diagram](/assets/architecture.png)

The two main elements of the NAT instance solution are:

1. The NAT instance Auto Scaling Groups, one per zone, with a corresponding standby NAT Gateway
1. The replace-route Lambda function

Both are deployed by the Terraform module.

### NAT Instance Auto Scaling Group and Standby NAT Gateway

The solution deploys an Auto Scaling Group (ASG) for each provided public subnet. Each ASG contains a single instance. When the instance boots, the [user data](alternat.sh.tftpl) initializes the instance to do the NAT stuff.

By default, the ASGs are configured with a [maximum instance lifetime](https://docs.aws.amazon.com/autoscaling/ec2/userguide/asg-max-instance-lifetime.html). This is to facilitate periodic replacement of the instance to automate patching. When the maximum instance lifetime is reached (14 days by default), the following occurs:

1. The instance is terminated by the Auto Scaling service.
1. A [`Terminating:Wait` lifecycle hook](https://docs.aws.amazon.com/autoscaling/ec2/userguide/lifecycle-hooks.html) fires to an SNS topic.
1. The replace-route function updates the route table of the corresponding private subnet to instead route through a standby NAT Gateway.
1. When the new instance boots, its user data automatically reclaims the Elastic IP address and updates the route table to route through itself.

The standby NAT Gateway is a safety measure. It is only used if the NAT instance is actively being replaced, either due to the maximum instance lifetime or due to some other failure scenario.

### `replace-route` Lambda Function

The purpose of [the replace-route Lambda Function](functions/replace-route) is to update the route table of the private subnets to route through the standby NAT gateway. It does this in response to two events:

1. By the lifecycle hook (via SNS topic) when the ASG terminates a NAT instance (such as when the max instance lifetime is reached), and
1. by a CloudWatch Event rule, once per minute for every private subnet.

When a NAT instance in any of the zonal ASGs is terminated, the lifecycle hook publishes an event to an SNS topic to which the Lambda function is subscribed. The Lambda then performs the necessary steps to identify which zone is affected and updates the respective private route table to point at its standby NAT gateway.

The replace-route function also acts as a health check. Every minute, in the private subnet of each availability zone, the function checks that connectivity to the Internet works by requesting https://www.example.com and, if that fails, https://www.google.com. If the request succeeds, the function exits. If both requests fail, the NAT instance is presumably borked, and the function updates the route to point at the standby NAT gateway.

In the event that a NAT instance is unavailable, the function would have no route to the AWS EC2 API to perform the necessary steps to update the route table. This is mitigated by the use of an [interface VPC endpoint](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/interface-vpc-endpoints.html) to EC2.

## Drawbacks

No solution is without its downsides. To understand the primary drawback of this design, a brief discussion about how NAT works is warranted.

NAT stands for Network Address Translation. NAT devices act as proxies, allowing hosts in private networks to communicate over the Internet without public, Internet-routable addresses. They have a network presence in both the private network and on the Internet. NAT devices accept connections from hosts on the private network, mark the connection in a translation table, then open a corresponding connection to the destination using their public-facing Internet connection.

![NAT Translation Table](/assets/nat-table.png)

The table, typically stored in memory on the NAT device, tracks the state of open connections. If the state is lost or changes abruptly, the connections will be unexpectedly closed. Processes on clients in the private network with open connections to the Internet will need to open new connections.

In the design described above, NAT instances are intentionally terminated for automated patching. The route is updated to use the NAT Gateway, then back to the newly launched, freshly patched NAT instance. During these changes the NAT table is lost. Established TCP connections present at the time of the change will still appear to be open on both ends of the connection (client and server) because no TCP FIN or RST has been sent, but will in fact be closed because the table is lost and the public IP address of the NAT has changed.

Importantly, **connectivity to the Internet is never lost**. A route to the Internet is available at all times.

For our use case, and for many others, this limitation is acceptable. Many clients will open new connections. Other clients may use primarily short-lived connections that retry after a failure.

For some use cases - for example, file transfers, or other operations that are unable to recover from failures - this drawback may be unacceptable. In this case, the max instance lifetime can be disabled, and route changes would only occur in the unlikely event that a NAT instance failed for another reason, in which case the connectivity checker automatically redirects through the NAT Gateway.

[The Internet is unreliable](https://en.wikipedia.org/wiki/Fallacies_of_distributed_computing), so failure modes such as connection loss should be a consideration in any resilient system.

### Edge Cases

As described above, alterNAT uses the [`ReplaceRoute` API](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_ReplaceRoute.html) (among others) to switch the route in the event of a NAT instance failure or Auto Scaling termination event. One possible failure scenario could occur where the EC2 control plane is for some reason not functional (e.g. an outage within AWS) and a NAT instance fails at the same time. The replace-route function may be unable to automatically switch the route to the NAT Gateway because the control plane is down. One mitigation would be to attempt to manually replace the route for the impacted subnet(s) using the CLI or console. However, if the control plane is in fact down and no APIs are working, waiting until the issue is resolved may be the only option.

## Usage and Considerations

There are two ways to deploy alterNAT:

- By building a Docker image and using AWS Lambda support for containers
- By using AWS Lambda runtime for Python directly

Use this project directly, as provided, or draw inspiration from it and use only the parts you need. We cut [releases](https://github.com/chime/terraform-aws-alternat/releases) following the [Semantic Versioning](https://semver.org/) method. We recommend pinning to our tagged releases or using the short commit SHA if you decide to use this repo directly.

### Building and Pushing the Container Image

Build and push the container image using the [`Dockerfile`](Dockerfile).

We do not provide a public image, so you'll need to build an image and push it to the registry and repo of your choice. [Amazon ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/what-is-ecr.html) is the obvious choice.

```
docker build . -t <your_registry_url>/<your_repo:<release tag or short git commit sha>
docker push <your_registry_url>/<your_repo:<release tag or short git commit sha>
```

### Use the Terraform Module

Start by reviewing the available [input variables](variables.tf).

Example usage using the [terraform module](https://registry.terraform.io/modules/chime/alternat/aws/latest):

```hcl
locals {
  vpc_az_maps = [
    for index, rt in module.vpc.private_route_table_ids : {
      az                 = data.aws_subnet.subnet[index].availability_zone
      route_table_ids    = [rt]
      public_subnet_id   = module.vpc.public_subnets[index]
      private_subnet_ids = [module.vpc.private_subnets[index]]
    }
  ]
}

data "aws_subnet" "subnet" {
  count = length(module.vpc.private_subnets)
  id    = module.vpc.private_subnets[count.index]
}

module "alternat_instances" {
  source  = "chime/alternat/aws"
  # It's recommended to pin every module to a specific version
  # version = "x.x.x"

  alternat_image_uri = "0123456789012.dkr.ecr.us-east-1.amazonaws.com/alternat-functions-lambda"
  alternat_image_tag = "v0.3.3"

  ingress_security_group_ids = var.ingress_security_group_ids

  lambda_package_type = "Image"

  # Optional EBS volume settings. If omitted, the AMI defaults will be used.
  nat_instance_block_devices = {
    xvda = {
      device_name = "/dev/xvda"
      ebs = {
        encrypted   = true
        volume_type = "gp3"
        volume_size = 20
      }
    }
  }

  tags = var.tags

  vpc_id      = module.vpc.vpc_id
  vpc_az_maps = local.vpc_az_maps
}
```

To use AWS Lambda runtime for Python, remove `alternat_image_*` inputs and set `lambda_package_type` to `Zip`, e.g.:

```hcl
module "alternat_instances" {
  ...
  lambda_package_type = "Zip"
  ...
}
```

The `nat_instance_user_data_post_install` variable allows you to run an additional script to be executed after the main configuration has been installed.

```hcl
module "alternat_instances" {
  ...
    nat_instance_user_data_post_install = templatefile("${path.root}/post_install.tpl", {
      VERSION_ENV = var.third_party_version
    })
  ...
}
```

Feel free to submit a pull request or create an issue if you need an input or output that isn't available.

#### Can I use my own NAT Gateways?

Yes, but with caveats. You can set `create_nat_gateways = false` and alterNAT will not create NAT Gateways or EIPs for the NAT Gateways. However, alterNAT needs to manage the route to the Internet (`0.0.0.0/0`) for the private route tables. You have to ensure that you do not have an `aws_route` resource that points to the NAT Gateway from the route tables that you want to route through the alterNAT instances.

If you are using the open source terraform-aws-vpc module, you can set `nat_gateway_destination_cidr_block` to a value that is unlikely to affect your network. For instance, you could set `nat_gateway_destination_cidr_block=192.0.2.0/24`, an example CIDR range as discussed in [RFC5735](https://www.rfc-editor.org/rfc/rfc5735). This way the terraform-aws-vpc module will create and manage the NAT Gateways and their EIPs, but will not set the route to the Internet.

AlterNATively, you can remove the NAT Gateways and their EIPs from your existing configuration and then `terraform import` them to allow alterNAT to manage them.

### Other Considerations

- Read [the Amazon EC2 instance network bandwidth page](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-network-bandwidth.html) carefully. In particular:

  > To other Regions, an internet gateway, Direct Connect, or local gateways (LGW) – Traffic can utilize up to 50% of the network bandwidth available to a current generation instance with a minimum of 32 vCPUs. Bandwidth for a current generation instance with less than 32 vCPUs is limited to 5 Gbps.

- Hence if you need more than 5Gbps, make sure to use an instance type with at least 32 vCPUs, and divide the bandwidth in half. So the `c6gn.8xlarge` which offers 50Gbps guaranteed bandwidth will have 25Gbps available for egress to other regions, an internet gateway, etc.

- It's wise to start by overprovisioning, observing patterns, and resizing if necessary. Don't be surprised by the network I/O credit mechanism explained in [the AWS EC2 docs](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-network-bandwidth.html) thusly:

  > Typically, instances with 16 vCPUs or fewer (size 4xlarge and smaller) are documented as having "up to" a specified bandwidth; for example, "up to 10 Gbps". These instances have a baseline bandwidth. To meet additional demand, they can use a network I/O credit mechanism to burst beyond their baseline bandwidth. Instances can use burst bandwidth for a limited time, typically from 5 to 60 minutes, depending on the instance size.

- [SSM Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html) is enabled by default. To view NAT connections on an instance, use sessions manager to connect, then run `sudo cat /proc/net/nf_conntrack`. Disable SSM by setting `enable_ssm=false`.

- We intentionally use `most_recent=true` for the Amazon Linux 2 AMI. This helps to ensure that the latest AMI is used in the ASG launch template. If a new AMI is available when you run `terraform apply`, the launch template will be updated with the latest AMI. The new AMI will be launched automatically when the maximum instance lifetime is reached.

- Most of the time, except when the instance is actively being replaces, NAT traffic should be routed through the NAT instance and NOT through the NAT Gateway. You should monitor your logs for the text "Failed connectivity tests! Replacing route" and alert when this occurs as you may need to manually intervene to resolve a problem with the NAT instances.

- There are four Elastic IP addresses for the NAT instances and four for the NAT Gateways. Be sure to add all eight addresses to any external allow lists if necessary.

- If you plan on running this in a dual stack network (IPv4 and IPv6), you may notice that it takes ~10 minutes for an alternat node to start. In that case, you can use the `nat_instance_user_data_pre_install` variable to prefer IPv4 over IPv6 before running any user data.

  ```tf
    nat_instance_user_data_pre_install = <<-EOF
      # Prefer IPv4 over IPv6
      echo 'precedence ::ffff:0:0/96 100' >> /etc/gai.conf
    EOF
  ```
- If you see errors like: `error connecting to https://www.google.com/: <urlopen error [Errno 97] Address family not supported by protocol>` in the connectivity tester logs, you can set `lambda_has_ipv6 = false`. This will cause the lambda to request IPv4 addresses only in DNS lookups.

- If you want to use just a single NAT Gateway for fallback, you can create it externally and provide its ID through the `nat_gateway_id` variable. Note that you will incur cross AZ traffic charges of $0.01/GB.

  ```tf
    create_nat_gateways = false
    nat_gateway_id      = "nat-..."
  ```

## Contributing

[Issues](https://github.com/chime/terraform-aws-alternat/issues) and [pull requests](https://github.com/chime/terraform-aws-alternat/pulls) are most welcome!

alterNAT is intended to be a safe, welcoming space for collaboration. Contributors are expected to adhere to the [Contributor Covenant code of conduct](CODE_OF_CONDUCT.md).


## Local Testing

### Terraform module testing

The `test/` directory uses the [Terratest](https://terratest.gruntwork.io/) library to run integration tests on the Terraform module. The test uses the example located in `examples/` to set up Alternat, runs validations, then destroys the resources. Unfortunately, because of how the [Lambda Hyperplane ENI](https://docs.aws.amazon.com/lambda/latest/dg/foundation-networking.html#foundation-nw-eni) deletion process works, this takes a very long time (about 35 minutes) to run.

### Lambda function testing

To test locally, install the AWS SAM CLI client:

```shell
brew tap aws/tap
brew install aws-sam-cli
```

Build sam and invoke the functions:

```shell
sam build
sam local invoke <FUNCTION NAME> -e <event_filename>.json
```

Example:

```shell
cd functions/replace-route
sam local invoke AutoScalingTerminationFunction -e sns-event.json
sam local invoke ConnectivityTestFunction -e cloudwatch-event.json
```


## Testing with SAM

In the first terminal

```shell
cd functions/replace-route
sam build && sam local start-lambda # This will start up a docker container running locally
```

In a second terminal, invoke the function back in terminal one:

```shell
cd functions/replace-route
aws lambda invoke --function-name "AutoScalingTerminationFunction" --endpoint-url "http://127.0.0.1:3001" --region us-east-1 --cli-binary-format raw-in-base64-out --payload file://./sns-event.json --no-verify-ssl out.txt
aws lambda invoke --function-name "ConnectivityTestFunction" --endpoint-url "http://127.0.0.1:3001" --region us-east-1 --cli-binary-format raw-in-base64-out --payload file://./cloudwatch-event.json --no-verify-ssl out.txt
```
