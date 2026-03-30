plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

# These rules are disabled because the existing modules do not yet declare
# required_providers or required_version in their terraform blocks.
# Enable them once the modules are updated.
rule "terraform_required_providers" {
  enabled = false
}

rule "terraform_required_version" {
  enabled = false
}

plugin "aws" {
  enabled = true
  version = "0.35.0"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"
}
