# Worker container for a Devin Outpost session running in a Lambda MicroVM.
#
# The base image is the same one devin-outpost-k8s runs in its worker pods, so
# the CLI version tracks the regular Devin release flow. Only the `stable` tag
# is supported; never pin a versioned tag.
FROM public.ecr.aws/e0h8a4b6/devin-cli:stable

# git is a hard requirement for the Devin worker; ffmpeg enables screen
# recording and chromium enables browser/computer-use. python3 backs the
# lifecycle-hook supervisor.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium-browser \
        ffmpeg \
        git \
        python3 \
    && rm -rf /var/lib/apt/lists/*

COPY supervisor.py /opt/devin/supervisor.py

ENV DEVIN_WORKER_CACHE_DIR=/var/lib/devin-worker/cache \
    HOOK_PORT=8080

# Only the supervisor is snapshotted. The worker itself starts from the /run
# hook so that each MicroVM resumed from this snapshot serves its own session
# with its own connect token.
ENTRYPOINT ["python3", "/opt/devin/supervisor.py"]
