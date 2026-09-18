VARIANT ?= both
IMAGE_TAG ?= 0.1.0
AIPERF_IMAGE_TAG ?= 0.12.0

.PHONY: help up down download-model build-image load-image build-benchmark-image load-benchmark-image

help:
	@echo "Targets:"
	@echo "  up                    Create or reuse the local Kubernetes cluster"
	@echo "  down                  Delete the local Kubernetes cluster"
	@echo "  download-model        Download the pinned model files (LOCAL_K8S_MODELS_DIR overrides .models)"
	@echo "  build-image           Build inference images (VARIANT=base|enhanced|both)"
	@echo "  load-image            Load inference images into the cluster nodes"
	@echo "  build-benchmark-image Build the AIPerf benchmark image"
	@echo "  load-benchmark-image  Load the AIPerf benchmark image into the cluster nodes"
	@echo ""
	@echo "Variables: VARIANT=$(VARIANT) IMAGE_TAG=$(IMAGE_TAG) AIPERF_IMAGE_TAG=$(AIPERF_IMAGE_TAG)"

up:
	./scripts/local-k8s.sh up

down:
	./scripts/local-k8s.sh down

download-model:
	./scripts/download-model.sh

build-image:
	IMAGE_TAG=$(IMAGE_TAG) ./scripts/build-inference-images.sh $(VARIANT)

load-image:
	IMAGE_TAG=$(IMAGE_TAG) ./scripts/load-inference-images.sh $(VARIANT)

build-benchmark-image:
	AIPERF_IMAGE_TAG=$(AIPERF_IMAGE_TAG) ./scripts/build-benchmark-images.sh

load-benchmark-image:
	AIPERF_IMAGE_TAG=$(AIPERF_IMAGE_TAG) ./scripts/load-benchmark-images.sh
