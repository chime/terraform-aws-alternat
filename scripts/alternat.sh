#!/bin/bash

# Send output to a file and to the console
# Credit to the alestic blog for this one-liner
# https://alestic.com/2010/12/ec2-user-data-output/
exec > >(tee /var/log/user-data.log|logger -t user-data -s 2>/dev/console) 2>&1

shopt -s expand_aliases

panic() {
  [ -n "$1" ] && echo "$1"
  complete_asg_lifecycle_action ABANDON
  echo "alterNAT setup failed"
  exit 1
}

load_config() {
   if [ -f "$CONFIG_FILE" ]; then
      . "$CONFIG_FILE"
   else
      panic "Config file $CONFIG_FILE not found"
   fi
   validate_var "vpc_cidrs_csv" "$vpc_cidrs_csv"
   validate_var "eip_allocation_ids_csv" "$eip_allocation_ids_csv"
   validate_var "route_table_ids_csv" "$route_table_ids_csv"
   validate_var "enable_ssm" "$enable_ssm"
   validate_var "enable_cloudwatch_agent" "$enable_cloudwatch_agent"
}

validate_var() {
   var_name="$1"
   var_val="$2"
   if [ ! "$2" ]; then
      echo "Config var \"$var_name\" is unset"
      exit 1
   fi
}

# configure_nat() sets up Linux to act as a NAT device.
# See https://docs.aws.amazon.com/vpc/latest/userguide/VPC_NAT_Instance.html#NATInstance
configure_nat() {
   $dnf_cmd install nftables
   systemctl enable --now nftables

   local nic_name="$(ip route show | grep default | sed -n 's/.*dev \([^\ ]*\).*/\1/p')"
   echo "Found interface name ${nic_name}"

   echo "Enabling NAT..."
   # Read more about these settings here: https://www.kernel.org/doc/Documentation/networking/ip-sysctl.txt

   sysctl -q -w "net.ipv4.ip_forward"=1 "net.ipv4.conf.$nic_name.send_redirects"=0 "net.ipv4.ip_local_port_range"="1024 65535" ||
      panic

   local vpc_cidrs
   IFS=',' read -r -a vpc_cidrs <<< "${vpc_cidrs_csv}"
   echo "Retrieved VPC CIDR range(s) ${vpc_cidrs[*]} from config"

   nft add table ip nat
   nft add chain ip nat postrouting { type nat hook postrouting priority 100 \; }

   for cidr in "${vpc_cidrs[@]}";
   do
      nft add rule ip nat postrouting ip saddr "$cidr" oif "$nic_name" masquerade
      if [ $? -ne 0 ]; then
         panic "Unable to add nft rule for cidr $cidr. nft exited with status $?"
      fi
   done

   sysctl "net.ipv4.ip_forward" "net.ipv4.conf.${nic_name}.send_redirects" "net.ipv4.ip_local_port_range"
   nft list ruleset

   echo "NAT configuration complete"
}

# Disabling source/dest check is what makes a NAT instance a NAT instance.
# See https://docs.aws.amazon.com/vpc/latest/userguide/VPC_NAT_Instance.html#EIP_Disable_SrcDestCheck
disable_source_dest_check() {
   echo "Disabling source/destination check"
   aws ec2 modify-instance-attribute --instance-id $INSTANCE_ID --source-dest-check "{\"Value\": false}"
   if [ $? -ne 0 ]; then
      panic "Unable to disable source/dest check."
   fi
   echo "source/destination check disabled for $INSTANCE_ID"
}

# associate_eip() tries each provided EIP allocation id until it finds one that is not already associated.
function associate_eip() {
   echo "Associating an EIP from the pool of addresses"

   local associated_allocation_id=""
   local eip=""
   local num_retries=10
   local sleep_len=60

   IFS=',' read -r -a eip_allocation_ids <<< "${eip_allocation_ids_csv}"

   # Retry the allocation operation $num_retries times with a $sleep_len wait between retries.
   # This is to handle any delays in releasing an EIP allocation during instance termination.
   for n in $(seq 1 "$num_retries"); do
      for eip_allocation_id in "${eip_allocation_ids[@]}"
      do
         eip=$(aws ec2 describe-addresses --allocation-ids "$eip_allocation_id" --query 'Addresses[0].PublicIp' | tr -d '"')
         echo "Trying IP $eip"
         aws ec2 associate-address --no-allow-reassociation --allocation-id "$eip_allocation_id" --instance-id "$INSTANCE_ID"
         if [ $? -eq 0 ]; then
            break
         fi
         echo "Failed to associate IP $eip"
         eip=""
      done
      if [ ! -z "$eip" ]; then
         break
      else
         echo "Unable to associate an EIP ($n of $num_retries attempts)."
         sleep "$sleep_len"
      fi
   done

   if [ -z "$eip" ]; then
      panic "Unable to associate an EIP!"
   fi

   echo "Associated EIP $eip with instance $INSTANCE_ID";
}

