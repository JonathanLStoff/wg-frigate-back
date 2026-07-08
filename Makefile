# --- Configuration & Variables ---
# Default to python 3.11 if no version is provided
X ?= 11
PYTHON_VERSION := 3.$(X)

# Docker configuration
IMAGE_NAME ?= my-python-app
REGISTRY   ?= my-docker-registry.com
TAG        := $(PYTHON_VERSION)
FULL_IMAGE := $(REGISTRY)/$(IMAGE_NAME):$(TAG)

.PHONY: all build push deploy clean help

# --- Targets ---

all: build push deploy ## Run the entire pipeline (build, push, deploy)

build: ## Build the Docker image with the specified Python version
	@echo "========================================="
	@echo "Building Docker image for Python $(PYTHON_VERSION)..."
	@echo "========================================="
	docker build \
		--build-arg PYTHON_VERSION=$(PYTHON_VERSION) \
		-t $(IMAGE_NAME):$(TAG) \
		-t $(FULL_IMAGE) .

push: ## Push the image to the remote registry
	@echo "========================================="
	@echo "Pushing $(FULL_IMAGE) to registry..."
	@echo "========================================="
	docker push $(FULL_IMAGE)

deploy: ## Deploy the image (placeholder for your specific deployment command)
	@echo "========================================="
	@echo "Deploying $(FULL_IMAGE)..."
	@echo "========================================="
	@# Example: kubectl set image deployment/my-deployment my-container=$(FULL_IMAGE)
	@echo "Deployment complete."

clean: ## Remove local images built by this Makefile
	@echo "Cleaning up local images..."
	-docker rmi $(IMAGE_NAME):$(TAG)
	-docker rmi $(FULL_IMAGE)

help: ## Display this help screen
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'