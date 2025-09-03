data "aws_availability_zones" "available" {}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 4"

  name                  = var.vpc_name
  cidr                  = var.vpc_cidr
  secondary_cidr_blocks = [var.vpc_secondary_cidr]
  private_subnets       = var.private_subnets
  public_subnets        = var.public_subnets
  azs                   = local.azs
  enable_nat_gateway    = var.enable_nat_gateway
}

resource "aws_subnet" "secondary_subnets" {
  count             = length(var.vpc_secondary_subnets)
  vpc_id            = module.vpc.vpc_id
  cidr_block        = var.vpc_secondary_subnets[count.index]
  availability_zone = local.azs[count.index]
}

resource "aws_route_table_association" "secondary_subnets" {
  count          = length(var.vpc_secondary_subnets)
  subnet_id      = aws_subnet.secondary_subnets[count.index].id
  route_table_id = module.vpc.private_route_table_ids[count.index]
}

data "aws_subnet" "subnet" {
  count = length(module.vpc.private_subnets)
  id    = module.vpc.private_subnets[count.index]
}

locals {
  vpc_az_maps = [
    for index, rt in module.vpc.private_route_table_ids
    : {
      az               = local.azs[index]
      route_table_ids  = [rt]
      public_subnet_id = module.vpc.public_subnets[index]
      # The secondary subnets do not need to be included here. this data is
      # used for the connectivity test function and VPC endpoint which are
      # only needed in one subnet per zone.
      private_subnet_ids = [module.vpc.private_subnets[index]]
    }
  ]
}

module "alternat" {
  # To use Alternat from the Terraform Registry:
  # source = "chime/alternat/aws"
  source = "./.."

  create_nat_gateways                = var.create_nat_gateways
  ingress_security_group_cidr_blocks = var.private_subnets
  vpc_az_maps                        = local.vpc_az_maps
  vpc_id                             = module.vpc.vpc_id

  lambda_package_type = "Zip"

  nat_instance_type     = var.alternat_instance_type
  nat_instance_key_name = var.nat_instance_key_name
  enable_nat_restore    = var.enable_nat_restore
  enable_ssm            = var.enable_ssm
}