# First try to replace an existing route
# If no route exists already (e.g. first time set up) then create the route.
configure_route_table() {
   echo "Configuring route tables"

   IFS=',' read -r -a route_table_ids <<< "${route_table_ids_csv}"

   for route_table_id in "${route_table_ids[@]}"
   do
      echo "Attempting to find route table $route_table_id"
      local rtb_id=$(aws ec2 describe-route-tables --filters Name=route-table-id,Values=${route_table_id} --query 'RouteTables[0].RouteTableId' | tr -d '"')
      if [ -z "$rtb_id" ]; then
         panic "Unable to find route table $rtb_id"
      fi

      echo "Found route table $rtb_id"
      echo "Replacing route to 0.0.0.0/0 for $rtb_id"
      aws ec2 replace-route --route-table-id "$rtb_id" --instance-id "$INSTANCE_ID" --destination-cidr-block 0.0.0.0/0
      if [ $? -eq 0 ]; then
         echo "Successfully replaced route to 0.0.0.0/0 via instance $INSTANCE_ID for route table $rtb_id"
         continue
      fi

      echo "Unable to replace route. Attempting to create route"
      aws ec2 create-route --route-table-id "$rtb_id" --instance-id "$INSTANCE_ID" --destination-cidr-block 0.0.0.0/0
      if [ $? -eq 0 ]; then
         echo "Successfully created route to 0.0.0.0/0 via instance $INSTANCE_ID for route table $rtb_id"
      else
         panic "Unable to replace or create the route!"
      fi
   done
}

# install_ssm_agent() installs the SSM agent if enable_ssm is true.
install_ssm_agent() {
   if [ "$enable_ssm" = "true" ]; then
      echo "Installing SSM agent"
      $dnf_cmd install amazon-ssm-agent && \
      systemctl enable --now amazon-ssm-agent
      if [ $? -ne 0 ]; then
         panic "Unable to install SSM agent"
      fi
      echo "SSM agent installed successfully"
   fi
}

# install_cloudwatch_agent() installs the CloudWatch Agent if enable_cloudwatch_agent is true.
install_cloudwatch_agent() {
   if [ "$enable_cloudwatch_agent" = "true" ]; then
      echo "Installing CloudWatch agent"
      $dnf_cmd install amazon-cloudwatch-agent && \
      systemctl enable --now amazon-cloudwatch-agent
      if [ $? -ne 0 ]; then
         panic "Unable to install CloudWatch Agent"
      fi
      echo "CloudWatch Agent installed successfully"
   fi
}

ASG_LIFECYCLE_HOOK_NAME="NATInstanceLaunchScript"
complete_asg_lifecycle_action() {
  if [[ -z "$1" ]]; then
    echo "No ASG lifecycle action result given"
    return
  fi
  if [[ -z "${AUTO_SCALING_GROUP_NAME}" ]]; then
    echo "Skipping ASG lifecycle action"
    return
  fi

  local output status
  output="$(aws autoscaling complete-lifecycle-action \
    --lifecycle-hook-name "${ASG_LIFECYCLE_HOOK_NAME}" \
    --auto-scaling-group-name "${AUTO_SCALING_GROUP_NAME}" \
    --lifecycle-action-result "$1" \
    --instance-id "${INSTANCE_ID}" 2>&1)"
  status=$?
  if [[ $status -ne 0 ]]; then
    if grep -q "No active Lifecycle Action found" <<< "${output}"; then
      echo "Ignoring missing ASG lifecycle action"
      return
    else
      echo "${output}"
      echo "Failed to complete ASG lifecycle action"
      return
    fi
  fi

  echo "Completed ASG lifecycle action with result $1"
}

retrieve_ec2_metadata() {
  INSTANCE_ID="$(ec2-metadata --quiet --instance-id)"

  AWS_DEFAULT_REGION="$(ec2-metadata --quiet --region)"
  export AWS_DEFAULT_REGION

  AUTO_SCALING_GROUP_NAME="$(ec2-metadata --quiet --tags | grep 'aws:autoscaling:groupName:' | awk '{print $2}')"
}

dnf_cmd="dnf --quiet --assumeyes"

# Set CLI Output to text
export AWS_DEFAULT_OUTPUT="text"

# Disable pager output
# https://docs.aws.amazon.com/cli/latest/userguide/cli-usage-pagination.html#cli-usage-pagination-clientside
export AWS_PAGER=""

retrieve_ec2_metadata

# alterNAT config file containing inputs needed for initialization
CONFIG_FILE="/etc/alternat.conf"
load_config

echo "Beginning self-managed NAT configuration"
install_ssm_agent
install_cloudwatch_agent
configure_nat
disable_source_dest_check
associate_eip
configure_route_table
complete_asg_lifecycle_action CONTINUE
echo "Configuration completed successfully!"
