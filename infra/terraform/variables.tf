variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "europe-west1"
}

variable "github_repository" {
  type        = string
  description = "owner/name of the repository whose main branch may deploy."
}

variable "image" {
  type        = string
  description = "Image for the first apply; CD deploys every later image."
}

variable "outbox_schedule" {
  type        = string
  default     = "*/5 * * * *"
  description = "How often the outbox job delivers webhooks (cron, UTC)."
}
