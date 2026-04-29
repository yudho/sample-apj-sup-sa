################################################################################
# Development Environment - Fine-tuning on EKS
################################################################################

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.25"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.12"
    }
    kubectl = {
      source  = "alekc/kubectl"
      version = "~> 2.0"
    }
  }

  # Uncomment and configure for remote state
  # backend "s3" {
  #   bucket         = "your-terraform-state-bucket"
  #   key            = "fine-tuning-on-eks/dev/terraform.tfstate"
  #   region         = "us-west-2"
  #   encrypt        = true
  #   dynamodb_table = "terraform-lock"
  # }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "fine-tuning-on-eks"
      Environment = "dev"
      ManagedBy   = "terraform"
    }
  }
}

provider "kubernetes" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name]
  }
}

provider "helm" {
  kubernetes {
    host                   = module.eks.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)

    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name]
    }
  }
}

provider "kubectl" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)
  load_config_file       = false

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name]
  }
}

################################################################################
# Variables
################################################################################

variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-west-2"
}

variable "name" {
  description = "Name prefix for resources"
  type        = string
  default     = "genai-eks-dev"
}

variable "gpu_instance_types" {
  description = "GPU instance types for training (used by managed node group)"
  type        = list(string)
  default     = ["g5.xlarge"]
}

variable "gpu_instance_categories" {
  description = "GPU instance categories for Karpenter (g = A10G/L40S/RTX, p = A100/H100)"
  type        = list(string)
  default     = ["g", "p"]
}

variable "gpu_min_size" {
  description = "Minimum GPU nodes"
  type        = number
  default     = 0
}

variable "gpu_max_size" {
  description = "Maximum GPU nodes"
  type        = number
  default     = 2
}

variable "gpu_desired_size" {
  description = "Desired GPU nodes"
  type        = number
  default     = 0
}

variable "enable_cilium" {
  description = "Enable Cilium CNI (chaining mode)"
  type        = bool
  default     = true
}

variable "enable_karpenter" {
  description = "Enable Karpenter for node provisioning"
  type        = bool
  default     = true
}

variable "enable_kueue" {
  description = "Enable Kueue for gang scheduling and job queuing"
  type        = bool
  default     = true
}

variable "enable_capacity_block_nodepool" {
  description = "Enable Capacity Block NodePool for p-family instances (A100/H100)"
  type        = bool
  default     = true
}

variable "capacity_block_tags" {
  description = "Tags to match Capacity Block reservations"
  type        = map(string)
  default     = { "purpose" = "ml-training" }
}

################################################################################
# Data Sources
################################################################################

