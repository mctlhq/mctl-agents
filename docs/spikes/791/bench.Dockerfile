# Throwaway container for the candidate arm of the #791 bench (workstation run).
#
# The harness PRECONDITION says candidate cells keep Bash on the machine running `bench`, so
# they must run where there is no credential and no operator home to read. This image holds
# only a `git archive` export of the repo at the committed SHA: no .git, no host mounts, no
# GitHub/Anthropic/Vault credential. The Hetzner key is passed at `docker run` time by name
# (`-e HETZNER_INFERENCE_API_KEY`), so it never appears in argv or in an image layer.
#
#   git archive HEAD | docker build -t spike791-bench -f docs/spikes/791/bench.Dockerfile -
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv@sha256:798712e57f879c5393777cbda2bb309b29fcdeb0532129d4b1c3125c5385975a /uv /usr/local/bin/uv

RUN useradd --create-home bench
RUN install -d -o bench -g bench /work
WORKDIR /work
COPY --chown=bench:bench . .
USER bench
# dev group included: the implementation oracle runs pytest
RUN uv sync --locked --no-cache
