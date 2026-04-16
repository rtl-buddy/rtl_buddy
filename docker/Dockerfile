# DESCRIPTION: Dockerfile for image to run Verilator + rtl_buddy inside
#
# Adapted from the Verilator repo Dockerfile (ci/docker/run/Dockerfile).
# SPDX-License-Identifier: LGPL-3.0-only OR Artistic-2.0

FROM ubuntu:24.04

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
        autoconf \
        bc \
        bison \
        build-essential \
        ca-certificates \
        ccache \
        flex \
        git \
        help2man \
        libfl2 \
        libfl-dev \
        libgoogle-perftools-dev \
        numactl \
        perl \
        perl-doc \
        python3 \
        zlib1g \
        zlib1g-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

ARG VERILATOR_REPO=https://github.com/verilator/verilator
ARG VERILATOR_VERSION=master
ARG RTL_BUDDY_VERSION

WORKDIR /tmp

# Install Verilator from source
# hadolint ignore=DL3003
RUN git clone --depth 1 --branch "${VERILATOR_VERSION}" "${VERILATOR_REPO}" verilator \
    && cd verilator \
    && autoconf \
    && ./configure \
    && make -j "$(nproc)" \
    && make install \
    && cd .. \
    && rm -rf verilator \
    && ccache -C

# Install rtl_buddy from PyPI via uv
# Strip leading 'v' so "v2.1.4" becomes the valid PyPI specifier "2.1.4".
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN version="$(echo "${RTL_BUDDY_VERSION}" | sed 's/^v//')" \
    && uv pip install --system "rtl_buddy==${version}"

WORKDIR /work

COPY verilator-wrap.sh /usr/local/bin/verilator-wrap.sh
