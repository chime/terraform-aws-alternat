output "nat_instance_eips" {
  description = "List of Elastic IP addresses created for the NAT instances."
  value       = [for eip in local.created_nat_instance_eip_resources : eip.public_ip]
}

output "nat_gateway_eips" {
  description = "List of Elastic IP addresses created for the standby NAT gateways."
  value       = [for eip in local.created_nat_gateway_eip_resources : eip.public_ip]
}

output "nat_instance_security_group_id" {
  description = "NAT Instance Security Group ID."
  value       = aws_security_group.nat_instance.id
}

output "autoscaling_group_names" {
  description = "Name of autoscaling groups for NAT instances."
  value = [
    for asg in aws_autoscaling_group.nat_instance
    : asg.name
  ]
}