data "aws_availability_zones" "available" {
  state = "available"
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

################################################################################
# VPC
################################################################################

module "vpc" {
  source = "../../modules/vpc"

  name         = var.name
  cidr         = "10.0.0.0/16"
  azs          = slice(data.aws_availability_zones.available.names, 0, 3)
  cluster_name = var.name  # For Karpenter subnet discovery tags

  tags = {
    Environment = "dev"
  }
}

################################################################################
# EKS Cluster
################################################################################

module "eks" {
  source = "../../modules/eks"

  name            = var.name
  cluster_version = "1.33"
  vpc_id          = module.vpc.vpc_id
  subnet_ids      = module.vpc.private_subnets

  tags = {
    Environment = "dev"
  }
}

################################################################################
# Update kubeconfig after EKS cluster is created
################################################################################
# This ensures Helm/kubectl providers can authenticate to the cluster
# during the same terraform apply (solves chicken-and-egg problem)

resource "terraform_data" "update_kubeconfig" {
  # Re-run when cluster endpoint changes (new cluster or update)
  triggers_replace = [
    module.eks.cluster_endpoint
  ]

  provisioner "local-exec" {
    command = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
  }

  depends_on = [module.eks]
}

################################################################################
# GPU Node Group (Managed Node Group - fallback when Karpenter is disabled)
################################################################################

module "gpu_nodegroup" {
  source = "../../modules/gpu-nodegroup"
  count  = var.enable_karpenter ? 0 : 1  # Disable when using Karpenter

  cluster_name   = module.eks.cluster_name
  subnet_ids     = module.vpc.private_subnets
  instance_types = var.gpu_instance_types
  min_size       = var.gpu_min_size
  max_size       = var.gpu_max_size
  desired_size   = var.gpu_desired_size
  disk_size      = 200

  tags = {
    Environment = "dev"
  }
}

################################################################################
# Cilium CNI (Chaining Mode)
################################################################################

module "cilium" {
  source = "../../modules/cilium"
  count  = var.enable_cilium ? 1 : 0

  cluster_name                       = module.eks.cluster_name
  cluster_endpoint                   = module.eks.cluster_endpoint
  cluster_certificate_authority_data = module.eks.cluster_certificate_authority_data

  enable_hubble    = true
  enable_hubble_ui = true

  tags = {
    Environment = "dev"
  }

  depends_on = [module.eks, terraform_data.update_kubeconfig]
}

################################################################################
# Karpenter Node IAM Role
################################################################################

resource "aws_iam_role" "karpenter_node" {
  count = var.enable_karpenter ? 1 : 0

  name = "${var.name}-karpenter-node"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Environment = "dev"
  }
}

resource "aws_iam_role_policy_attachment" "karpenter_node_policies" {
  for_each = var.enable_karpenter ? toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
    "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
  ]) : toset([])

  policy_arn = each.value
  role       = aws_iam_role.karpenter_node[0].name
}

################################################################################
# Karpenter
################################################################################

module "karpenter" {
  source = "../../modules/karpenter"
  count  = var.enable_karpenter ? 1 : 0

  cluster_name                       = module.eks.cluster_name
  cluster_endpoint                   = module.eks.cluster_endpoint
  cluster_certificate_authority_data = module.eks.cluster_certificate_authority_data
  oidc_provider_arn                  = module.eks.oidc_provider_arn
  node_iam_role_arn                  = aws_iam_role.karpenter_node[0].arn

  # Subnet and security group configuration
  subnet_ids         = module.vpc.private_subnets
  security_group_ids = [module.eks.cluster_primary_security_group_id, module.eks.node_security_group_id]

  # GPU NodePool configuration
  enable_gpu_nodepool     = true
  gpu_instance_categories = var.gpu_instance_categories

  # Enable spot termination handling
  enable_spot_termination = true

  # Capacity Block for p-family instances (A100/H100)
  # Requires an active Capacity Block reservation tagged with capacity_block_tags
  enable_capacity_block_nodepool = var.enable_capacity_block_nodepool
  capacity_block_tags            = var.capacity_block_tags

  tags = {
    Environment = "dev"
  }

  depends_on = [
    module.eks,
    terraform_data.update_kubeconfig,
    aws_iam_role_policy_attachment.karpenter_node_policies
  ]
}

################################################################################
# EFS for Shared ML Data (Multi-Node Training)
################################################################################

module "efs" {
  source = "../../modules/efs"

  cluster_name       = module.eks.cluster_name
  vpc_id             = module.vpc.vpc_id
  vpc_cidr           = module.vpc.vpc_cidr_block
  private_subnet_ids = module.vpc.private_subnets
  oidc_provider_arn  = module.eks.oidc_provider_arn

  tags = {
    Environment = "dev"
  }

  depends_on = [module.eks, terraform_data.update_kubeconfig]
}

################################################################################
# KubeRay Operator (Multi-Node Training)
################################################################################

module "kuberay" {
  source = "../../modules/kuberay"

  cluster_name    = module.eks.cluster_name
  kuberay_version = "1.2.2"

  tags = {
    Environment = "dev"
  }

  depends_on = [module.eks, terraform_data.update_kubeconfig]
}

################################################################################
# Kueue (Gang Scheduling)
################################################################################

