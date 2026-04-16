locals {
  suffix = "${var.app_name}-${var.environment}"

  # TGI DLC image — account 763104351884 is AWS's deep learning container registry.
  # Update the tag when a newer TGI version is available; override via var.tgi_image_uri.
  default_tgi_image = "763104351884.dkr.ecr.${var.aws_region}.amazonaws.com/huggingface-pytorch-tgi-inference:2.1.1-tgi1.4.5-gpu-py310-cu121-ubuntu22.04"
  tgi_image         = var.tgi_image_uri != "" ? var.tgi_image_uri : local.default_tgi_image
}
