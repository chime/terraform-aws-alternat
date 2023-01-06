output "nat_instance_eips" {
  description = "List of Elastic IP addresses used by the NAT instances. This will be empty if EIPs are provided in var.nat_instance_eip_ids."
  value       = local.reuse_nat_instance_eips ? [] : aws_eip.nat_instance_eips[*].public_ip
}

output "nat_gateway_eips" {
  description = "List of Elastic IP addresses used by the standby NAT gateways."
  value = [
    for eip in aws_eip.nat_gateway_eips
    : eip.public_ip
    if var.create_nat_gateways
  ]
}