module "kueue" {
  source = "../../modules/kueue"
  count  = var.enable_kueue ? 1 : 0

  cluster_name   = module.eks.cluster_name
  kueue_version  = "0.16.0"

  tags = {
    Environment = "dev"
  }

  depends_on = [module.eks, terraform_data.update_kubeconfig]
}

################################################################################
# S3 Storage for Checkpoints and Model Outputs
################################################################################

module "s3" {
  source = "../../modules/s3"

  cluster_name         = module.eks.cluster_name
  oidc_provider_arn    = module.eks.oidc_provider_arn
  namespace            = "ml-training"
  service_account_name = "ray-training-sa"

  tags = {
    Environment = "dev"
  }

  depends_on = [module.eks]
}

################################################################################
# NVIDIA GPU Operator
################################################################################

resource "helm_release" "gpu_operator" {
  name             = "gpu-operator"
  repository       = "https://helm.ngc.nvidia.com/nvidia"
  chart            = "gpu-operator"
  namespace        = "gpu-operator"
  create_namespace = true
  version          = "v24.9.2"

  # Disable driver and toolkit (pre-installed in AMI)
  set {
    name  = "driver.enabled"
    value = "false"
  }
  set {
    name  = "toolkit.enabled"
    value = "false"
  }

  # Tolerate GPU node taints
  set {
    name  = "daemonsets.tolerations[0].key"
    value = "nvidia.com/gpu"
  }
  set {
    name  = "daemonsets.tolerations[0].operator"
    value = "Exists"
  }
  set {
    name  = "daemonsets.tolerations[0].effect"
    value = "NoSchedule"
  }

  depends_on = [module.eks, terraform_data.update_kubeconfig]
}

################################################################################
# Outputs
################################################################################

output "cluster_name" {
  description = "EKS cluster name"
  value       = module.eks.cluster_name
}

output "cluster_endpoint" {
  description = "EKS cluster endpoint"
  value       = module.eks.cluster_endpoint
}

output "region" {
  description = "AWS region"
  value       = var.region
}

output "configure_kubectl" {
  description = "Command to configure kubectl"
  value       = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
}

output "gpu_nodegroup_name" {
  description = "GPU node group name (when Karpenter is disabled)"
  value       = var.enable_karpenter ? null : module.gpu_nodegroup[0].node_group_name
}

output "cilium_enabled" {
  description = "Whether Cilium CNI is enabled"
  value       = var.enable_cilium
}

output "karpenter_enabled" {
  description = "Whether Karpenter is enabled"
  value       = var.enable_karpenter
}

output "karpenter_node_role_arn" {
  description = "Karpenter node IAM role ARN"
  value       = var.enable_karpenter ? aws_iam_role.karpenter_node[0].arn : null
}

output "efs_file_system_id" {
  description = "EFS file system ID for shared ML data"
  value       = module.efs.file_system_id
}

output "efs_storage_class" {
  description = "Kubernetes StorageClass name for EFS"
  value       = module.efs.storage_class_name
}

output "kuberay_namespace" {
  description = "Namespace where KubeRay operator is installed"
  value       = module.kuberay.operator_namespace
}

output "kueue_enabled" {
  description = "Whether Kueue is enabled"
  value       = var.enable_kueue
}

output "kueue_namespace" {
  description = "Namespace where Kueue is installed"
  value       = var.enable_kueue ? module.kueue[0].namespace : null
}

output "s3_bucket_name" {
  description = "S3 bucket name for training storage"
  value       = module.s3.bucket_name
}

output "s3_training_role_arn" {
  description = "IAM role ARN for training service account (IRSA)"
  value       = module.s3.training_role_arn
}

output "ray_storage_path" {
  description = "S3 path for Ray storage"
  value       = module.s3.ray_storage_path
}

output "s3_output_path" {
  description = "S3 path for model outputs"
  value       = module.s3.output_path
}
